"""
hr_backend.ordering tests: rank_id then employee_id, numeric ascending -
"do not sort numeric IDs lexicographically" (2 before 10, not "10" before
"2"), missing/non-numeric values sort after all numeric ones.
"""

import unittest

from hr_backend.normalize import NormalizedEmployee
from hr_backend.ordering import sort_employees


def _emp(employee_id, rank_id):
    return NormalizedEmployee(
        employee_id=employee_id, pf_no=None, full_name_ar=None, full_name=None,
        rank_id=rank_id, divn_id=None, unit_id=None,
    )


class OrderingTests(unittest.TestCase):
    def test_numeric_not_lexicographic(self):
        emps = [_emp("1", "10"), _emp("2", "2"), _emp("3", "9")]
        ordered = sort_employees(emps)
        self.assertEqual([e.rank_id for e in ordered], ["2", "9", "10"])

    def test_ties_broken_by_employee_id_numeric(self):
        emps = [_emp("10", "5"), _emp("2", "5"), _emp("9", "5")]
        ordered = sort_employees(emps)
        self.assertEqual([e.employee_id for e in ordered], ["2", "9", "10"])

    def test_missing_rank_sorts_last(self):
        emps = [_emp("1", None), _emp("2", "1")]
        ordered = sort_employees(emps)
        self.assertEqual([e.employee_id for e in ordered], ["2", "1"])

    def test_non_numeric_rank_sorts_last_not_lexicographically(self):
        emps = [_emp("1", "abc"), _emp("2", "3")]
        ordered = sort_employees(emps)
        self.assertEqual([e.employee_id for e in ordered], ["2", "1"])


if __name__ == "__main__":
    unittest.main()
