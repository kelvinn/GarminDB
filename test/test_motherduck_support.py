"""Unit tests for direct MotherDuck support helpers."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import idbutils
from idbutils import DbParams

from garmindb import motherduck_support
from garmindb.garmindb import GarminDb, Attributes
from garmindb.fitbitdb import FitBitDb


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

    def execute(self, statement):
        self.commands.append(str(statement))


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


class TestMotherDuckSupport(unittest.TestCase):
    """Tests for direct MotherDuck support."""

    def test_motherduck_url_uses_named_database(self):
        db_params = SimpleNamespace(db_name='health')
        with patch.object(motherduck_support, '_ensure_duckdb_driver'):
            self.assertEqual(motherduck_support._motherduck_url(GarminDb, db_params), 'duckdb:///md:health')

    def test_motherduck_connect_args_use_config_token(self):
        db_params = SimpleNamespace(motherduck_token='secret')
        self.assertEqual(motherduck_support._motherduck_connect_args(db_params), {'config': {'motherduck_token': 'secret'}})

    def test_motherduck_schema_allows_garmin_workflow_dbs(self):
        db_params = SimpleNamespace(test_db=False)
        self.assertEqual(motherduck_support._motherduck_schema(GarminDb, db_params), 'garmin')

    def test_motherduck_schema_prefixes_test_dbs(self):
        db_params = SimpleNamespace(test_db=True)
        self.assertEqual(motherduck_support._motherduck_schema(GarminDb, db_params), 'test_garmin')

    def test_motherduck_schema_rejects_non_garmin_workflow_dbs(self):
        db_params = SimpleNamespace(test_db=False)
        with self.assertRaises(motherduck_support.MotherDuckSupportException):
            motherduck_support._motherduck_schema(FitBitDb, db_params)

    def test_install_search_path_event_registers_connect_and_begin(self):
        engine = object()
        with patch.object(motherduck_support.event, 'listen') as listen_mock:
            motherduck_support._install_search_path_event(engine, 'garmin')

        self.assertEqual(listen_mock.call_count, 3)
        calls = {call.args[1]: call.args[2] for call in listen_mock.call_args_list}
        self.assertIn('connect', calls)
        self.assertIn('checkout', calls)
        self.assertIn('begin', calls)

        dbapi_connection = _FakeDbapiConnection()
        calls['connect'](dbapi_connection, None)
        self.assertEqual(dbapi_connection.cursor_value.commands, ["SET search_path = 'garmin'"])
        self.assertTrue(dbapi_connection.cursor_value.closed)

        dbapi_connection = _FakeDbapiConnection()
        calls['checkout'](dbapi_connection, None, None)
        self.assertEqual(dbapi_connection.cursor_value.commands, ["SET search_path = 'garmin'"])
        self.assertTrue(dbapi_connection.cursor_value.closed)

        connection = _FakeSqlAlchemyConnection()
        calls['begin'](connection)
        self.assertEqual(connection.commands, ["SET search_path = 'garmin'"])

    def test_prepare_motherduck_engine_creates_schema_before_events(self):
        connection = _FakeSqlAlchemyConnection()
        engine = _FakeEngine(connection)
        with patch.object(motherduck_support, '_install_search_path_event') as search_path_mock:
            motherduck_support._prepare_motherduck_engine(engine, 'garmin')
        self.assertIn('CREATE SCHEMA IF NOT EXISTS "garmin"', connection.commands[0])
        self.assertIn("SET search_path = 'garmin'", connection.commands[1])
        search_path_mock.assert_called_once_with(engine, 'garmin')

    def test_local_duckdb_smoke_initializes_garmin_schema(self):
        try:
            import duckdb  # noqa: F401
            import duckdb_engine  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest('duckdb and duckdb-engine are not installed')

        original_url = idbutils.DB._motherduck_url
        original_connect_args = motherduck_support._motherduck_connect_args
        with tempfile.TemporaryDirectory() as temp_dir:
            db_file = Path(temp_dir) / 'motherduck_smoke.duckdb'
            idbutils.DB._motherduck_url = classmethod(lambda cls, db_params: 'duckdb:///' + str(db_file))
            motherduck_support._motherduck_connect_args = lambda db_params: {}
            try:
                db_params = DbParams(db_type='motherduck', db_name='health', motherduck_token='dummy', test_db=True)
                db = GarminDb(db_params)
                self.assertEqual(db.schema_name, 'test_garmin')
                Attributes.set(db, 'motherduck_smoke', 'ok')
                self.assertEqual(Attributes.get_string(db, 'motherduck_smoke'), 'ok')
            finally:
                idbutils.DB._motherduck_url = original_url
                motherduck_support._motherduck_connect_args = original_connect_args
                if 'db' in locals():
                    db.engine.dispose()


if __name__ == '__main__':
    unittest.main(verbosity=2)
