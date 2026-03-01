"""Query engine — classifies queries and assembles context for LLM answering."""

import logging
from dataclasses import dataclass, field

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from second_brain.config import get_config_int
from second_brain.models.entity import entry_entities
from second_brain.models.entry import Entry
from second_brain.prompts.query_simple import (
    QUERY_SIMPLE_SYSTEM,
    SimpleQueryResponse,
)
from second_brain.prompts.query_synthesis import (
    QUERY_SYNTHESIS_SYSTEM,
    SynthesisQueryResponse,
)
from second_brain.services.anthropic_client import AnthropicClient
from second_brain.services.query_session import QuerySession
from second_brain.utils.fts import fts_search

logger = logging.getLogger(__name__)


# --- Classification prompt (Haiku) ---

_CLASSIFY_SYSTEM = """\
You are a query classifier. Given a user question, determine if it requires:
- "simple": A direct fact lookup, date check, or single-topic retrieval.
- "synthesis": Summarization, comparison, analysis across multiple topics, or \
pattern identification.

RESPOND WITH VALID JSON:
{"complexity": "simple" or "synthesis"}\
"""


class _ClassifyResponse(BaseModel):
    complexity: str = Field(description="Query complexity: 'simple' or 'synthesis'.")


# --- Query response model ---


@dataclass
class QuerySource:
    """A source entry referenced in a query response."""

    entry_id: int
    date: str
    snippet: str


@dataclass
class QueryResponse:
    """Result from handle_query."""

    answer: str
    sources: list[QuerySource] = field(default_factory=list)
    model_used: str = ""


class QueryEngine:
    """Handles user queries against the knowledge base.

    Flow:
    1. Classify complexity via Haiku (simple vs synthesis).
    2. Assemble context: FTS search -> matching entries -> linked entities
       -> one-hop entries -> deduplicate.
    3. Route to Haiku (simple) or Sonnet (synthesis) with assembled context.
    4. Return response with source attribution.
    """

    def __init__(
        self,
        anthropic_client: AnthropicClient,
        session_factory: sessionmaker,
    ) -> None:
        self.anthropic_client = anthropic_client
        self.session_factory = session_factory

    def handle_query(
        self,
        query_text: str,
        session_context: QuerySession | None = None,
    ) -> QueryResponse:
        """Process a user query and return an answer with sources.

        Args:
            query_text: The user's question.
            session_context: Optional previous query session for follow-ups.

        Returns:
            QueryResponse with answer, sources, and model used.
        """
        # 1. Classify complexity
        complexity = self._classify_query(query_text)
        logger.info("Query classified as '%s': %s", complexity, query_text[:100])

        with self.session_factory() as session:
            # 2. Assemble context entries
            max_entries = get_config_int(session, "query_max_entries") or 30
            context_entries = self._assemble_context(session, query_text, max_entries)

            # Include session context entries if available
            if session_context and session_context.source_entry_ids:
                session_entry_ids = set(session_context.source_entry_ids)
                existing_ids = {e.id for e in context_entries}
                missing_ids = session_entry_ids - existing_ids
                if missing_ids:
                    extra = (
                        session.query(Entry)
                        .filter(Entry.id.in_(missing_ids))
                        .all()
                    )
                    context_entries.extend(extra)

            # 3. Build user prompt
            user_prompt = self._build_user_prompt(
                query_text, context_entries, session_context
            )

            # 4. Build source lookup for attribution
            entry_map = {e.id: e for e in context_entries}

        # 5. Route to appropriate model
        if complexity == "synthesis":
            raw = self.anthropic_client.call_sonnet(
                system_prompt=QUERY_SYNTHESIS_SYSTEM,
                user_prompt=user_prompt,
                response_model=SynthesisQueryResponse,
            )
            model_used = "sonnet"
        else:
            raw = self.anthropic_client.call_haiku(
                system_prompt=QUERY_SIMPLE_SYSTEM,
                user_prompt=user_prompt,
                response_model=SimpleQueryResponse,
            )
            model_used = "haiku"

        # 6. Build response with source attribution
        sources = []
        for eid in raw.source_entry_ids:
            entry = entry_map.get(eid)
            if entry:
                snippet = (entry.clean_text or entry.raw_text or "")[:120]
                date_str = entry.created_at.strftime("%Y-%m-%d")
                sources.append(QuerySource(entry_id=eid, date=date_str, snippet=snippet))

        logger.info(
            "Query answered | model=%s | sources=%d | query=%s",
            model_used,
            len(sources),
            query_text[:80],
        )

        return QueryResponse(
            answer=raw.answer,
            sources=sources,
            model_used=model_used,
        )

    def _classify_query(self, query_text: str) -> str:
        """Classify query complexity via Haiku."""
        result = self.anthropic_client.call_haiku(
            system_prompt=_CLASSIFY_SYSTEM,
            user_prompt=query_text,
            response_model=_ClassifyResponse,
        )
        if result.complexity in ("simple", "synthesis"):
            return result.complexity
        return "simple"

    def _assemble_context(
        self,
        session: Session,
        query_text: str,
        max_entries: int,
    ) -> list[Entry]:
        """Assemble context entries for the query.

        Steps:
        a. FTS search for relevant entries.
        b. Pull entities linked to those entries.
        c. Pull entries linked to those entities (one hop out).
        d. Deduplicate and cap at max_entries.
        """
        # a. FTS search
        fts_results = fts_search(session, query_text, limit=max_entries)
        if not fts_results:
            return []

        seen_ids: set[int] = set()
        ordered: list[Entry] = []

        for entry in fts_results:
            if entry.id not in seen_ids:
                seen_ids.add(entry.id)
                ordered.append(entry)

        # b. Collect entities from FTS results
        entity_ids: set[int] = set()
        for entry in fts_results:
            for entity in entry.entities:
                entity_ids.add(entity.id)

        # c. One-hop: entries linked to those entities (not already seen)
        if entity_ids and len(ordered) < max_entries:
            one_hop_entries = (
                session.query(Entry)
                .join(entry_entities, Entry.id == entry_entities.c.entry_id)
                .filter(
                    entry_entities.c.entity_id.in_(entity_ids),
                    Entry.id.notin_(seen_ids),
                )
                .limit(max_entries - len(ordered))
                .all()
            )
            for entry in one_hop_entries:
                if entry.id not in seen_ids:
                    seen_ids.add(entry.id)
                    ordered.append(entry)

        return ordered[:max_entries]

    @staticmethod
    def _build_user_prompt(
        query_text: str,
        entries: list[Entry],
        session_context: QuerySession | None,
    ) -> str:
        """Build the user prompt with context entries and optional session history."""
        parts: list[str] = []

        # Include previous session context if available
        if session_context:
            parts.append("PREVIOUS QUERY CONTEXT:")
            parts.append(f"Previous question: {session_context.query}")
            parts.append(f"Previous answer: {session_context.response}")
            parts.append("")

        # Add knowledge base entries
        parts.append("KNOWLEDGE BASE ENTRIES:")
        if not entries:
            parts.append("(No matching entries found)")
        else:
            for entry in entries:
                text = entry.clean_text or entry.raw_text or "(empty)"
                date_str = entry.created_at.strftime("%Y-%m-%d")
                parts.append(
                    f"\n[Entry {entry.id}, {date_str}, {entry.entry_type}]\n{text}"
                )

        parts.append(f"\nQUESTION:\n{query_text}")

        return "\n".join(parts)
