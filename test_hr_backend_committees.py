"""
End-to-end router tests for committees: unit-scope membership matching
(rejecting an out-of-scope employee), the shared rank/employee_id ordering
on member listings, display eligibility surfaced per-member, and
authorization gating committee creation/mutation.
"""

import unittest

from hr_backend_test_support import HrBackendApiTestCase

DIVISIONS = [
    {"DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "root", "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT"},
    {"DIVN_ID": "9", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "elsewhere", "DIVN_NAME_ENG": "Elsewhere", "DIVN_TYPE": "DEPT"},
]


def _emp(employee_id, rank_id, organization_id="1"):
    return {
        "employee_id": employee_id, "pf_no": employee_id, "full_name_ar": None, "full_name": f"Emp {employee_id}",
        "rank_id": rank_id, "divn_id": organization_id, "unit_id": None,
        "organization_id": organization_id, "department_id": organization_id,
        "section_id": None, "subsection_id": None, "organization_name_ar": None,
    }


class CommitteeAuthorizationTests(HrBackendApiTestCase):
    def test_viewer_cannot_create_committee(self):
        self.login_as("viewer1")
        response = self.client.post("/committees", json={"name_ar": "x", "organization_id": "1"})
        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_committee(self):
        self.login_as("admin1", role="admin")
        response = self.client.post("/committees", json={"name_ar": "لجنة", "organization_id": "1"})
        self.assertEqual(response.status_code, 201)


class CommitteeMembershipTests(HrBackendApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed_hr_cache(
            divisions=DIVISIONS,
            roster=[_emp("10", "10"), _emp("2", "2"), _emp("99", "5", organization_id="9")],
        )
        self.login_as("admin1", role="admin")
        self.committee_id = self.client.post(
            "/committees", json={"name_ar": "لجنة", "organization_id": "1"}
        ).json()["id"]

    def test_add_matching_member_succeeds(self):
        response = self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        self.assertEqual(response.status_code, 201)

    def test_add_out_of_scope_member_rejected(self):
        response = self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "99"})
        self.assertEqual(response.status_code, 400)

    def test_add_unknown_employee_rejected(self):
        response = self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "no-such-id"})
        self.assertEqual(response.status_code, 404)

    def test_duplicate_member_rejected(self):
        self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        response = self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        self.assertEqual(response.status_code, 409)

    def test_member_listing_ordered_numerically_not_lexicographically(self):
        self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "2"})
        response = self.client.get(f"/committees/{self.committee_id}/members")
        ids = [m["employee_id"] for m in response.json()]
        self.assertEqual(ids, ["2", "10"])  # rank 2 before rank 10, numeric not lexicographic

    def test_member_listing_surfaces_eligibility(self):
        self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        response = self.client.get(f"/committees/{self.committee_id}/members")
        self.assertTrue(all(m["eligible"] for m in response.json()))  # no-op eligibility, documented gap

    def test_remove_member(self):
        self.client.post(f"/committees/{self.committee_id}/members", json={"employee_id": "10"})
        response = self.client.delete(f"/committees/{self.committee_id}/members/10")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self.client.get(f"/committees/{self.committee_id}/members").json(), [])


if __name__ == "__main__":
    unittest.main()
