"""Unit tests for summary base selectable generation."""

__author__ = "Tom Goetz"
__copyright__ = "Copyright Tom Goetz"
__license__ = "GPL"

import unittest

from sqlalchemy import column, select

from garmindb.summarydb.summary_base import SummaryBase


class _SummaryForSelectableTest(SummaryBase):
    __tablename__ = 'summary_test'
    time_col = column('first_day')


class TestSummaryBase(unittest.TestCase):
    """Tests for SummaryBase view-selectable helpers."""

    def test_weeks_months_years_selectable_has_single_sweat_loss_avg_alias(self):
        selectable = _SummaryForSelectableTest._SummaryBase__create_weeks_months_years_selectable(days_count=365)
        query_sql = str(select(*selectable))

        self.assertEqual(query_sql.count('AS sweat_loss_avg'), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
