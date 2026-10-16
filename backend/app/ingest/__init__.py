"""Ingest adapters that load external sources into Informed's ZeroDB tables.

Each module here owns one source: `legislators.py` for
unitedstates/congress-legislators (story 2.2). An adapter's job stops at
producing validated `app.repositories.models` rows and writing them through
the story 1.4 repository layer — it never calls `ZeroDBClient` directly and
never invents a table of its own.
"""
