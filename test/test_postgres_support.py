"""Unit tests for postgres support helpers."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import datetime
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from sqlalchemy import column
from sqlalchemy.types import Integer, Time

import idbutils
from garmindb import postgres_support


class _FakeCursor:

    def __init__(self):
        self.commands = []
        self.closed = False

    def execute(self, command):
        self.commands.append(command)

    def close(self):
        self.closed = True


class _FakeDbapiConnection:

    def __init__(self):
        self.cursor_value = _FakeCursor()

    def cursor(self):
        return self.cursor_value


class _FakeSqlAlchemyConnection:

    def __init__(self):
        self.commands = []

    def exec_driver_sql(self, command):
        self.commands.append(command)


class _FakeEngineBeginContext:

    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _FakeEngine:

    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _FakeEngineBeginContext(self.connection)


class _FakePreparedConnection:

    def __init__(self):
        self.executed = []

    def execute(self, statement):
        self.executed.append(str(statement))


class _FakeLatestTimeTable:

    time_col = column('day')
    last_call = None
    return_value = None

    @classmethod
    def get_col_max_greater_than_value(cls, db, col, match_col, match_value, start_ts=None, end_ts=None):
        cls.last_call = (db, col, match_col, match_value, start_ts, end_ts)
        return cls.return_value


class TestPostgresSupport(unittest.TestCase):
    """Tests for postgres search_path setup."""

    def test_postgres_backend_profile_defaults_to_postgres(self):
        db_params = SimpleNamespace(db_host='db.example.com')
        profile = postgres_support._postgres_backend_profile(db_params)
        self.assertEqual(profile, 'postgres_native')

    def test_postgres_connect_args_uses_defaults(self):
        db_params = SimpleNamespace()
        connect_args = postgres_support._postgres_connect_args(db_params)
        self.assertEqual(connect_args, {'connect_timeout': 10})

    def test_postgres_connect_args_includes_statement_timeout_when_configured(self):
        db_params = SimpleNamespace(postgres_connect_timeout_sec=25, postgres_statement_timeout_ms=30000)
        connect_args = postgres_support._postgres_connect_args(db_params)
        self.assertEqual(connect_args, {'connect_timeout': 25, 'options': '-c statement_timeout=30000'})

    def test_create_postgres_engine_uses_pre_ping_and_connect_args(self):
        db_params = SimpleNamespace(postgres_connect_timeout_sec=9, postgres_statement_timeout_ms=0)
        with patch.object(postgres_support, 'create_engine', return_value='engine') as create_engine_mock:
            engine = postgres_support._create_postgres_engine('postgresql+psycopg://db', db_params, echo=True)
        self.assertEqual(engine, 'engine')
        create_engine_mock.assert_called_once_with(
            'postgresql+psycopg://db',
            echo=True,
            pool_pre_ping=True,
            connect_args={'connect_timeout': 9}
        )

    def test_install_search_path_event_registers_connect_and_begin_postgres_native(self):
        engine = object()
        with patch.object(postgres_support.event, 'listen') as listen_mock:
            postgres_support._install_search_path_event(engine, 'garmin', 'postgres_native')

        self.assertEqual(listen_mock.call_count, 2)
        calls = {call.args[1]: call.args[2] for call in listen_mock.call_args_list}
        self.assertIn('connect', calls)
        self.assertIn('begin', calls)

        dbapi_connection = _FakeDbapiConnection()
        calls['connect'](dbapi_connection, None)
        self.assertEqual(dbapi_connection.cursor_value.commands, ['SET search_path TO "garmin", public'])

        connection = _FakeSqlAlchemyConnection()
        calls['begin'](connection)
        self.assertEqual(connection.commands, ['SET LOCAL search_path TO "garmin", public'])

    def test_set_local_search_path_uses_local_setting_postgres_native(self):
        connection = _FakeSqlAlchemyConnection()
        postgres_support._set_local_search_path(connection, 'monitoring', 'postgres_native')
        self.assertEqual(connection.commands, ['SET LOCAL search_path TO "monitoring", public'])

    def test_set_search_path_closes_cursor_postgres_native(self):
        dbapi_connection = _FakeDbapiConnection()
        postgres_support._set_search_path(dbapi_connection, None, 'activities', 'postgres_native')
        self.assertEqual(dbapi_connection.cursor_value.commands, ['SET search_path TO "activities", public'])
        self.assertTrue(dbapi_connection.cursor_value.closed)

    def test_set_local_search_path_validates_schema_identifier(self):
        connection = _FakeSqlAlchemyConnection()
        with self.assertRaises(postgres_support.PostgresSupportException):
            postgres_support._set_local_search_path(connection, 'bad-schema', 'postgres_native')

    def test_prepare_postgres_engine_installs_functions_for_postgres_native(self):
        connection = _FakePreparedConnection()
        engine = _FakeEngine(connection)
        with patch.object(postgres_support, '_install_search_path_event') as search_path_mock, \
                patch.object(postgres_support, '_install_postgres_functions') as install_functions_mock:
            postgres_support._prepare_postgres_engine(engine, 'garmin', 'postgres_native')
        search_path_mock.assert_called_once_with(engine, 'garmin', 'postgres_native')
        install_functions_mock.assert_called_once_with(connection, 'garmin')
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "garmin"', connection.executed[0])
        self.assertIn('SET search_path TO "garmin", public', connection.executed[1])

    def test_install_postgres_functions_uses_create_or_replace_without_cascade(self):
        connection = _FakePreparedConnection()
        postgres_support._install_postgres_functions(connection, 'garmin')

        ddl = '\n'.join(connection.executed)
        self.assertIn('CREATE OR REPLACE FUNCTION "garmin".strftime', ddl)
        self.assertIn('CREATE OR REPLACE FUNCTION "garmin".round', ddl)
        self.assertNotIn('DROP FUNCTION', ddl)
        self.assertNotIn('CASCADE', ddl)

    def test_latest_time_postgres_time_column_uses_time_min_threshold(self):
        db = SimpleNamespace(db_params=SimpleNamespace(db_type='postgres'))
        time_column = column('total_sleep', Time())
        _FakeLatestTimeTable.return_value = datetime.datetime(2026, 1, 1)
        _FakeLatestTimeTable.last_call = None

        result = idbutils.DbObject.latest_time.__func__(_FakeLatestTimeTable, db, time_column)

        self.assertEqual(result, datetime.datetime(2026, 1, 1))
        self.assertIsNotNone(_FakeLatestTimeTable.last_call)
        self.assertIs(_FakeLatestTimeTable.last_call[0], db)
        self.assertEqual(_FakeLatestTimeTable.last_call[1], _FakeLatestTimeTable.time_col)
        self.assertEqual(_FakeLatestTimeTable.last_call[2], time_column)
        self.assertEqual(_FakeLatestTimeTable.last_call[3], datetime.time.min)

    def test_latest_time_postgres_non_time_column_keeps_original_threshold(self):
        db = SimpleNamespace(db_params=SimpleNamespace(db_type='postgres'))
        int_column = column('heart_rate', Integer())
        _FakeLatestTimeTable.return_value = datetime.datetime(2026, 1, 2)
        _FakeLatestTimeTable.last_call = None

        result = idbutils.DbObject.latest_time.__func__(_FakeLatestTimeTable, db, int_column)

        self.assertEqual(result, datetime.datetime(2026, 1, 2))
        self.assertIsNotNone(_FakeLatestTimeTable.last_call)
        self.assertEqual(_FakeLatestTimeTable.last_call[3], 0)

    def test_latest_time_non_postgres_keeps_original_threshold(self):
        db = SimpleNamespace(db_params=SimpleNamespace(db_type='sqlite'))
        time_column = column('total_sleep', Time())
        _FakeLatestTimeTable.return_value = datetime.datetime(2026, 1, 3)
        _FakeLatestTimeTable.last_call = None

        result = idbutils.DbObject.latest_time.__func__(_FakeLatestTimeTable, db, time_column)

        self.assertEqual(result, datetime.datetime(2026, 1, 3))
        self.assertIsNotNone(_FakeLatestTimeTable.last_call)
        self.assertEqual(_FakeLatestTimeTable.last_call[3], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
