"""Unit tests for FIT file transaction recovery behavior."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import datetime
import logging
import types
import unittest
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import patch

import fitfile
from sqlalchemy.exc import InvalidRequestError

from garmindb.fit_file_processor import FitFileProcessor
from garmindb.monitoring_fit_file_processor import MonitoringFitFileProcessor
from garmindb.garmindb import MonitoringPulseOx


class _FakeSession:

    def __init__(self):
        self.events = []

    def commit(self):
        self.events.append('commit')

    def rollback(self):
        self.events.append('rollback')

    def close(self):
        self.events.append('close')


class _FakePostgresSession:

    def __init__(self):
        self.statements = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))

    def execute(self, statement):
        self.statements.append(statement)


class _FakeMessage:

    def __init__(self, fields, label=None):
        self.fields = fields
        self.label = label

    def __repr__(self):
        return f'<FakeMessage {self.label}>'


class _Fields(dict):

    def __getattr__(self, key):
        return self[key]


class _LogCaptureHandler(logging.Handler):

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class TestFitFileProcessorTransactions(unittest.TestCase):

    def _base_processor(self):
        processor = FitFileProcessor.__new__(FitFileProcessor)
        processor.db_params = SimpleNamespace(db_type='sqlite')
        processor._closed_transaction_error_counts = {}
        processor.garmin_db_session = _FakeSession()
        return processor

    def _monitoring_processor(self):
        processor = MonitoringFitFileProcessor.__new__(MonitoringFitFileProcessor)
        processor.db_params = SimpleNamespace(db_type='sqlite')
        processor._closed_transaction_error_counts = {}
        processor.garmin_mon_db_session = _FakeSession()
        return processor

    def test_generic_writer_logs_failure_then_continues(self):
        processor = self._base_processor()
        processed = []

        def write_test_entry(self, fit_file, fields):
            processed.append(fields['id'])
            if fields.get('fail'):
                raise ValueError('bad row')

        processor._write_test_entry = types.MethodType(write_test_entry, processor)
        fit_file = SimpleNamespace(filename='file.fit')
        message_type = SimpleNamespace(name='test')
        messages = [
            _FakeMessage({'id': 1, 'fail': True}, label='bad'),
            _FakeMessage({'id': 2, 'fail': False}, label='good'),
        ]

        processor._FitFileProcessor__write_generic(fit_file, message_type, messages)

        self.assertEqual(processed, [1, 2])
        self.assertEqual(processor.garmin_db_session.events, [])

    def test_closed_transaction_errors_are_deduplicated(self):
        processor = self._base_processor()

        def write_test_entry(self, fit_file, fields):
            raise InvalidRequestError(FitFileProcessor.CLOSED_TRANSACTION_ERROR_TEXT)

        processor._write_test_entry = types.MethodType(write_test_entry, processor)
        fit_file = SimpleNamespace(filename='file.fit')
        message_type = SimpleNamespace(name='test')
        messages = [_FakeMessage({'id': i}, label=str(i)) for i in range(3)]

        capture = _LogCaptureHandler()
        root_logger = logging.getLogger()
        root_logger.addHandler(capture)
        try:
            processor._FitFileProcessor__write_generic(fit_file, message_type, messages)
            processor._flush_closed_transaction_error_summary()
        finally:
            root_logger.removeHandler(capture)

        error_records = [record for record in capture.records if 'Failed to write message' in record.getMessage()]
        summary_records = [record for record in capture.records if 'Suppressed 2 repeated closed transaction errors' in record.getMessage()]

        self.assertEqual(len(error_records), 2)
        self.assertEqual(len(summary_records), 1)

    def test_monitoring_pulse_ox_continues_after_first_insert_failure(self):
        processor = self._monitoring_processor()
        fit_file = SimpleNamespace(
            filename='monitoring.fit',
            type=fitfile.FileType.monitoring_b,
            utc_datetime_to_local=lambda dt: dt,
        )
        message_type = SimpleNamespace(name='pulse_ox')
        messages = [
            _FakeMessage(_Fields({'timestamp': datetime.datetime(2022, 3, 18, 19, 30, tzinfo=datetime.timezone.utc), 'pulse_ox': 93.0}), label='1'),
            _FakeMessage(_Fields({'timestamp': datetime.datetime(2022, 3, 18, 19, 31, tzinfo=datetime.timezone.utc), 'pulse_ox': 94.0}), label='2'),
            _FakeMessage(_Fields({'timestamp': datetime.datetime(2022, 3, 18, 19, 32, tzinfo=datetime.timezone.utc), 'pulse_ox': 95.0}), label='3'),
        ]

        with patch.object(MonitoringPulseOx, 's_insert_or_update', side_effect=[ValueError('bad pulse_ox row'), None, None]) as insert_mock:
            processor._FitFileProcessor__write_generic(fit_file, message_type, messages)

        self.assertEqual(insert_mock.call_count, 3)
        self.assertEqual(processor.garmin_mon_db_session.events, [])

    def test_postgres_monitoring_writes_use_bulk_conflict_upsert(self):
        processor = self._monitoring_processor()
        processor._bulk_upsert_entries = defaultdict(list)
        session = _FakePostgresSession()
        rows = [
            {'timestamp': datetime.datetime(2022, 3, 18, 19, 30), 'pulse_ox': 93.0},
            {'timestamp': datetime.datetime(2022, 3, 18, 19, 31), 'pulse_ox': 94.0},
        ]

        with patch.object(MonitoringPulseOx, 's_insert_or_update') as insert_mock:
            for row in rows:
                processor._insert_or_update(MonitoringPulseOx, session, row)
            processor._flush_bulk_upserts()

        self.assertFalse(insert_mock.called)
        self.assertEqual(len(session.statements), 1)
        self.assertIn('ON CONFLICT', str(session.statements[0]))


if __name__ == '__main__':
    unittest.main(verbosity=2)
