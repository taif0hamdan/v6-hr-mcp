"""
LDAP auth + account-linking tests:
  - every LdapOutcome branch (not_configured, unavailable, not_found,
    invalid_credentials, authenticated), with ldap3's Server/Connection
    mocked out - no real LDAP server involved.
  - safe account linking: attach-by-username, attach-by-employee_id,
    ambiguous mapping rejected (both the "two different existing accounts"
    case and the "one matched account already conflicts" case fixed during
    this build).
  - HR outage during profile sync never blanks a known profile.
  - dev-login-bypass is refused outright when HR_BACKEND_ENV=production,
    and HR_SESSION_SECRET is mandatory (no safe default).
"""

import unittest
from unittest.mock import MagicMock, patch

from hr_backend_test_support import isolated_hr_backend_env
from hr_backend.config import LdapSettings
from hr_backend.db import session_scope
from hr_backend.ldap_auth import LdapIdentity, LdapOutcome, authenticate
from hr_backend.models import User
from hr_backend.normalize import NormalizedEmployee
from hr_backend.security import AmbiguousAccountMapping, find_or_link_account, sync_profile_fields


class _FakeAttr:
    def __init__(self, value):
        self.value = value


class _FakeEntry(dict):
    def __init__(self, entry_dn, **attrs):
        super().__init__({k: _FakeAttr(v) for k, v in attrs.items()})
        self.entry_dn = entry_dn


NOT_CONFIGURED_SETTINGS = LdapSettings(host=None, search_base=None)
CONFIGURED_SETTINGS = LdapSettings(
    host="ldap.example.com", search_base="dc=example,dc=com", bind_dn="svc", bind_password="pw",
    employee_attr="employeeID", username_attr="sAMAccountName", email_attr="mail",
)


class LdapOutcomeTests(unittest.TestCase):
    def test_not_configured(self):
        result = authenticate("user", "pw", NOT_CONFIGURED_SETTINGS)
        self.assertEqual(result.outcome, LdapOutcome.NOT_CONFIGURED)

    @patch("hr_backend.ldap_auth.Connection")
    @patch("hr_backend.ldap_auth.Server")
    def test_service_bind_unavailable(self, mock_server, mock_connection):
        from ldap3.core.exceptions import LDAPSocketOpenError

        mock_connection.side_effect = LDAPSocketOpenError("no route")
        result = authenticate("user", "pw", CONFIGURED_SETTINGS)
        self.assertEqual(result.outcome, LdapOutcome.UNAVAILABLE)

    @patch("hr_backend.ldap_auth.Connection")
    @patch("hr_backend.ldap_auth.Server")
    def test_user_not_found(self, mock_server, mock_connection):
        service_conn = MagicMock()
        service_conn.entries = []
        mock_connection.return_value = service_conn

        result = authenticate("ghost", "pw", CONFIGURED_SETTINGS)
        self.assertEqual(result.outcome, LdapOutcome.NOT_FOUND)

    @patch("hr_backend.ldap_auth.Connection")
    @patch("hr_backend.ldap_auth.Server")
    def test_invalid_credentials(self, mock_server, mock_connection):
        from ldap3.core.exceptions import LDAPBindError

        service_conn = MagicMock()
        service_conn.entries = [_FakeEntry("cn=jdoe,dc=example,dc=com", employeeID="10021", mail="jdoe@example.com")]

        def connection_side_effect(server, user=None, password=None, auto_bind=None):
            if user == CONFIGURED_SETTINGS.bind_dn:
                return service_conn
            raise LDAPBindError("bad password")

        mock_connection.side_effect = connection_side_effect
        result = authenticate("jdoe", "wrongpw", CONFIGURED_SETTINGS)
        self.assertEqual(result.outcome, LdapOutcome.INVALID_CREDENTIALS)

    @patch("hr_backend.ldap_auth.Connection")
    @patch("hr_backend.ldap_auth.Server")
    def test_authenticated_extracts_identity_case_insensitively(self, mock_server, mock_connection):
        service_conn = MagicMock()
        service_conn.entries = [_FakeEntry("cn=jdoe,dc=example,dc=com", employeeID="010021", mail="jdoe@example.com")]
        user_conn = MagicMock()

        def connection_side_effect(server, user=None, password=None, auto_bind=None):
            return service_conn if user == CONFIGURED_SETTINGS.bind_dn else user_conn

        mock_connection.side_effect = connection_side_effect
        result = authenticate("jdoe", "rightpw", CONFIGURED_SETTINGS)
        self.assertEqual(result.outcome, LdapOutcome.AUTHENTICATED)
        self.assertEqual(result.identity.employee_id, "010021")  # leading zero preserved
        self.assertEqual(result.identity.email, "jdoe@example.com")


class SafeAccountLinkingTests(unittest.TestCase):
    def test_attach_employee_id_to_username_match_preserves_role(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                s.add(User(ldap_username="jdoe", role="admin"))
            with session_scope() as s:
                user = find_or_link_account(s, LdapIdentity(username="JDoe", employee_id="10021", email=None))
                self.assertEqual(user.employee_id, "10021")
                self.assertEqual(user.role, "admin")  # app-owned field untouched by linking

    def test_ambiguous_two_different_accounts_rejected(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                s.add(User(ldap_username="alice", role="viewer"))
                s.add(User(employee_id="999", role="viewer"))
            with self.assertRaises(AmbiguousAccountMapping):
                with session_scope() as s:
                    find_or_link_account(s, LdapIdentity(username="alice", employee_id="999", email=None))

    def test_ambiguous_conflicting_value_on_single_matched_account_rejected(self):
        """Regression: a username already on file with a DIFFERENT employee_id
        than LDAP now presents must be rejected, not silently kept as-is."""
        with isolated_hr_backend_env():
            with session_scope() as s:
                s.add(User(employee_id="55555", ldap_username="otherlogin", role="viewer"))
            with self.assertRaises(AmbiguousAccountMapping):
                with session_scope() as s:
                    find_or_link_account(s, LdapIdentity(username="otherlogin", employee_id="77777", email=None))

    def test_auto_provision_disabled_rejects_unknown_identity(self):
        with isolated_hr_backend_env(AUTO_PROVISION_USERS="false"):
            with self.assertRaises(LookupError):
                with session_scope() as s:
                    find_or_link_account(s, LdapIdentity(username="brandnew", employee_id="1", email=None))


class ProfileSyncOutageTests(unittest.TestCase):
    def test_hr_outage_never_blanks_known_profile(self):
        user = User(full_name_ar="existing-name", rank_id="2")
        sync_profile_fields(user, identity=None, hr_employee=None)
        self.assertEqual(user.full_name_ar, "existing-name")
        self.assertEqual(user.rank_id, "2")

    def test_hr_available_updates_hr_owned_fields_only(self):
        user = User(full_name_ar="old", role="admin", is_active=True)
        emp = NormalizedEmployee(
            employee_id="1", pf_no=None, full_name_ar="new", full_name="New Name",
            rank_id="3", divn_id=None, unit_id=None, organization_id="7",
        )
        sync_profile_fields(user, identity=None, hr_employee=emp)
        self.assertEqual(user.full_name_ar, "new")
        self.assertEqual(user.organization_id, "7")
        self.assertEqual(user.role, "admin")  # app-owned, never touched here


class SecuritySettingsValidationTests(unittest.TestCase):
    """These construct Settings() directly (bypassing init_hr_backend_db/
    get_engine, which isolated_hr_backend_env's __enter__ would otherwise
    trigger before the assertRaises block even starts) - the point here is
    solely that SecuritySettings.__post_init__ itself refuses these configs."""

    def test_missing_session_secret_refused(self):
        import os

        import hr_backend.config as hr_config

        previous = os.environ.get("HR_SESSION_SECRET")
        os.environ["HR_SESSION_SECRET"] = ""
        try:
            with self.assertRaises(RuntimeError):
                hr_config.SecuritySettings()
        finally:
            if previous is None:
                os.environ.pop("HR_SESSION_SECRET", None)
            else:
                os.environ["HR_SESSION_SECRET"] = previous

    def test_dev_bypass_refused_in_production(self):
        import hr_backend.config as hr_config

        with self.assertRaises(RuntimeError):
            hr_config.SecuritySettings(
                session_secret="test-secret", dev_login_bypass=True, environment="production"
            )


if __name__ == "__main__":
    unittest.main()
