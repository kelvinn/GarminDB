"""Unit tests for postgres support helpers."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import unittest
from unittest.mock import patch

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


class TestPostgresSupport(unittest.TestCase):
    """Tests for postgres search_path setup."""

    def test_install_search_path_event_registers_connect_and_begin(self):
        engine = object()
        with patch.object(postgres_support.event, 'listen') as listen_mock:
            postgres_support._install_search_path_event(engine, 'garmin')

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

    def test_set_local_search_path_uses_local_setting(self):
        connection = _FakeSqlAlchemyConnection()
        postgres_support._set_local_search_path(connection, 'monitoring')
        self.assertEqual(connection.commands, ['SET LOCAL search_path TO "monitoring", public'])

    def test_set_search_path_closes_cursor(self):
        dbapi_connection = _FakeDbapiConnection()
        postgres_support._set_search_path(dbapi_connection, None, 'activities')
        self.assertEqual(dbapi_connection.cursor_value.commands, ['SET search_path TO "activities", public'])
        self.assertTrue(dbapi_connection.cursor_value.closed)

    def test_set_local_search_path_validates_schema_identifier(self):
        connection = _FakeSqlAlchemyConnection()
        with self.assertRaises(postgres_support.PostgresSupportException):
            postgres_support._set_local_search_path(connection, 'bad-schema')


if __name__ == '__main__':
    unittest.main(verbosity=2)
