"""
HR Backend configuration.

Same env-first pattern as the MCP server's main.py:load_config() - every
setting is `os.getenv("ENV_NAME") or <default>`, so .env always wins and
nothing requires editing code to deploy against different infrastructure.

No setting here has a working default for LDAP (host/bind DN/credentials)
or for the eligibility status field - those are genuinely unknown for this
environment and must be supplied, not guessed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class LdapSettings:
    host: str | None = field(default_factory=lambda: os.getenv("LDAP_HOST"))
    port: int = field(default_factory=lambda: _int_env("LDAP_PORT", 389))
    use_ssl: bool = field(default_factory=lambda: _bool_env("LDAP_USE_SSL", False))
    bind_dn: str | None = field(default_factory=lambda: os.getenv("LDAP_BIND_DN"))
    bind_password: str | None = field(default_factory=lambda: os.getenv("LDAP_BIND_PASSWORD"))
    search_base: str | None = field(default_factory=lambda: os.getenv("LDAP_SEARCH_BASE"))
    # Attribute on the LDAP entry that carries the HR join key (matched
    # case-insensitively against the configured employee_id field, per spec).
    employee_attr: str = field(default_factory=lambda: os.getenv("LDAP_EMPLOYEE_ATTR", "employeeID"))
    username_attr: str = field(default_factory=lambda: os.getenv("LDAP_USERNAME_ATTR", "sAMAccountName"))
    email_attr: str = field(default_factory=lambda: os.getenv("LDAP_EMAIL_ATTR", "mail"))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.search_base)


@dataclass(frozen=True)
class HrApiSettings:
    # V4 - oracleapi's actual, verified default. Override via env for any
    # other deployment - never hardcode this into a browser-facing request,
    # it is read only on the server side.
    base_url: str = field(default_factory=lambda: os.getenv("HR_API_BASE_URL", "http://192.168.27.31:9009"))
    # Bounded budgets (seconds) - a single-person lookup must never
    # silently inherit the bulk roster's longer budget, and vice versa.
    single_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("HR_API_SINGLE_TIMEOUT_SECONDS", "10")))
    bulk_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("HR_API_BULK_TIMEOUT_SECONDS", "120")))
    # The field that would encode active/inactive status, IF one existed in
    # this environment's data. Verified empty (always NULL) against the
    # live Oracle instance as of this build - see eligibility.py. Left
    # configurable so wiring in a real field later is a one-line change,
    # not a code change.
    status_field: str | None = field(default_factory=lambda: os.getenv("HR_EMPLOYEE_STATUS_FIELD") or None)
    active_status_values: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            v.strip() for v in os.getenv("HR_ACTIVE_STATUS_VALUES", "").split(",") if v.strip()
        )
    )


@dataclass(frozen=True)
class CacheSettings:
    profile_ttl_seconds: int = field(default_factory=lambda: _int_env("CACHE_PROFILE_TTL_SECONDS", 15 * 60))
    divisions_ttl_seconds: int = field(default_factory=lambda: _int_env("CACHE_DIVISIONS_TTL_SECONDS", 60 * 60))
    search_ttl_seconds: int = field(default_factory=lambda: _int_env("CACHE_SEARCH_TTL_SECONDS", 60))
    roster_fresh_seconds: int = field(default_factory=lambda: _int_env("CACHE_ROSTER_FRESH_SECONDS", 15 * 60))
    roster_max_served_age_seconds: int = field(default_factory=lambda: _int_env("CACHE_ROSTER_MAX_SERVED_AGE_SECONDS", 24 * 60 * 60))
    background_poll_seconds: int = field(default_factory=lambda: _int_env("CACHE_BACKGROUND_POLL_SECONDS", 60))
    # Off by default in tests (set CACHE_BACKGROUND_REFRESH_ENABLED=false) to
    # avoid every test run making real, slow network calls to the HR provider.
    background_refresh_enabled: bool = field(default_factory=lambda: _bool_env("CACHE_BACKGROUND_REFRESH_ENABLED", True))
    # Mapping/schema version - bump this when normalize.py's field mapping
    # or eligibility rules change, so old snapshots are treated as stale
    # rather than silently reused under a new meaning.
    mapping_version: str = field(default_factory=lambda: os.getenv("CACHE_MAPPING_VERSION", "v1"))


@dataclass(frozen=True)
class SecuritySettings:
    session_secret: str = field(default_factory=lambda: os.getenv("HR_SESSION_SECRET", ""))
    session_max_age_seconds: int = field(default_factory=lambda: _int_env("HR_SESSION_MAX_AGE_SECONDS", 8 * 60 * 60))
    # Development-only local-login bypass. Must be explicitly off in
    # production; refused entirely if HR_BACKEND_ENV=production.
    dev_login_bypass: bool = field(default_factory=lambda: _bool_env("DEV_LOGIN_BYPASS", False))
    environment: str = field(default_factory=lambda: os.getenv("HR_BACKEND_ENV", "development"))
    # Whether a successful LDAP authentication with no matching local
    # account should create one automatically. False means an administrator
    # must provision accounts first - LDAP identity alone is not enough.
    auto_provision_users: bool = field(default_factory=lambda: _bool_env("AUTO_PROVISION_USERS", True))

    def __post_init__(self):
        if self.dev_login_bypass and self.environment == "production":
            raise RuntimeError(
                "DEV_LOGIN_BYPASS is set but HR_BACKEND_ENV=production - "
                "refusing to start with a login bypass enabled in production."
            )
        if not self.session_secret:
            raise RuntimeError(
                "HR_SESSION_SECRET is not set. Generate one (e.g. "
                "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`) "
                "and set it via environment/.env - there is no safe default."
            )


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("HR_BACKEND_DATABASE_URL", "sqlite:///hr_backend.db"))
    ldap: LdapSettings = field(default_factory=LdapSettings)
    hr_api: HrApiSettings = field(default_factory=HrApiSettings)
    cache: CacheSettings = field(default_factory=CacheSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test-only: force get_settings() to re-read env on next call."""
    global _settings
    _settings = None
