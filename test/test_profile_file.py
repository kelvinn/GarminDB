"""Test profile file parsing."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import unittest
import logging

import fitfile

from garmindb import GarminConnectConfigManager, GarminUserSettings, GarminPersonalInformation, GarminSocialProfile
from garmindb.garmindb import GarminDb, Attributes

from test_db_base import TestDBBase


root_logger = logging.getLogger()
handler = logging.FileHandler('profile_file.log', 'w')
root_logger.addHandler(handler)
root_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)


class TestProfileFile(unittest.TestCase):
    """Class for testing profile JSON file parsing."""

    @classmethod
    def setUpClass(cls):
        cls.gc_config = GarminConnectConfigManager()
        cls.file_path = cls.gc_config.get_fit_files_dir()
        cls.expected_db = GarminDb(cls.gc_config.get_db_params())

    @classmethod
    def tearDownClass(cls):
        TestDBBase.dispose_dbs(cls)

    def process_or_skip(self, profile_data):
        if profile_data.file_count() == 0:
            self.skipTest(f'No {profile_data.__class__.__name__} files found in {self.file_path}')
        profile_data.process()

    def test_parse_usersettings(self):
        db_params = self.gc_config.get_db_params(test_db=True)
        gus = GarminUserSettings(db_params, self.file_path, debug=2)
        try:
            self.process_or_skip(gus)
            gdb = GarminDb(db_params)
            try:
                measurement_system = Attributes.measurements_type(gdb)
                expected = Attributes.measurements_type(self.expected_db)
                self.assertEqual(measurement_system, expected,
                                 'DisplayMeasure expected %r found %r from %r' % (expected, measurement_system, gus.file_names))
            finally:
                TestDBBase.dispose_dbs(gdb)
        finally:
            TestDBBase.dispose_dbs(gus)

    def test_parse_personalinfo(self):
        db_params = self.gc_config.get_db_params(test_db=True)
        gpi = GarminPersonalInformation(db_params, self.file_path, debug=2)
        try:
            self.process_or_skip(gpi)
            gdb = GarminDb(db_params)
            try:
                locale = Attributes.get_string(gdb, 'locale')
                expected = Attributes.get_string(self.expected_db, 'locale')
                self.assertEqual(locale, expected, 'locale expected %r found %r from %r' % (expected, locale, gpi.file_names))
            finally:
                TestDBBase.dispose_dbs(gdb)
        finally:
            TestDBBase.dispose_dbs(gpi)

    def test_parse_socialprofile(self):
        db_params = self.gc_config.get_db_params(test_db=True)
        gsp = GarminSocialProfile(db_params, self.file_path, debug=2)
        try:
            self.process_or_skip(gsp)
            gdb = GarminDb(db_params)
            try:
                id = Attributes.get_string(gdb, 'id')
                expected = Attributes.get_string(self.expected_db, 'id')
                self.assertEqual(id, expected, 'Id expected %r found %r from %r' % (expected, id, gsp.file_names))
            finally:
                TestDBBase.dispose_dbs(gdb)
        finally:
            TestDBBase.dispose_dbs(gsp)


if __name__ == '__main__':
    unittest.main(verbosity=2)
