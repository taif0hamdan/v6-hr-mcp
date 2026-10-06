"""
hr_backend.authorization / visibility tests: exact vs subtree grants, the
global admin role, and the explicit guarantee that visibility (hidden-unit
overrides) never changes what a grant authorizes.
"""

import unittest

from hr_backend_test_support import isolated_hr_backend_env
from hr_backend.authorization import (
    expand_subtree,
    get_authorized_divn_ids,
    is_authorized_for_unit,
)
from hr_backend.db import session_scope
from hr_backend.models import UnitAdminGrant, User
from hr_backend.normalize import build_division_index
from hr_backend.visibility import compute_visible_tree, get_hidden_divn_ids, set_visibility

DIVISIONS = [
    {"DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "root", "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT"},
    {"DIVN_ID": "2", "CTRL_DIVN_ID": "1", "DIVN_NAME_ARB": "child", "DIVN_NAME_ENG": "Child", "DIVN_TYPE": "UNIT"},
    {"DIVN_ID": "3", "CTRL_DIVN_ID": "2", "DIVN_NAME_ARB": "grandchild", "DIVN_NAME_ENG": "Grandchild", "DIVN_TYPE": "SECTION"},
    {"DIVN_ID": "9", "CTRL_DIVN_ID": "1", "DIVN_NAME_ARB": "sibling", "DIVN_NAME_ENG": "Sibling", "DIVN_TYPE": "UNIT"},
]


class SubtreeExpansionTests(unittest.TestCase):
    def test_includes_root_and_all_descendants_only(self):
        index = build_division_index(DIVISIONS)
        self.assertEqual(expand_subtree("2", index), {"2", "3"})

    def test_leaf_expands_to_itself(self):
        index = build_division_index(DIVISIONS)
        self.assertEqual(expand_subtree("3", index), {"3"})


class AuthorizationGrantTests(unittest.TestCase):
    def test_admin_role_gets_all(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                admin = User(ldap_username="admin1", role="admin")
                s.add(admin)
                s.flush()
                authorized = get_authorized_divn_ids(s, admin, build_division_index(DIVISIONS))
            self.assertEqual(authorized, "all")

    def test_exact_grant_does_not_include_descendants(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                user = User(ldap_username="u1", role="unit_admin")
                s.add(user)
                s.flush()
                s.add(UnitAdminGrant(user_id=user.id, divn_id="2", scope="exact"))
                s.flush()
                authorized = get_authorized_divn_ids(s, user, build_division_index(DIVISIONS))
            self.assertEqual(authorized, {"2"})
            self.assertFalse(is_authorized_for_unit(authorized, "3"))

    def test_subtree_grant_does_not_broaden_to_organization_wide(self):
        """A subtree grant on a child unit must never be treated as access
        to its parent or siblings - no silent broadening to org-wide."""
        with isolated_hr_backend_env():
            with session_scope() as s:
                user = User(ldap_username="u2", role="unit_admin")
                s.add(user)
                s.flush()
                s.add(UnitAdminGrant(user_id=user.id, divn_id="2", scope="subtree"))
                s.flush()
                authorized = get_authorized_divn_ids(s, user, build_division_index(DIVISIONS))
            self.assertEqual(authorized, {"2", "3"})
            self.assertFalse(is_authorized_for_unit(authorized, "1"))  # parent - not authorized
            self.assertFalse(is_authorized_for_unit(authorized, "9"))  # sibling - not authorized
            self.assertTrue(is_authorized_for_unit(authorized, "3"))  # own descendant - authorized

    def test_no_grants_means_no_access(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                user = User(ldap_username="u3", role="unit_admin")
                s.add(user)
                s.flush()
                authorized = get_authorized_divn_ids(s, user, build_division_index(DIVISIONS))
            self.assertEqual(authorized, set())


class VisibilityNeverGatesAuthorizationTests(unittest.TestCase):
    def test_hiding_a_unit_does_not_revoke_its_grant(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                user = User(ldap_username="u4", role="unit_admin")
                s.add(user)
                s.flush()
                s.add(UnitAdminGrant(user_id=user.id, divn_id="2", scope="subtree"))
                s.flush()
                set_visibility(s, "2", True, set_by=None)
                authorized = get_authorized_divn_ids(s, user, build_division_index(DIVISIONS))
            self.assertTrue(is_authorized_for_unit(authorized, "3"))

    def test_hidden_unit_and_descendants_excluded_from_visible_tree(self):
        with isolated_hr_backend_env():
            index = build_division_index(DIVISIONS)
            with session_scope() as s:
                set_visibility(s, "2", True, set_by=None)
                hidden = get_hidden_divn_ids(s)
            visible = compute_visible_tree(index, hidden)
            self.assertEqual(set(visible.keys()), {"1", "9"})


if __name__ == "__main__":
    unittest.main()
