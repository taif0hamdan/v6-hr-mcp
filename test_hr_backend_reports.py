"""
End-to-end router tests for the unit/session report: the eligible-roster
LEFT JOIN never drops an employee who has no Evaluation/Result row at all
(headcount is never derived solely from saved result rows), and the three
separated statuses (completed / absent / not_completed) are reported
correctly and distinctly.
"""

import unittest

from hr_backend_test_support import HrBackendApiTestCase

DIVISIONS = [
    {"DIVN_ID": "1", "CTRL_DIVN_ID": None, "DIVN_NAME_ARB": "root", "DIVN_NAME_ENG": "Root", "DIVN_TYPE": "DEPT"},
]


def _emp(employee_id, rank_id):
    return {
        "employee_id": employee_id, "pf_no": employee_id, "full_name_ar": None, "full_name": f"Emp {employee_id}",
        "rank_id": rank_id, "divn_id": "1", "unit_id": None,
        "organization_id": "1", "department_id": "1", "section_id": None, "subsection_id": None,
        "organization_name_ar": None,
    }


class SessionReportTests(HrBackendApiTestCase):
    def setUp(self):
        super().setUp()
        self.seed_hr_cache(
            divisions=DIVISIONS,
            roster=[_emp("10021", "2"), _emp("10050", "10"), _emp("10099", "4")],
        )
        self.login_as("admin1", role="admin")
        committee_id = self.client.post("/committees", json={"name_ar": "لجنة", "organization_id": "1"}).json()["id"]
        self.session_id = self.client.post(
            "/sessions", json={"committee_id": committee_id, "name": "جلسة"}
        ).json()["id"]

    def _evaluate_and_resolve(self, employee_id: str, outcome: str | None) -> None:
        response = self.client.post(f"/sessions/{self.session_id}/evaluations", json={"employee_id": employee_id, "score": 1})
        self.assertEqual(response.status_code, 201, response.text)
        if outcome is not None:
            eval_id = response.json()["id"]
            result_response = self.client.post(f"/evaluations/{eval_id}/results", json={"outcome": outcome})
            self.assertEqual(result_response.status_code, 201, result_response.text)

    def test_untouched_employee_still_appears_as_not_completed(self):
        """10021/10050 get evaluated below; 10099 is never touched at all -
        it must still appear in the report (roster-driven, not result-driven)."""
        self._evaluate_and_resolve("10021", "pass")
        self._evaluate_and_resolve("10050", "absent")

        response = self.client.get(f"/reports/sessions/{self.session_id}")
        self.assertEqual(response.status_code, 200)
        rows = {row["employee_id"]: row for row in response.json()["rows"]}

        self.assertEqual(set(rows.keys()), {"10021", "10050", "10099"})
        self.assertEqual(rows["10021"]["status"], "completed")
        self.assertEqual(rows["10050"]["status"], "absent")
        self.assertEqual(rows["10099"]["status"], "not_completed")
        self.assertIsNone(rows["10099"]["outcome"])

    def test_report_ordered_numerically_by_rank(self):
        response = self.client.get(f"/reports/sessions/{self.session_id}")
        ids_in_order = [row["employee_id"] for row in response.json()["rows"]]
        self.assertEqual(ids_in_order, ["10021", "10099", "10050"])  # ranks 2, 4, 10

    def test_viewer_without_grant_cannot_read_report(self):
        self.client.post("/auth/logout")
        self.login_as("plainviewer")
        response = self.client.get(f"/reports/sessions/{self.session_id}")
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
