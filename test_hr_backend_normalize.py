"""
hr_backend.normalize tests: field mapping off the HR provider's verified
contract, ancestor-chain walking (ancestry stays intact through an
excluded-type node), OTHERS/MILITARY exclusion, leading-zero id
preservation, and orphan handling (a dangling parent_id never raises).
"""

import unittest

from hr_backend.normalize import (
    build_division_index,
    classify_membership,
    is_excluded_division_type,
    normalize_division,
    normalize_employee,
    normalize_id,
    resolve_ancestor_chain,
)


class NormalizeIdTests(unittest.TestCase):
    def test_preserves_leading_zeros(self):
        self.assertEqual(normalize_id("010"), "010")
        self.assertEqual(normalize_id(10), "10")

    def test_blank_and_none_become_none(self):
        self.assertIsNone(normalize_id(None))
        self.assertIsNone(normalize_id("   "))


class ExcludedDivisionTypeTests(unittest.TestCase):
    def test_trimmed_case_insensitive_match(self):
        for value in ("OTHERS", " others ", "Others", "MILITARY", "military"):
            self.assertTrue(is_excluded_division_type(value), value)

    def test_dept_unit_section_not_excluded(self):
        for value in ("DEPT", "UNIT", "SECTION", None, ""):
            self.assertFalse(is_excluded_division_type(value), value)


class FieldMappingTests(unittest.TestCase):
    def test_employee_field_map(self):
        emp = normalize_employee({
            "MIL_ID": "10021", "PF_NO": 21, "FULL_NAME_ARB": "محمد",
            "LATIN_NAME": "Mohammed", "RANK_ID": "2", "DIVN_ID": "1", "UNIT_ID": "3",
        })
        self.assertEqual(emp.employee_id, "10021")
        self.assertEqual(emp.full_name, "Mohammed")
        self.assertEqual(emp.full_name_ar, "محمد")

    def test_full_name_fallback_chain(self):
        emp = normalize_employee({"MIL_ID": "5", "FULL_NAME_ARB": "فقط عربي", "LATIN_NAME": None})
        self.assertEqual(emp.full_name, "فقط عربي")

        emp2 = normalize_employee({"MIL_ID": "5", "FULL_NAME_ARB": None, "LATIN_NAME": None})
        self.assertEqual(emp2.full_name, "5")  # final fallback: employee_id itself

    def test_missing_mil_id_raises(self):
        with self.assertRaises(ValueError):
            normalize_employee({"PF_NO": 1, "FULL_NAME_ARB": "x"})

    def test_division_field_map(self):
        div = normalize_division({
            "DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "ا",
            "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT",
        })
        self.assertEqual(div.id, "1")
        self.assertIsNone(div.parent_id)
        self.assertEqual(div.divn_type, "DEPT")


class AncestorChainTests(unittest.TestCase):
    def setUp(self):
        self.index = build_division_index([
            {"DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "root", "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT"},
            {"DIVN_ID": "2", "CTRL_DIVN_ID": "1", "DIVN_NAME_ARB": "section", "DIVN_NAME_ENG": "Section", "DIVN_TYPE": "SECTION"},
            {"DIVN_ID": "99", "CTRL_DIVN_ID": "2", "DIVN_NAME_ARB": "others", "DIVN_NAME_ENG": "Others", "DIVN_TYPE": "OTHERS"},
        ])

    def test_chain_walks_through_excluded_type_node(self):
        """An OTHERS/MILITARY node in the middle of the chain must not break ancestry for its descendants."""
        chain = resolve_ancestor_chain("99", self.index)
        self.assertEqual([n.id for n in chain], ["99", "2", "1"])

    def test_orphan_parent_stops_without_raising(self):
        chain = resolve_ancestor_chain("999", self.index)
        self.assertEqual(chain, [])

    def test_none_divn_id_returns_empty_chain(self):
        self.assertEqual(resolve_ancestor_chain(None, self.index), [])

    def test_cycle_safe(self):
        cyclic_index = build_division_index([
            {"DIVN_ID": "a", "CTRL_DIVN_ID": "b", "DIVN_NAME_ARB": "", "DIVN_NAME_ENG": "", "DIVN_TYPE": "UNIT"},
            {"DIVN_ID": "b", "CTRL_DIVN_ID": "a", "DIVN_NAME_ARB": "", "DIVN_NAME_ENG": "", "DIVN_TYPE": "UNIT"},
        ])
        chain = resolve_ancestor_chain("a", cyclic_index)
        self.assertEqual(len(chain), 2)  # terminates instead of looping forever


class ClassifyMembershipTests(unittest.TestCase):
    def test_classifies_org_dept_section_subsection(self):
        index = build_division_index([
            {"DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "root", "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT"},
            {"DIVN_ID": "2", "CTRL_DIVN_ID": "1", "DIVN_NAME_ARB": "sec", "DIVN_NAME_ENG": "Sec", "DIVN_TYPE": "SECTION"},
        ])
        emp = classify_membership(
            normalize_employee({"MIL_ID": "10021", "DIVN_ID": "2", "UNIT_ID": "7"}),
            index,
        )
        self.assertEqual(emp.organization_id, "1")
        self.assertEqual(emp.department_id, "1")
        self.assertEqual(emp.section_id, "2")
        self.assertEqual(emp.subsection_id, "7")  # UNIT_ID, the finest level this provider exposes directly

    def test_unresolvable_division_leaves_levels_none_not_raising(self):
        emp = classify_membership(normalize_employee({"MIL_ID": "1", "DIVN_ID": "nonexistent"}), {})
        self.assertIsNone(emp.organization_id)
        self.assertIsNone(emp.department_id)


if __name__ == "__main__":
    unittest.main()
