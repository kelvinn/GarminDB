"""Unit tests for monitoring FIT file discovery."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import fitfile

from garmindb import GarminMonitoringFitData


class _FakeProcessor:

    def __init__(self):
        self.written_files = []

    def write_file(self, fit_file):
        self.written_files.append(fit_file.filename)


class TestMonitoringFitData(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.monitoring_dir = os.path.join(self.temp_dir.name, '2024')
        os.makedirs(self.monitoring_dir)
        for file_name in (
                '100_WELLNESS.fit',
                '101_HRV_STATUS.fit',
                '102_SLEEP_DATA.fit',
                '103_METRICS.fit',
                '104_ACTIVITY.fit',
                'daily_summary_2024-01-01.json'):
            with open(os.path.join(self.monitoring_dir, file_name), 'wb') as file:
                file.write(b'not a real fit file')

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_monitoring_import_queues_only_monitoring_fit_names(self):
        monitoring_data = GarminMonitoringFitData(
            self.temp_dir.name,
            latest=False,
            measurement_system=fitfile.field_enums.DisplayMeasure.metric,
            debug=0
        )
        self.assertEqual(
            sorted(os.path.basename(file_name) for file_name in monitoring_data.file_names),
            ['100_WELLNESS.fit', '101_HRV_STATUS.fit']
        )

    def test_non_monitoring_fit_names_are_not_parsed(self):
        monitoring_data = GarminMonitoringFitData(
            self.temp_dir.name,
            latest=False,
            measurement_system=fitfile.field_enums.DisplayMeasure.metric,
            debug=0
        )
        processor = _FakeProcessor()

        def fake_fit_file(file_name, measurement_system):
            return SimpleNamespace(filename=file_name, type=fitfile.FileType.monitoring_b)

        with patch('fitfile.file.File', side_effect=fake_fit_file) as fit_file_mock:
            monitoring_data.process_files(processor)

        parsed_file_names = [os.path.basename(call.args[0]) for call in fit_file_mock.call_args_list]
        self.assertEqual(parsed_file_names, ['100_WELLNESS.fit', '101_HRV_STATUS.fit'])
        self.assertEqual([os.path.basename(file_name) for file_name in processor.written_files], parsed_file_names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
