"""Unit tests for FIT file import retry handling."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from garmindb.fit_data import FitData


class _SqlStateError(Exception):

    def __init__(self, message, sqlstate=None):
        super().__init__(message)
        self.sqlstate = sqlstate


class _FakeProcessor:

    def __init__(self, db_params, write_behavior):
        self.db_params = db_params
        self.write_behavior = write_behavior
        self.write_calls = []

    def write_file(self, fit_file):
        self.write_calls.append(fit_file.filename)
        action = self.write_behavior(fit_file.filename, len(self.write_calls))
        if isinstance(action, Exception):
            raise action
        return action


def _operational_error(message, sqlstate=None):
    return OperationalError('SELECT 1', {}, _SqlStateError(message, sqlstate=sqlstate))


class TestFitDataRetry(unittest.TestCase):

    def _fit_data(self, file_names):
        with patch('idbutils.FileProcessor.dir_to_files', return_value=file_names):
            return FitData('unused', debug=0, latest=False, recursive=False, fit_types=None)

    def test_retries_transient_postgres_failure_then_succeeds(self):
        fit_data = self._fit_data(['activity.fit'])
        processor = _FakeProcessor(
            db_params=SimpleNamespace(
                db_type='postgres',
                postgres_retry_attempts=3,
                postgres_retry_base_backoff_sec=0.5,
                postgres_retry_max_backoff_sec=4.0,
            ),
            write_behavior=lambda _name, attempt: _operational_error('SSL SYSCALL error: Operation timed out')
            if attempt == 1 else None,
        )

        with patch('fitfile.file.File', side_effect=lambda file_name, _measurement: SimpleNamespace(filename=file_name, type='any')), \
                patch('garmindb.fit_data.random.uniform', return_value=0.0), \
                patch('garmindb.fit_data.time.sleep') as sleep_mock:
            fit_data.process_files(processor)

        self.assertEqual(processor.write_calls, ['activity.fit', 'activity.fit'])
        sleep_mock.assert_called_once_with(0.0)

    def test_non_transient_postgres_failure_is_not_retried(self):
        fit_data = self._fit_data(['activity.fit'])
        processor = _FakeProcessor(
            db_params=SimpleNamespace(
                db_type='postgres',
                postgres_retry_attempts=3,
                postgres_retry_base_backoff_sec=0.5,
                postgres_retry_max_backoff_sec=4.0,
            ),
            write_behavior=lambda _name, _attempt: _operational_error('duplicate key value violates unique constraint', sqlstate='23505'),
        )

        with patch('fitfile.file.File', side_effect=lambda file_name, _measurement: SimpleNamespace(filename=file_name, type='any')), \
                patch('garmindb.fit_data.time.sleep') as sleep_mock:
            fit_data.process_files(processor)

        self.assertEqual(processor.write_calls, ['activity.fit'])
        self.assertFalse(sleep_mock.called)

    def test_exhausted_transient_retries_continue_to_next_file(self):
        fit_data = self._fit_data(['bad.fit', 'good.fit'])

        def behavior(file_name, _attempt):
            if file_name == 'bad.fit':
                return _operational_error('could not receive data from server: Operation timed out')
            return None

        processor = _FakeProcessor(
            db_params=SimpleNamespace(
                db_type='postgres',
                postgres_retry_attempts=3,
                postgres_retry_base_backoff_sec=0.25,
                postgres_retry_max_backoff_sec=0.5,
            ),
            write_behavior=behavior,
        )

        with patch('fitfile.file.File', side_effect=lambda file_name, _measurement: SimpleNamespace(filename=file_name, type='any')), \
                patch('garmindb.fit_data.random.uniform', return_value=0.0), \
                patch('garmindb.fit_data.time.sleep') as sleep_mock:
            fit_data.process_files(processor)

        self.assertEqual(processor.write_calls, ['bad.fit', 'bad.fit', 'bad.fit', 'good.fit'])
        self.assertEqual(sleep_mock.call_count, 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
