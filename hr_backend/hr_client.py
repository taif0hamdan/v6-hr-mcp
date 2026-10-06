"""
HTTP client for the HR provider (V4 - oracleapi, verified contract - see
database_docs/hr.yaml and the plan's field-map table). Talks only to the
REAL endpoints that exist; does not assume /employees/search, pagination,
or an {"items": [...]} envelope - none of those exist on this provider.

Bounded, separate timeout budgets: a single-person lookup must never
silently inherit the bulk roster's longer budget, and the bulk roster
fetch is only ever called from background.py's explicit refresh job or an
authorized admin action - never as a fallback from a failed single lookup,
and never for login/add-one-person.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from hr_backend.config import HrApiSettings, get_settings

logger = logging.getLogger(__name__)


class HrApiError(Exception):
    """Raised for any HR provider failure - connection, timeout, non-2xx, bad JSON."""


class HrApiUnavailable(HrApiError):
    """The provider could not be reached at all (connection/timeout) - distinct from a confirmed negative response."""


def _get(url: str, *, timeout: float) -> Any:
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.exceptions.Timeout as exc:
        raise HrApiUnavailable(f"timeout calling {url}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise HrApiUnavailable(f"connection error calling {url}") from exc
    if resp.status_code >= 500:
        raise HrApiUnavailable(f"{url} returned {resp.status_code}")
    if resp.status_code == 404:
        return None
    if not resp.ok:
        raise HrApiError(f"{url} returned {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise HrApiError(f"{url} returned non-JSON body") from exc


class HrClient:
    def __init__(self, settings: HrApiSettings | None = None):
        self._settings = settings or get_settings().hr_api

    def test_connection(self) -> bool:
        try:
            result = _get(f"{self._settings.base_url}/test-connection", timeout=self._settings.single_timeout_seconds)
        except HrApiError:
            return False
        return bool(result) and result.get("status") == "success"

    # --- single-employee lookups (10s budget) -----------------------------

    def get_employee(self, pf_no: str | int) -> dict | None:
        """GET /employees/{pf_no} - the provider's real primary-key lookup. Returns None on a confirmed 404."""
        return _get(
            f"{self._settings.base_url}/employees/{pf_no}",
            timeout=self._settings.single_timeout_seconds,
        )

    def get_employee_view(self, emp_no: str | int) -> dict | None:
        """GET /employee-view/{emp_no} - legacy PF_NO-keyed rank/job-title enrichment ONLY. Never used as the primary lookup."""
        return _get(
            f"{self._settings.base_url}/employee-view/{emp_no}",
            timeout=self._settings.single_timeout_seconds,
        )

    def get_division(self, divn_id: str | int) -> dict | None:
        return _get(
            f"{self._settings.base_url}/divisions/{divn_id}",
            timeout=self._settings.single_timeout_seconds,
        )

    # --- bulk fetches (120s budget - background/admin job only) -----------

    def list_employees(self) -> list[dict]:
        """GET /employees - the FULL roster in one call (confirmed: this provider has no pagination).
        Only call from background.py's refresh job or an explicit, authorized admin action."""
        result = _get(f"{self._settings.base_url}/employees", timeout=self._settings.bulk_timeout_seconds)
        return result if isinstance(result, list) else []

    def list_divisions(self) -> list[dict]:
        result = _get(f"{self._settings.base_url}/divisions", timeout=self._settings.bulk_timeout_seconds)
        return result if isinstance(result, list) else []
