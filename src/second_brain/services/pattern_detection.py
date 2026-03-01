"""Pattern detection service — identifies recurring themes, contradictions, and patterns.

Uses a Sonnet call to analyze the last 7 days of entries and surface
genuinely interesting cross-entry insights.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session, sessionmaker

from second_brain.models.entry import Entry
from second_brain.models.nudge import NudgeHistory
from second_brain.prompts.pattern_detection import (
    PATTERN_DETECTION_SYSTEM_PROMPT,
    PatternDetectionResult,
    PatternInsight,
    build_pattern_detection_user_prompt,
)
from second_brain.services.anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


class PatternDetectionService:
    """Detects patterns and insights across recent entries via Sonnet."""

    def __init__(
        self,
        anthropic_client: AnthropicClient,
        session_factory: sessionmaker,
    ) -> None:
        self.anthropic_client = anthropic_client
        self.session_factory = session_factory

    def detect_patterns(self) -> list[PatternInsight]:
        """Pull entries from the last 7 days and identify patterns via Sonnet.

        Returns:
            List of PatternInsight objects. Empty if nothing noteworthy is found.
        """
        entries_data = self._fetch_recent_entries()

        if not entries_data:
            logger.info("No entries in the last 7 days — skipping pattern detection.")
            return []

        user_prompt = build_pattern_detection_user_prompt(entries_data)

        result: PatternDetectionResult = self.anthropic_client.call_sonnet(
            system_prompt=PATTERN_DETECTION_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=PatternDetectionResult,
        )

        logger.info(
            "Pattern detection complete | entries_analyzed=%d | patterns_found=%d",
            len(entries_data),
            len(result.patterns),
        )

        if result.patterns:
            self._record_nudge_history(result.patterns)

        return result.patterns

    def _fetch_recent_entries(self) -> list[dict]:
        """Fetch entries from the last 7 days and format them for the prompt.

        Returns:
            List of entry dicts with id, clean_text, entry_type, created_at, and tags.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        with self.session_factory() as session:
            entries = (
                session.query(Entry)
                .filter(
                    Entry.created_at >= cutoff,
                    Entry.status.notin_(["archived", "pending_enrichment", "pending_transcription"]),
                )
                .order_by(Entry.created_at.asc())
                .all()
            )

            entries_data = []
            for entry in entries:
                tag_names = [tag.name for tag in entry.tags] if entry.tags else []
                entries_data.append(
                    {
                        "id": entry.id,
                        "clean_text": entry.clean_text or entry.raw_text,
                        "entry_type": entry.entry_type,
                        "created_at": entry.created_at.strftime("%Y-%m-%d %H:%M"),
                        "tags": tag_names,
                    }
                )

        return entries_data

    def _record_nudge_history(self, patterns: list[PatternInsight]) -> None:
        """Create nudge_history records for delivered pattern insights.

        Args:
            patterns: The pattern insights to record.
        """
        with self.session_factory() as session:
            for pattern in patterns:
                nudge = NudgeHistory(
                    entry_id=None,  # Pattern nudges span multiple entries
                    nudge_type="pattern_insight",
                    message_text=pattern.insight_text,
                    escalation_level=1,
                    sent_at=datetime.now(timezone.utc),
                )
                session.add(nudge)

            session.commit()

            logger.info(
                "Recorded %d pattern insight nudge(s) in nudge_history.",
                len(patterns),
            )
