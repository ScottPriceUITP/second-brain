"""Full-text search utilities using SQLite FTS5."""

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

from second_brain.models.entry import Entry

logger = logging.getLogger(__name__)


def _sanitize_fts_query(query_text: str) -> str:
    """Sanitize a query string for FTS5 syntax.

    FTS5 treats certain characters as special operators. This function
    quotes individual terms to prevent syntax errors from user input.
    """
    # Remove FTS5 special characters and other non-alphanumeric chars
    cleaned = re.sub(r'[^a-zA-Z0-9\s]', " ", query_text)
    # Split into terms and quote each one
    terms = [t.strip() for t in cleaned.split() if t.strip()]
    if not terms:
        return ""
    # Join with OR for broader matching
    return " OR ".join(f'"{term}"' for term in terms)


def fts_search(
    session: Session,
    query_text: str,
    limit: int = 10,
    exclude_entry_id: int | None = None,
) -> list[Entry]:
    """Search the FTS5 virtual table on entries.clean_text.

    Args:
        session: SQLAlchemy session.
        query_text: The search query text.
        limit: Maximum number of results to return.
        exclude_entry_id: Optional entry ID to exclude from results.

    Returns:
        List of Entry objects ranked by relevance (best match first).
    """
    fts_query = _sanitize_fts_query(query_text)
    if not fts_query:
        return []

    # FTS5 rank is negative (more negative = better match)
    # Use bm25() for relevance ranking
    if exclude_entry_id is not None:
        sql = text(
            "SELECT rowid, rank FROM entries_fts "
            "WHERE entries_fts MATCH :query AND rowid != :exclude_id "
            "ORDER BY rank "
            "LIMIT :limit"
        )
        rows = session.execute(
            sql,
            {"query": fts_query, "exclude_id": exclude_entry_id, "limit": limit},
        ).fetchall()
    else:
        sql = text(
            "SELECT rowid, rank FROM entries_fts "
            "WHERE entries_fts MATCH :query "
            "ORDER BY rank "
            "LIMIT :limit"
        )
        rows = session.execute(
            sql,
            {"query": fts_query, "limit": limit},
        ).fetchall()

    if not rows:
        return []

    entry_ids = [row[0] for row in rows]

    # Fetch Entry objects preserving FTS rank order
    entries_by_id = {
        e.id: e
        for e in session.query(Entry).filter(Entry.id.in_(entry_ids)).all()
    }

    return [entries_by_id[eid] for eid in entry_ids if eid in entries_by_id]
