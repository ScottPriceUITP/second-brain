"""Connection scoring service — finds and scores related entries via FTS + Haiku."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel
from sqlalchemy.orm import Session

from second_brain.config import get_config_float, get_config_int
from second_brain.models.entry import Entry
from second_brain.models.relation import EntryRelation
from second_brain.prompts.connection_scoring import (
    CONNECTION_SCORING_SYSTEM,
    build_scoring_user_prompt,
)
from second_brain.services.anthropic_client import AnthropicClient
from second_brain.utils.fts import fts_search

logger = logging.getLogger(__name__)


class ConnectionScore(BaseModel):
    """A single scored connection from Haiku."""

    candidate_id: int
    score: int
    relation_type: str


class ConnectionScoringResponse(BaseModel):
    """Haiku's response for connection scoring."""

    connections: list[ConnectionScore]


@dataclass
class ScoredConnection:
    """A scored connection between two entries."""

    entry_id: int
    score: int
    relation_type: str
    entry: Entry | None = None


class ConnectionScoringService:
    """Scores connections between a new entry and existing entries."""

    def __init__(self, client: AnthropicClient, session: Session) -> None:
        self.client = client
        self.session = session

    def _get_score_threshold(self) -> int:
        return get_config_int(self.session, "connection_score_threshold") or 4

    def _get_min_count(self) -> int:
        return get_config_int(self.session, "connection_min_count") or 2

    def _build_fts_query(self, entry: Entry) -> str:
        """Build an FTS query from the entry's entities and key terms.

        Combines entity names with key terms extracted from the entry text.
        """
        terms: list[str] = []

        # Add entity names
        for entity in entry.entities:
            terms.append(entity.name)

        # Add key terms from clean_text (first ~100 words, skip very short words)
        if entry.clean_text:
            words = entry.clean_text.split()[:100]
            for word in words:
                cleaned = word.strip(".,!?;:\"'()[]{}").lower()
                if len(cleaned) >= 4 and cleaned not in terms:
                    terms.append(cleaned)

        return " ".join(terms[:20])  # Cap at 20 terms for FTS

    def score_connections(self, entry: Entry) -> list[ScoredConnection]:
        """Score connections between a new entry and existing entries.

        Flow:
        1. Build FTS query from entry's entities + key terms
        2. FTS search for top 10 related entries (excluding the entry itself)
        3. Pass entry + candidates to Haiku for scoring
        4. Store relations for all scores
        5. Return strong connections (score >= threshold) for notification

        Args:
            entry: The new entry to find connections for.

        Returns:
            List of ScoredConnection objects for strong connections.
            Empty list if fewer than min_count strong connections found.
        """
        fts_query = self._build_fts_query(entry)
        if not fts_query:
            logger.info("No FTS query terms for entry %d, skipping scoring", entry.id)
            return []

        candidates = fts_search(
            self.session,
            fts_query,
            limit=10,
            exclude_entry_id=entry.id,
        )

        if not candidates:
            logger.info("No FTS candidates for entry %d", entry.id)
            return []

        # Build prompt for Haiku
        candidate_data = [
            {"id": c.id, "clean_text": c.clean_text or c.raw_text}
            for c in candidates
        ]
        user_prompt = build_scoring_user_prompt(
            entry.clean_text or entry.raw_text,
            candidate_data,
        )

        # Call Haiku for scoring
        response = self.client.call_haiku(
            CONNECTION_SCORING_SYSTEM,
            user_prompt,
            ConnectionScoringResponse,
        )

        # Map candidate IDs to entries for quick lookup
        candidate_map = {c.id: c for c in candidates}

        threshold = self._get_score_threshold()
        min_count = self._get_min_count()
        strong_connections: list[ScoredConnection] = []

        # Store all scored connections as EntryRelation records
        for conn in response.connections:
            if conn.candidate_id not in candidate_map:
                logger.warning(
                    "Haiku returned unknown candidate_id %d for entry %d",
                    conn.candidate_id,
                    entry.id,
                )
                continue

            relation = EntryRelation(
                from_entry_id=entry.id,
                to_entry_id=conn.candidate_id,
                relation_type=conn.relation_type,
                confidence_score=float(conn.score),
                created_at=datetime.now(timezone.utc),
            )
            self.session.add(relation)

            if conn.score >= threshold:
                strong_connections.append(
                    ScoredConnection(
                        entry_id=conn.candidate_id,
                        score=conn.score,
                        relation_type=conn.relation_type,
                        entry=candidate_map[conn.candidate_id],
                    )
                )

        self.session.flush()

        logger.info(
            "Scored %d connections for entry %d (%d strong)",
            len(response.connections),
            entry.id,
            len(strong_connections),
        )

        # Only return strong connections if we meet the minimum count
        if len(strong_connections) >= min_count:
            return strong_connections

        return []
