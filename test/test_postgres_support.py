"""Unit tests for postgres support helpers."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import unittest
from unittest.mock import patch
from types import SimpleNamespace

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


class TestPostgresSupport(unittest.TestCase):
    """Tests for postgres search_path setup."""

    def test_postgres_backend_profile_defaults_to_postgres(self):
        db_params = SimpleNamespace(db_host='db.example.com')
        profile = postgres_support._postgres_backend_profile(db_params)
        self.assertEqual(profile, 'postgres_native')

    def test_postgres_backend_profile_detects_motherduck(self):
        db_params = SimpleNamespace(db_host='api.MOTHERDUCK.com')
        profile = postgres_support._postgres_backend_profile(db_params)
        self.assertEqual(profile, 'motherduck_pgwire')

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

    def test_install_search_path_event_registers_connect_and_begin_motherduck(self):
        engine = object()
        with patch.object(postgres_support.event, 'listen') as listen_mock:
            postgres_support._install_search_path_event(engine, 'garmin', 'motherduck_pgwire')

        self.assertEqual(listen_mock.call_count, 2)
        calls = {call.args[1]: call.args[2] for call in listen_mock.call_args_list}

        dbapi_connection = _FakeDbapiConnection()
        calls['connect'](dbapi_connection, None)
        self.assertEqual(dbapi_connection.cursor_value.commands, ["SET search_path = 'garmin,public'"])

        connection = _FakeSqlAlchemyConnection()
        calls['begin'](connection)
        self.assertEqual(connection.commands, ["SET search_path = 'garmin,public'"])

    def test_set_local_search_path_uses_local_setting_postgres_native(self):
        connection = _FakeSqlAlchemyConnection()
        postgres_support._set_local_search_path(connection, 'monitoring', 'postgres_native')
        self.assertEqual(connection.commands, ['SET LOCAL search_path TO "monitoring", public'])

    def test_set_local_search_path_uses_set_for_motherduck(self):
        connection = _FakeSqlAlchemyConnection()
        postgres_support._set_local_search_path(connection, 'monitoring', 'motherduck_pgwire')
        self.assertEqual(connection.commands, ["SET search_path = 'monitoring,public'"])

    def test_set_search_path_closes_cursor_postgres_native(self):
        dbapi_connection = _FakeDbapiConnection()
        postgres_support._set_search_path(dbapi_connection, None, 'activities', 'postgres_native')
        self.assertEqual(dbapi_connection.cursor_value.commands, ['SET search_path TO "activities", public'])
        self.assertTrue(dbapi_connection.cursor_value.closed)

    def test_set_search_path_closes_cursor_motherduck(self):
        dbapi_connection = _FakeDbapiConnection()
        postgres_support._set_search_path(dbapi_connection, None, 'activities', 'motherduck_pgwire')
        self.assertEqual(dbapi_connection.cursor_value.commands, ["SET search_path = 'activities,public'"])
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

    def test_prepare_postgres_engine_skips_functions_for_motherduck(self):
        connection = _FakePreparedConnection()
        engine = _FakeEngine(connection)
        with patch.object(postgres_support, '_install_search_path_event') as search_path_mock, \
                patch.object(postgres_support, '_install_postgres_functions') as install_functions_mock:
            postgres_support._prepare_postgres_engine(engine, 'garmin', 'motherduck_pgwire')
        search_path_mock.assert_called_once_with(engine, 'garmin', 'motherduck_pgwire')
        install_functions_mock.assert_not_called()
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "garmin"', connection.executed[0])
        self.assertIn("SET search_path = 'garmin,public'", connection.executed[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
