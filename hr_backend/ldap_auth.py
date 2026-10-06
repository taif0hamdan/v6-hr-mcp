"""
LDAP authentication - verifies the user's own credentials via a bind as
that user (not just a directory search), and fetches the configured
employee attribute case-insensitively. LDAP is the ONLY authentication
authority; HR is never consulted for login, and AD is never substituted
as an organization source when HR is unreachable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from ldap3 import Connection, Server
from ldap3.core.exceptions import LDAPBindError, LDAPException, LDAPSocketOpenError
from ldap3.utils.conv import escape_filter_chars

from hr_backend.config import LdapSettings, get_settings
from hr_backend.normalize import normalize_id

logger = logging.getLogger(__name__)


class LdapOutcome(str, Enum):
    AUTHENTICATED = "authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    employee_id: str | None
    email: str | None


@dataclass(frozen=True)
class LdapAuthResult:
    outcome: LdapOutcome
    identity: LdapIdentity | None = None


def _get_attr(entry, name: str) -> str | None:
    # ldap3 Entry attribute access is case-insensitive by attribute name,
    # but be defensive and try exact + lowercase.
    for candidate in (name, name.lower(), name.upper()):
        try:
            value = entry[candidate].value
        except Exception:
            continue
        if value:
            return str(value).strip() or None
    return None


def authenticate(username: str, password: str, settings: LdapSettings | None = None) -> LdapAuthResult:
    settings = settings or get_settings().ldap

    if not settings.configured:
        return LdapAuthResult(outcome=LdapOutcome.NOT_CONFIGURED)

    try:
        server = Server(settings.host, port=settings.port, use_ssl=settings.use_ssl, get_info=None)
        service_conn = Connection(
            server, user=settings.bind_dn, password=settings.bind_password, auto_bind=True
        )
    except (LDAPException, LDAPSocketOpenError) as exc:
        logger.error("LDAP service bind failed: %s", type(exc).__name__)
        return LdapAuthResult(outcome=LdapOutcome.UNAVAILABLE)

    try:
        safe_username = escape_filter_chars(username)
        search_filter = f"({settings.username_attr}={safe_username})"
        service_conn.search(
            settings.search_base,
            search_filter,
            attributes=[settings.employee_attr, settings.username_attr, settings.email_attr],
        )
        if not service_conn.entries:
            return LdapAuthResult(outcome=LdapOutcome.NOT_FOUND)

        entry = service_conn.entries[0]
        user_dn = entry.entry_dn
        employee_id = normalize_id(_get_attr(entry, settings.employee_attr))
        email = _get_attr(entry, settings.email_attr)
    finally:
        service_conn.unbind()

    try:
        user_conn = Connection(server, user=user_dn, password=password, auto_bind=True)
        user_conn.unbind()
    except LDAPBindError:
        return LdapAuthResult(outcome=LdapOutcome.INVALID_CREDENTIALS)
    except (LDAPException, LDAPSocketOpenError) as exc:
        logger.error("LDAP user bind failed: %s", type(exc).__name__)
        return LdapAuthResult(outcome=LdapOutcome.UNAVAILABLE)

    return LdapAuthResult(
        outcome=LdapOutcome.AUTHENTICATED,
        identity=LdapIdentity(username=username, employee_id=employee_id, email=email),
    )
