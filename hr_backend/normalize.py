"""
Maps raw HR provider fields (V4 - oracleapi's verified contract) onto a
normalized model, and resolves each employee's organizational ancestry.

Field map (see the plan's table for the full rationale):
    employee_id        <- MIL_ID
    full_name_ar        <- FULL_NAME_ARB
    full_name            <- LATIN_NAME, fallback FULL_NAME_ARB, fallback employee_id
    rank_id               <- RANK_ID
    divn_id / unit_id       <- DIVN_ID / UNIT_ID (both present directly on Employee)
    organization_name_ar     <- Division.DIVN_NAME_ARB lookup

Division hierarchy: DIVN_ID -> CTRL_DIVN_ID is the real parent pointer;
DIVN_TYPE is one of DEPT/UNIT/SECTION in the live data (others theoretically
possible per the spec's OTHERS/MILITARY exclusion rule, not currently seen).

Two separate concerns, kept separate on purpose:
  - ancestry (resolve_ancestor_chain): walks the FULL, unfiltered tree -
    "do not silently lose valid nodes" / orphans still resolve correctly
    even if an intermediate node happens to be an excluded type.
  - exclusion (is_excluded_division_type): only affects which divisions are
    offered in a selection/listing tree (see visibility.py), never breaks
    the parent-pointer walk and never excludes an EMPLOYEE for any reason
    tied to their own EMPLOYEE_TYPE-equivalent field.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

EXCLUDED_DIVISION_TYPES = {"others", "military"}


def normalize_id(raw) -> str | None:
    """Trim to a string, preserving leading zeros; blank/None -> None. Never coerce to int."""
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def is_excluded_division_type(divn_type: str | None) -> bool:
    return (divn_type or "").strip().lower() in EXCLUDED_DIVISION_TYPES


@dataclass(frozen=True)
class NormalizedDivision:
    id: str
    parent_id: str | None
    name_ar: str | None
    name_en: str | None
    divn_type: str | None


@dataclass(frozen=True)
class NormalizedEmployee:
    employee_id: str  # MIL_ID
    pf_no: str | None  # kept for the legacy /employee-view enrichment path only
    full_name_ar: str | None
    full_name: str | None
    rank_id: str | None
    divn_id: str | None
    unit_id: str | None
    # Filled in by classify_membership() once the division index is available.
    organization_id: str | None = None
    department_id: str | None = None
    section_id: str | None = None
    subsection_id: str | None = None
    organization_name_ar: str | None = None


def normalize_division(raw: dict) -> NormalizedDivision:
    div_id = normalize_id(raw.get("DIVN_ID"))
    if div_id is None:
        raise ValueError(f"division row missing DIVN_ID: {raw!r}")
    return NormalizedDivision(
        id=div_id,
        parent_id=normalize_id(raw.get("CTRL_DIVN_ID")),
        name_ar=raw.get("DIVN_NAME_ARB"),
        name_en=raw.get("DIVN_NAME_ENG"),
        divn_type=raw.get("DIVN_TYPE"),
    )


def normalize_employee(raw: dict) -> NormalizedEmployee:
    employee_id = normalize_id(raw.get("MIL_ID"))
    if employee_id is None:
        raise ValueError(f"employee row missing MIL_ID: {raw!r}")
    full_name_ar = raw.get("FULL_NAME_ARB") or None
    full_name = raw.get("LATIN_NAME") or full_name_ar or employee_id
    return NormalizedEmployee(
        employee_id=employee_id,
        pf_no=normalize_id(raw.get("PF_NO")),
        full_name_ar=full_name_ar,
        full_name=full_name,
        rank_id=normalize_id(raw.get("RANK_ID")),
        divn_id=normalize_id(raw.get("DIVN_ID")),
        unit_id=normalize_id(raw.get("UNIT_ID")),
    )


def build_division_index(divisions: list[dict]) -> dict[str, NormalizedDivision]:
    index: dict[str, NormalizedDivision] = {}
    for raw in divisions:
        try:
            d = normalize_division(raw)
        except ValueError:
            continue
        index[d.id] = d
    return index


def resolve_ancestor_chain(
    divn_id: str | None,
    index: dict[str, NormalizedDivision],
    *,
    max_depth: int = 50,
) -> list[NormalizedDivision]:
    """Leaf-to-root chain, walking the FULL tree (never filtered). Stops at a
    missing/None parent; a cycle or a dangling parent_id (orphan) stops the
    walk rather than raising, so a single bad row never breaks the caller."""
    chain: list[NormalizedDivision] = []
    seen: set[str] = set()
    current = divn_id
    while current and current not in seen and len(chain) < max_depth:
        seen.add(current)
        node = index.get(current)
        if node is None:
            break  # orphan: parent_id points at a division we don't have
        chain.append(node)
        current = node.parent_id
    return chain


def classify_membership(
    emp: NormalizedEmployee,
    index: dict[str, NormalizedDivision],
) -> NormalizedEmployee:
    """Fills organization_id/department_id/section_id/subsection_id from
    however many levels actually exist for this employee - this schema has
    DEPT/UNIT/SECTION division types plus a separate UNIT_ID employee field,
    not a fixed always-4-levels model. Unmatched levels stay None (wildcard
    on the committee side, not a false non-match)."""
    chain = resolve_ancestor_chain(emp.divn_id, index)

    department_id = None
    section_id = None
    organization_id = None
    for node in chain:
        divn_type = (node.divn_type or "").strip().upper()
        if divn_type == "DEPT" and department_id is None:
            department_id = node.id
        elif divn_type == "SECTION" and section_id is None:
            section_id = node.id
        # The top-most node in the chain (root ancestor) stands in for
        # "organization" - walk to the end regardless of type.
        organization_id = node.id

    organization_name_ar = None
    if organization_id and organization_id in index:
        organization_name_ar = index[organization_id].name_ar

    return NormalizedEmployee(
        employee_id=emp.employee_id,
        pf_no=emp.pf_no,
        full_name_ar=emp.full_name_ar,
        full_name=emp.full_name,
        rank_id=emp.rank_id,
        divn_id=emp.divn_id,
        unit_id=emp.unit_id,
        organization_id=organization_id,
        department_id=department_id,
        section_id=section_id,
        subsection_id=emp.unit_id,  # UNIT_ID is the finest-grained level this provider exposes directly
        organization_name_ar=organization_name_ar,
    )


def get_cached_division_index(session: Session) -> dict[str, NormalizedDivision]:
    """Reads the cached division list and builds the full (unfiltered)
    index used for ancestry/authorization/visibility resolution. Returns
    an empty index on a cold/unavailable cache rather than raising - every
    caller already treats "division not found" as a safe no-match."""
    # Imported here, not at module level, to avoid a hr_cache <-> normalize
    # import cycle (hr_cache has no need to import normalize).
    from hr_backend.cache_keys import CACHE_KEY_DIVISIONS
    from hr_backend.config import get_settings
    from hr_backend.hr_cache import read_with_policy

    settings = get_settings()
    cached = read_with_policy(
        session,
        CACHE_KEY_DIVISIONS,
        fresh_ttl_seconds=settings.cache.divisions_ttl_seconds,
        max_served_age_seconds=settings.cache.roster_max_served_age_seconds,
    )
    if not cached.payload:
        return {}
    return build_division_index(cached.payload)


def get_cached_roster(session: Session) -> list[NormalizedEmployee]:
    """Reads the cached, already-normalized+classified roster (written by
    background.py's refresh job). Returns [] on a cold/unavailable cache -
    every caller already treats "no roster yet" as a safe empty result,
    never a crash."""
    from hr_backend.cache_keys import CACHE_KEY_ROSTER
    from hr_backend.config import get_settings
    from hr_backend.hr_cache import read_with_policy

    settings = get_settings()
    cached = read_with_policy(
        session,
        CACHE_KEY_ROSTER,
        fresh_ttl_seconds=settings.cache.roster_fresh_seconds,
        max_served_age_seconds=settings.cache.roster_max_served_age_seconds,
    )
    if not cached.payload:
        return []
    return [NormalizedEmployee(**row) for row in cached.payload]


def find_cached_employee(session: Session, employee_id: str | None) -> NormalizedEmployee | None:
    if not employee_id:
        return None
    for emp in get_cached_roster(session):
        if emp.employee_id == employee_id:
            return emp
    return None
