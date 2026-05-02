"""Class for importing monitoring FIT files into a database."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"


import sys
import logging
import random
import time
import traceback
from tqdm import tqdm

import fitfile
from idbutils import FileProcessor
from sqlalchemy.exc import DBAPIError, OperationalError


logger = logging.getLogger(__file__)
logger.addHandler(logging.StreamHandler(stream=sys.stdout))
root_logger = logging.getLogger()

TRANSIENT_DB_ERROR_TEXT = (
    'operation timed out',
    'ssl syscall',
    'could not receive data from server',
    'server closed the connection unexpectedly',
    'connection reset by peer',
    'connection refused',
    'connection to server',
    'could not connect to server',
)


class FitData():
    """Class for importing FIT files into a database."""

    def __init__(self, input_dir, debug, latest=False, recursive=False, fit_types=None, measurement_system=fitfile.field_enums.DisplayMeasure.metric,
                 file_filter=None):
        """
        Return an instance of FitData.

        Parameters:
        input_dir (string): directory (full path) to check for monitoring data files
        debug (Boolean): enable debug logging
        latest (Boolean): check for latest files only
        fit_types (Fit.field_enums.FileType): check for this file type only
        measurement_system (enum): which measurement system to use when importing the files

        """
        logger.info("Processing %s FIT data from %s", fit_types, input_dir)
        self.measurement_system = measurement_system
        self.debug = debug
        self.fit_types = fit_types
        self.file_names = FileProcessor.dir_to_files(input_dir, fitfile.file.name_regex, latest, recursive)
        if file_filter is not None:
            self.file_names = [file_name for file_name in self.file_names if file_filter(file_name)]

    def file_count(self):
        """Return the number of files that will be processed."""
        return len(self.file_names)

    @staticmethod
    def _sqlstate(error):
        orig = getattr(error, 'orig', None)
        if orig is None:
            return None
        sqlstate = getattr(orig, 'sqlstate', None)
        if sqlstate is None:
            sqlstate = getattr(orig, 'pgcode', None)
        if sqlstate is None:
            diag = getattr(orig, 'diag', None)
            if diag is not None:
                sqlstate = getattr(diag, 'sqlstate', None)
        return sqlstate

    @classmethod
    def _is_retryable_postgres_error(cls, error):
        if isinstance(error, DBAPIError) and getattr(error, 'connection_invalidated', False):
            return True
        if not isinstance(error, OperationalError):
            return False
        sqlstate = cls._sqlstate(error)
        if sqlstate and str(sqlstate).startswith('08'):
            return True
        error_text = str(error).lower()
        return any(text in error_text for text in TRANSIENT_DB_ERROR_TEXT)

    @staticmethod
    def _retry_policy_for_processor(fit_file_processor):
        db_params = getattr(fit_file_processor, 'db_params', None)
        if getattr(db_params, 'db_type', None) != 'postgres':
            return (1, 0.0, 0.0)
        retry_attempts = int(getattr(db_params, 'postgres_retry_attempts', 3))
        retry_base_backoff = float(getattr(db_params, 'postgres_retry_base_backoff_sec', 0.5))
        retry_max_backoff = float(getattr(db_params, 'postgres_retry_max_backoff_sec', 4.0))
        return (max(1, retry_attempts), max(0.0, retry_base_backoff), max(0.0, retry_max_backoff))

    def _write_file_with_retry(self, fit_file_processor, fit_file):
        retry_attempts, retry_base_backoff, retry_max_backoff = self._retry_policy_for_processor(fit_file_processor)
        for attempt in range(1, retry_attempts + 1):
            try:
                fit_file_processor.write_file(fit_file)
                return
            except Exception as error:
                if attempt == retry_attempts or not self._is_retryable_postgres_error(error):
                    raise
                capped_backoff = min(retry_max_backoff, retry_base_backoff * (2 ** (attempt - 1)))
                delay = random.uniform(0.0, capped_backoff)
                logger.warning(
                    "Retrying %s after transient Postgres error (attempt %d/%d, sleep %.2fs): %s",
                    fit_file.filename,
                    attempt + 1,
                    retry_attempts,
                    delay,
                    error,
                )
                time.sleep(delay)

    def process_files(self, fit_file_processor):
        """Import FIT files into the database."""
        skipped = 0
        for file_name in tqdm(self.file_names, unit='files'):
            try:
                fit_file = fitfile.file.File(file_name, self.measurement_system)
                if self.fit_types is None or fit_file.type in self.fit_types:
                    self._write_file_with_retry(fit_file_processor, fit_file)
                    root_logger.debug("Wrote %s to the database", fit_file)
                else:
                    skipped += 1
                    root_logger.debug("skipping non-matching %s", fit_file)
            except Exception as e:
                logger.error("Failed to parse %s: %s", file_name, e)
                root_logger.error("Failed to parse %s: %s - %s", file_name, e, traceback.format_exc())
        if skipped:
            root_logger.info("Skipped %d non-matching FIT files", skipped)
