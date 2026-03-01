"""Enrichment service — classifies, cleans, and extracts metadata from raw text.

Uses a single Haiku call to produce a structured EnrichmentResult including
intent, clean text, entry type, entities, open loop detection, tags, and
optional calendar event association.
"""

import logging
from datetime import date

from second_brain.prompts.enrichment import (
    ENRICHMENT_SYSTEM_PROMPT,
    EnrichmentResult,
    build_enrichment_user_prompt,
)
from second_brain.services.anthropic_client import AnthropicClient

logger = logging.getLogger(__name__)


class EnrichmentService:
    """Enriches raw text captures via a single Haiku LLM call."""

    def __init__(self, anthropic_client: AnthropicClient) -> None:
        self.anthropic_client = anthropic_client

    def enrich_text(
        self,
        raw_text: str,
        calendar_events: list[dict] | None = None,
    ) -> EnrichmentResult:
        """Run enrichment on raw text.

        Args:
            raw_text: The original text (typed or transcribed).
            calendar_events: Optional list of recent/upcoming calendar event
                dicts for meeting association context.

        Returns:
            EnrichmentResult with intent, clean_text, entities, tags, etc.
            If intent is 'query', the caller should route to the query pipeline.
        """
        current_date = date.today().isoformat()

        user_prompt = build_enrichment_user_prompt(
            raw_text=raw_text,
            calendar_events=calendar_events,
            current_date=current_date,
        )

        result = self.anthropic_client.call_haiku(
            system_prompt=ENRICHMENT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=EnrichmentResult,
        )

        logger.info(
            "Enrichment complete | intent=%s | entry_type=%s | entities=%d | tags=%s | is_open_loop=%s",
            result.intent,
            result.entry_type,
            len(result.entities),
            result.tags,
            result.is_open_loop,
        )

        return result
