"""A building block for other tests."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import logging
import cProfile
import pstats


logger = logging.getLogger(__name__)


class TestFitFileProcessorMixin():

    dispose_db_attributes = ()

    def write_file(self, fit_file):
        TestDBBase.dispose_dbs(*(getattr(self, attribute, None) for attribute in self.dispose_db_attributes))
        super().write_file(fit_file)


class TestDBBase():

    @classmethod
    def safe_repr(cls, value):
        if hasattr(value, '__dict__'):
            values = vars(value).copy()
            for key in values:
                if 'password' in key or key == 'database_url':
                    values[key] = '***'
            return f'<{value.__class__.__name__}() {repr(values)}'
        return repr(value)

    @classmethod
    def setUpClass(cls, db, table_dict, table_not_none_cols_dict={}, table_can_be_empty=[]):
        cls.db = db
        cls.table_dict = table_dict
        cls.table_not_none_cols_dict = table_not_none_cols_dict
        cls.table_can_be_empty = table_can_be_empty

    def profile_function(self, output_file_prefix, func, *args):
        pr = cProfile.Profile()
        pr.runcall(func, *args)
        with open(output_file_prefix + '_cum.txt', 'w') as output_file:
            pstats.Stats(pr, stream=output_file).sort_stats('cumulative').print_stats()
        with open(output_file_prefix + '_tot.txt', 'w') as output_file:
            pstats.Stats(pr, stream=output_file).sort_stats('tottime').print_stats()

    @classmethod
    def dispose_dbs(cls, *objects):
        seen = set()

        def dispose_object(value):
            if value is None or id(value) in seen:
                return
            seen.add(id(value))
            if isinstance(value, dict):
                for item in value.values():
                    dispose_object(item)
                return
            if isinstance(value, (list, tuple, set, frozenset)):
                for item in value:
                    dispose_object(item)
                return
            engine = getattr(value, 'engine', None)
            if engine is not None:
                engine.dispose()
            for attribute in ('db', 'garmin_db', 'garmin_mon_db', 'garmin_act_db', 'garmin_sum_db', 'sum_db',
                              'expected_db', 'test_mon_db', 'test_act_db'):
                dispose_object(getattr(value, attribute, None))

        for object_ in objects:
            dispose_object(object_)

    @classmethod
    def tearDownClass(cls):
        cls.dispose_dbs(cls)

    def check_not_none_cols(self, db, table_not_none_cols_dict):
        for table, not_none_cols_list in table_not_none_cols_dict.items():
            for not_none_col in not_none_cols_list:
                self.assertTrue(table.row_count(db, not_none_col, None) == 0, 'table %s col %s has None values' % (table, not_none_col))

    def check_db_tables_exists(self, db, table_dict, min_rows=1):
        for table_name, table in table_dict.items():
            if table_name not in self.table_can_be_empty:
                logger.info("Checking %s exists", table_name)
                self.assertGreaterEqual(table.row_count(db), min_rows, 'table %s has no data' % table_name)

    def test_db_exists(self):
        logger.info("Checking DB %s exists", self.db.db_name)
        self.assertIsNotNone(self.db, 'DB %s doesnt exist' % self.db.db_name)

    def test_db_tables_exists(self):
        self.check_db_tables_exists(self.db, self.table_dict)

    def test_not_none_cols(self):
        self.check_not_none_cols(self.db, self.table_not_none_cols_dict)

    def check_col_type(self, db, table, col, type):
        for value in table.get_col_distinct(db, col):
            self.assertEqual(str(type(value)), value, f'table {table} col {col} has type {type} mismatch for {value}')
