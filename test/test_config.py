"""Test config handling."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import os
import unittest
import logging
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from garmindb import GarminConnectConfigManager
from garmindb.garmin_connect_config_manager import ConfigException


root_logger = logging.getLogger()
handler = logging.FileHandler('copy.log', 'w')
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)


class TestConfig(unittest.TestCase):
    """Class for testing config handling."""

    @classmethod
    def setUpClass(cls):
        cls.gc_config = GarminConnectConfigManager()
        cls.homedir = os.path.expanduser('~')

    def test_directories(self):
        # config_dir
        expected_config_dir = self.homedir + os.sep + '.GarminDb'
        config_dir = self.gc_config.config_dir
        self.assertEqual(config_dir, expected_config_dir, f'actual {config_dir} expected {expected_config_dir}')
        # base_dir
        expected_base_dir = self.homedir + os.sep + 'HealthData'
        base_dir = self.gc_config.get_base_dir()
        self.assertEqual(base_dir, expected_base_dir, f'actual {base_dir} expected {expected_base_dir}')
        # monitoring_dir
        year = 2023
        expected_monitoring_dir = expected_base_dir + os.sep + "FitFiles" + os.sep + 'Monitoring' + os.sep + str(year)
        monitoring_dir = self.gc_config.get_monitoring_dir(year)
        self.assertEqual(monitoring_dir, expected_monitoring_dir, f'actual {monitoring_dir} expected {expected_monitoring_dir}')

    def test_db(self):
        expect_db_type = (self.gc_config.get_db_type() or 'sqlite').lower()
        if expect_db_type == 'postgresql':
            expect_db_type = 'postgres'
        db_params = self.gc_config.get_db_params()
        self.assertEqual(db_params.db_type, expect_db_type, f"expected {expect_db_type} actual {db_params.db_type}")
        if db_params.db_type == 'sqlite':
            expected_db_path = self.homedir + os.sep + 'HealthData' + os.sep + 'DBs'
            self.assertEqual(db_params.db_path, expected_db_path, f"expected {expected_db_path} actual {db_params.db_path}")
        elif db_params.db_type == 'postgres':
            self.assertIsNotNone(db_params.db_name, 'Postgres requires db_name')
            self.assertFalse(hasattr(db_params, 'database_url'), 'Postgres config should use db_* connection fields')

    def config_for_db(self, db_config):
        temp_dir = tempfile.TemporaryDirectory()
        config = {
            'db' : db_config,
            'directories' : {
                'relative_to_home' : False,
                'base_dir' : str(Path(temp_dir.name) / 'HealthData')
            }
        }
        config_path = Path(temp_dir.name) / 'GarminConnectConfig.json'
        with open(config_path, 'w') as file:
            json.dump(config, file)
        return temp_dir, GarminConnectConfigManager(temp_dir.name)

    def test_sqlite_db_params_without_database_url(self):
        temp_dir, gc_config = self.config_for_db({'type' : 'sqlite'})
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.db_type, 'sqlite')
            self.assertFalse(hasattr(db_params, 'database_url'))
            self.assertEqual(db_params.db_path, str(Path(temp_dir.name) / 'HealthData' / 'DBs'))

    def test_postgres_db_params_from_config_fields(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_username' : 'garmin',
            'db_password' : 'secret',
            'db_host' : 'db.local',
            'db_port' : '5433',
            'db_name' : 'garmindb'
        })
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.db_type, 'postgres')
            self.assertEqual(db_params.db_username, 'garmin')
            self.assertEqual(db_params.db_password, 'secret')
            self.assertEqual(db_params.db_host, 'db.local')
            self.assertEqual(db_params.db_port, 5433)
            self.assertEqual(db_params.db_name, 'garmindb')
            self.assertFalse(hasattr(db_params, 'database_url'))

    def test_postgres_legacy_database_url_fallback(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'database_url' : 'postgresql://garmin:secret@db.local:5433/garmindb'
        })
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.db_type, 'postgres')
            self.assertEqual(db_params.db_username, 'garmin')
            self.assertEqual(db_params.db_password, 'secret')
            self.assertEqual(db_params.db_host, 'db.local')
            self.assertEqual(db_params.db_port, 5433)
            self.assertEqual(db_params.db_name, 'garmindb')
            self.assertFalse(hasattr(db_params, 'database_url'))

    def test_postgres_runtime_defaults(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_name' : 'garmindb'
        })
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.postgres_connect_timeout_sec, 10)
            self.assertEqual(db_params.postgres_statement_timeout_ms, 0)

    def test_postgres_runtime_overrides(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_name' : 'garmindb',
            'postgres_connect_timeout_sec' : '15',
            'postgres_statement_timeout_ms' : '25000'
        })
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.postgres_connect_timeout_sec, 15)
            self.assertEqual(db_params.postgres_statement_timeout_ms, 25000)

    def test_postgres_config_fields_override_legacy_database_url(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'database_url' : 'postgresql://legacy:legacy@legacy.local:5432/legacy',
            'db_username' : 'garmin',
            'db_password' : 'secret',
            'db_host' : 'db.local',
            'db_port' : 5433,
            'db_name' : 'garmindb'
        })
        with temp_dir:
            db_params = gc_config.get_db_params()
            self.assertEqual(db_params.db_username, 'garmin')
            self.assertEqual(db_params.db_password, 'secret')
            self.assertEqual(db_params.db_host, 'db.local')
            self.assertEqual(db_params.db_port, 5433)
            self.assertEqual(db_params.db_name, 'garmindb')

    def test_postgres_rejects_invalid_db_port(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_port' : 'not-a-port',
            'db_name' : 'garmindb'
        })
        with temp_dir:
            with patch.dict(os.environ, {'DATABASE_URL' : ''}):
                with self.assertRaises(ConfigException):
                    gc_config.get_db_params()

    def test_postgres_requires_db_name(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_username' : 'garmin'
        })
        with temp_dir:
            with self.assertRaises(ConfigException):
                gc_config.get_db_params()

    def test_postgres_rejects_invalid_runtime_timeout_values(self):
        temp_dir, gc_config = self.config_for_db({
            'type' : 'postgres',
            'db_name' : 'garmindb',
            'postgres_connect_timeout_sec' : 0,
            'postgres_statement_timeout_ms' : -1
        })
        with temp_dir:
            with self.assertRaises(ConfigException):
                gc_config.get_db_params()

if __name__ == '__main__':
    unittest.main(verbosity=2)
