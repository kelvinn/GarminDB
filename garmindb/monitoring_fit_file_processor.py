"""Class that takes a parsed monitoring FIT file object and imports it into a database."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import logging
import sys
import traceback
import datetime
from collections import defaultdict

import fitfile
import idbutils
from sqlalchemy.dialects.postgresql import insert as postgres_insert

from .garmindb import File, Stress
from .garmindb import MonitoringDb, Monitoring, MonitoringInfo, MonitoringHeartRate, MonitoringIntensity, MonitoringClimb, MonitoringRespirationRate, MonitoringPulseOx, \
    MonitoringHrvValue, MonitoringHrvStatus
from .fit_file_processor import FitFileProcessor


logger = logging.getLogger(__file__)
logger.addHandler(logging.StreamHandler(stream=sys.stdout))
root_logger = logging.getLogger()


class MonitoringFitFileProcessor(FitFileProcessor):
    """Class that takes a parsed monitoring FIT file object and imports it into a database."""

    def __init__(self, db_params, plugin_manager=None, debug=0):
        """Return a new MonitoringFitFileProcessor instance."""
        super().__init__(db_params, plugin_manager, debug)
        self.monitoring_fit_file_plugins = []
        self._bulk_upsert_entries = defaultdict(list)
        self._monitoring_db_table_signature = None
        self.garmin_mon_db = None

    def write_file(self, fit_file):
        """Given a Fit File object, write all of its messages to the DB."""
        self.monitoring_fit_file_plugins = [plugin for plugin in self.plugin_manager.get_file_processors('MonitoringFit', fit_file).values()]
        if len(self.monitoring_fit_file_plugins):
            root_logger.info("Loaded %d monitoring plugins %r for file %s", len(self.monitoring_fit_file_plugins), self.monitoring_fit_file_plugins, fit_file)
        self._ensure_monitoring_db()
        self.garmin_db_session = self._session_for_db(self.garmin_db)
        self.garmin_mon_db_session = self._session_for_db(self.garmin_mon_db)
        try:
            self._write_message_types(fit_file, fit_file.message_types)
            self._flush_bulk_upserts()
            self._commit_active_sessions()
        except Exception:
            self._rollback_active_sessions()
            raise
        finally:
            self._flush_closed_transaction_error_summary()
            self._bulk_upsert_entries = defaultdict(list)
            self._close_active_sessions()
            self.garmin_db_session = None
            self.garmin_mon_db_session = None

    def _plugin_dispatch(self, handler_name, *args, **kwargs):
        return super()._plugin_dispatch(self.monitoring_fit_file_plugins, handler_name, *args, **kwargs)

    def _ensure_monitoring_db(self):
        table_signature = tuple(sorted(MonitoringDb.db_tables))
        if self.garmin_mon_db is None or table_signature != self._monitoring_db_table_signature:
            if self.garmin_mon_db is not None:
                self.garmin_mon_db.engine.dispose()
            self.garmin_mon_db = MonitoringDb(self.db_params, self.debug - 1)
            self._monitoring_db_table_signature = table_signature

    def _session_is_postgres(self, session):
        if not hasattr(session, 'get_bind'):
            return False
        bind = session.get_bind()
        return bind is not None and bind.dialect.name == 'postgresql'

    def _insert_or_update(self, table, session, entry, ignore_none=True, ignore_zero=False):
        if self._session_is_postgres(session):
            clean_entry = dict(entry)
            if ignore_none:
                clean_entry = {key: value for key, value in clean_entry.items() if value is not None}
            if ignore_zero:
                clean_entry = {key: value for key, value in clean_entry.items() if value != 0}
            self._bulk_upsert_entries[(table, session)].append(clean_entry)
        else:
            table.s_insert_or_update(session, entry, ignore_none=ignore_none, ignore_zero=ignore_zero)

    @classmethod
    def _primary_key_names(cls, table):
        return [column.name for column in table.__table__.primary_key.columns]

    @classmethod
    def _dedupe_rows(cls, table, rows):
        primary_key_names = cls._primary_key_names(table)
        deduped = {}
        for row in rows:
            key = tuple(row.get(name) for name in primary_key_names)
            if key not in deduped:
                deduped[key] = dict(row)
            else:
                deduped[key].update({column: value for column, value in row.items() if value is not None})
        return list(deduped.values())

    @classmethod
    def _group_rows_by_columns(cls, rows):
        grouped_rows = defaultdict(list)
        for row in rows:
            grouped_rows[tuple(sorted(row))].append(row)
        return grouped_rows.values()

    def _bulk_insert_or_update(self, table, session, rows):
        rows = self._dedupe_rows(table, rows)
        primary_key_names = self._primary_key_names(table)
        for grouped_rows in self._group_rows_by_columns(rows):
            insert_statement = postgres_insert(table.__table__).values(grouped_rows)
            row_columns = set(grouped_rows[0])
            update_columns = sorted(row_columns.difference(primary_key_names))
            if update_columns:
                update_statement = insert_statement.on_conflict_do_update(
                    index_elements=primary_key_names,
                    set_={column: getattr(insert_statement.excluded, column) for column in update_columns}
                )
            else:
                update_statement = insert_statement.on_conflict_do_nothing(index_elements=primary_key_names)
            session.execute(update_statement)

    def _flush_bulk_upserts(self):
        for (table, session), rows in self._bulk_upsert_entries.items():
            if rows:
                self._bulk_insert_or_update(table, session, rows)
        self._bulk_upsert_entries = defaultdict(list)

    @classmethod
    def __unpack_tuple(cls, entry, name, value, index):
        if type(value) is tuple:
            entry[name] = value[index]

    def _write_monitoring_info_entry(self, fit_file, message_fields):
        activity_types = message_fields.activity_type
        if isinstance(activity_types, list):
            for index, activity_type in enumerate(activity_types):
                entry = {
                    'file_id'                   : File.s_get_id(self.garmin_db_session, fit_file.filename),
                    'timestamp'                 : message_fields.local_timestamp,
                    'activity_type'             : activity_type,
                    'resting_metabolic_rate'    : message_fields.get('resting_metabolic_rate')
                }
                self.__unpack_tuple(entry, 'cycles_to_distance', message_fields.cycles_to_distance, index)
                self.__unpack_tuple(entry, 'cycles_to_calories', message_fields.cycles_to_calories, index)
                self._insert_or_update(MonitoringInfo, self.garmin_mon_db_session, entry)

    def _write_monitoring_entry(self, fit_file, message_fields):
        # Only include not None values so that we match and update only if a table's columns if it has values.
        entry = idbutils.list_and_dict.dict_filter_none_values(message_fields)
        timestamp = fit_file.utc_datetime_to_local(message_fields.timestamp)
        # Hack: daily monitoring summaries appear at 00:00:00 localtime for the PREVIOUS day. Subtract a second so they appear in the previous day.
        if timestamp.time() == datetime.time.min:
            timestamp = timestamp - datetime.timedelta(seconds=1)
        entry['timestamp'] = timestamp
        logger.debug("monitoring entry: %r", entry)
        try:
            intersection = MonitoringHeartRate.intersection(entry)
            if len(intersection) > 1 and intersection['heart_rate'] > 0:
                self._insert_or_update(MonitoringHeartRate, self.garmin_mon_db_session, intersection)
            intersection = MonitoringIntensity.intersection(entry)
            if len(intersection) > 1:
                self._insert_or_update(MonitoringIntensity, self.garmin_mon_db_session, intersection)
            intersection = MonitoringClimb.intersection(entry)
            if len(intersection) > 1:
                self._insert_or_update(MonitoringClimb, self.garmin_mon_db_session, intersection)
            intersection = Monitoring.intersection(entry)
            if len(intersection) > 1:
                self._insert_or_update(Monitoring, self.garmin_mon_db_session, intersection)
        except ValueError:
            logger.error("write_monitoring_entry: ValueError for %r: %s", entry, traceback.format_exc())
        except Exception:
            logger.error("Exception on monitoring entry: %r: %s", entry, traceback.format_exc())

    def _write_respiration_entry(self, fit_file, message_fields):
        logger.debug("respiration message: %r", message_fields)
        rr = message_fields.get('respiration_rate')
        if rr > 0:
            respiration = {
                'timestamp' : fit_file.utc_datetime_to_local(message_fields.timestamp),
                'rr'        : rr,
            }
            if fit_file.type is fitfile.FileType.monitoring_b:
                self._insert_or_update(MonitoringRespirationRate, self.garmin_mon_db_session, respiration)
            else:
                raise ValueError(f'Unexpected file type {repr(fit_file.type)} for respiration message')

    def _write_pulse_ox_entry(self, fit_file, message_fields):
        logger.debug("pulse_ox message: %r", message_fields)
        if fit_file.type is fitfile.FileType.monitoring_b:
            pulse_ox = message_fields.get('pulse_ox')
            if pulse_ox is not None:
                pulse_ox_entry = {
                    'timestamp': fit_file.utc_datetime_to_local(message_fields.timestamp),
                    'pulse_ox': pulse_ox,
                }
                self._insert_or_update(MonitoringPulseOx, self.garmin_mon_db_session, pulse_ox_entry)
        else:
            raise ValueError(f'Unexpected file type {repr(fit_file.type)} for pulse ox')

    def _write_hrv_value_entry(self, fit_file, message_fields):
        """Write an HRV reading entry to the database."""
        logger.debug("hrv_value message: %r", message_fields)
        hrv_value = message_fields.get('hrv_value')
        if hrv_value is not None and hrv_value > 0:
            # HRV values are scaled by 128 in the FIT file
            hrv_entry = {
                'timestamp': fit_file.utc_datetime_to_local(message_fields.timestamp),
                'hrv': hrv_value / 128.0,  # Convert to milliseconds
            }
            self._insert_or_update(MonitoringHrvValue, self.garmin_mon_db_session, hrv_entry)

    def _write_hrv_status_summary_entry(self, fit_file, message_fields):
        """Write an HRV status summary entry to the database."""
        logger.debug("hrv_status_summary message: %r", message_fields)
        # HRV values are scaled by 128 in the FIT file
        hrv_status_entry = {
            'timestamp': fit_file.utc_datetime_to_local(message_fields.timestamp),
            'weekly_average': message_fields.get('weekly_average', 0) / 128.0 if message_fields.get('weekly_average') else None,
            'last_night': message_fields.get('last_night', 0) / 128.0 if message_fields.get('last_night') else None,
            'last_night_average': message_fields.get('last_night_average', 0) / 128.0 if message_fields.get('last_night_average') else None,
            'baseline_low': message_fields.get('baseline_low', 0) / 128.0 if message_fields.get('baseline_low') else None,
            'baseline_high': message_fields.get('baseline_high', 0) / 128.0 if message_fields.get('baseline_high') else None,
            'status': message_fields.get('status'),
            'reading_count': message_fields.get('reading_count'),
        }
        self._insert_or_update(MonitoringHrvStatus, self.garmin_mon_db_session, hrv_status_entry)

    def _write_stress_level_entry(self, fit_file, message_fields):
        stress = {
            'timestamp' : message_fields.local_timestamp,
            'stress'    : message_fields.stress_level
        }
        self._insert_or_update(Stress, self.garmin_db_session, stress)
