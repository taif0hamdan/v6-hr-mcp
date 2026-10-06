"""
Additive schema creation for the HR backend's own database.

Run directly (`python init_hr_backend_db.py`) or imported and called from
hr_backend/app.py's startup hook. Uses SQLAlchemy's create_all, which only
creates tables that don't already exist - never drops or alters existing
ones. For a real migration history (adding a column later, say) use Alembic
revisions instead of editing this script to mutate existing tables.
"""

from __future__ import annotations

import logging

from hr_backend.db import get_engine
from hr_backend.models import Base

logger = logging.getLogger(__name__)


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    logger.info("hr_backend database schema ensured (additive, no drops).")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_db()
