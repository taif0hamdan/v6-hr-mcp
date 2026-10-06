"""
Inherited-setting resolver for a division's classification: explicit value
on this node -> nearest explicit ancestor -> default. The reference stores
a raw value ("regular"/"pro") and displays an Arabic label
(إداري/مقاتل) - both are resolved by the SAME function here, used by every
endpoint that needs it, so UI and backend can never disagree. An explicit
child override is intentional and must survive a parent edit unchanged;
"return to inheritance" means deleting the child's own override row, never
rewriting it to copy the ancestor's current value.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from hr_backend.models import UnitClassificationOverride
from hr_backend.normalize import NormalizedDivision, resolve_ancestor_chain

DEFAULT_CLASSIFICATION = "regular"

CLASSIFICATION_LABELS_AR = {
    "regular": "إداري",
    "pro": "مقاتل",
}


@dataclass(frozen=True)
class ResolvedClassification:
    value: str
    label_ar: str
    source: str  # "explicit" | "inherited" | "default"
    inherited_from_divn_id: str | None


def _load_overrides(session: Session) -> dict[str, str]:
    rows = session.query(UnitClassificationOverride).all()
    return {row.divn_id: row.classification for row in rows}


def resolve_classification(
    session: Session,
    divn_id: str,
    division_index: dict[str, NormalizedDivision],
) -> ResolvedClassification:
    overrides = _load_overrides(session)

    if divn_id in overrides:
        value = overrides[divn_id]
        return ResolvedClassification(
            value=value,
            label_ar=CLASSIFICATION_LABELS_AR.get(value, value),
            source="explicit",
            inherited_from_divn_id=None,
        )

    for ancestor in resolve_ancestor_chain(divn_id, division_index)[1:]:  # skip self, already checked above
        if ancestor.id in overrides:
            value = overrides[ancestor.id]
            return ResolvedClassification(
                value=value,
                label_ar=CLASSIFICATION_LABELS_AR.get(value, value),
                source="inherited",
                inherited_from_divn_id=ancestor.id,
            )

    return ResolvedClassification(
        value=DEFAULT_CLASSIFICATION,
        label_ar=CLASSIFICATION_LABELS_AR.get(DEFAULT_CLASSIFICATION, DEFAULT_CLASSIFICATION),
        source="default",
        inherited_from_divn_id=None,
    )


def set_explicit_classification(session: Session, divn_id: str, classification: str, *, set_by: int | None) -> None:
    row = session.get(UnitClassificationOverride, divn_id)
    if row is None:
        row = UnitClassificationOverride(divn_id=divn_id, classification=classification)
        session.add(row)
    else:
        row.classification = classification
    row.set_by = set_by


def clear_explicit_classification(session: Session, divn_id: str) -> bool:
    """"Return to inheritance" - deletes the override row entirely, rather
    than rewriting it to the ancestor's current value (which would silently
    freeze in a value that should keep tracking the ancestor)."""
    row = session.get(UnitClassificationOverride, divn_id)
    if row is None:
        return False
    session.delete(row)
    return True
