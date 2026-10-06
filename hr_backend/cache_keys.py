"""Shared cache-key constants, split out to avoid import cycles between
background.py, routers, and normalize.py (all of which need these)."""

CACHE_KEY_DIVISIONS = "divisions:all"
CACHE_KEY_ROSTER = "employees:roster"
