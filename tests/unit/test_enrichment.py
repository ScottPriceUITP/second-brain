"""Tests for enrichment service."""

from unittest.mock import MagicMock, patch

import pytest

from second_brain.prompts.enrichment import (
    EnrichmentResult,
    ExtractedEntity,
    build_enrichment_user_prompt,
    ENRICHMENT_SYSTEM_PROMPT,
)
from second_brain.services.enrichment import EnrichmentService


def _make_enrichment_result(**overrides) -> EnrichmentResult:
    """Build an EnrichmentResult with sensible defaults, overridable."""
    defaults = dict(
        intent="capture",
        clean_text="Meeting with Dave about the Reynolds project.",
        entry_type="meeting_note",
        entities=[
            ExtractedEntity(name="Dave", type="person"),
            ExtractedEntity(name="Reynolds", type="project"),
        ],
        is_open_loop=False,
        follow_up_date=None,
        tags=["meeting", "reynolds"],
        calendar_event_id=None,
    )
    defaults.update(overrides)
    return EnrichmentResult(**defaults)


class TestEnrichText:
    """Tests for EnrichmentService.enrich_text()."""

    def test_enrich_text_calls_haiku_with_correct_args(self, mock_anthropic_client):
        expected = _make_enrichment_result()
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("Meeting with Dave about the Reynolds project")

        mock_anthropic_client.call_haiku.assert_called_once()
        call_kwargs = mock_anthropic_client.call_haiku.call_args
        assert call_kwargs.kwargs["system_prompt"] == ENRICHMENT_SYSTEM_PROMPT
        assert call_kwargs.kwargs["response_model"] is EnrichmentResult
        assert "Meeting with Dave" in call_kwargs.kwargs["user_prompt"]

    def test_enrich_text_returns_enrichment_result(self, mock_anthropic_client):
        expected = _make_enrichment_result()
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("Meeting with Dave about the Reynolds project")

        assert isinstance(result, EnrichmentResult)
        assert result.intent == "capture"
        assert result.clean_text == "Meeting with Dave about the Reynolds project."
        assert result.entry_type == "meeting_note"

    def test_result_contains_all_expected_fields(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            follow_up_date="2026-03-15",
            is_open_loop=True,
            calendar_event_id="evt_123",
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("some text")

        assert result.clean_text is not None
        assert result.entry_type is not None
        assert isinstance(result.entities, list)
        assert isinstance(result.tags, list)
        assert result.is_open_loop is True
        assert result.follow_up_date == "2026-03-15"
        assert result.calendar_event_id == "evt_123"


class TestIntentClassification:
    """Tests for capture vs query intent routing."""

    def test_capture_intent(self, mock_anthropic_client):
        expected = _make_enrichment_result(intent="capture")
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("I had a great idea about the new feature")

        assert result.intent == "capture"

    def test_query_intent(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            intent="query",
            clean_text="What did Dave say about the Reynolds project?",
            entry_type="personal",
            entities=[ExtractedEntity(name="Dave", type="person")],
            tags=["query", "reynolds"],
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("What did Dave say about the Reynolds project?")

        assert result.intent == "query"


class TestEntryTypeClassification:
    """Tests for different entry types."""

    @pytest.mark.parametrize(
        "entry_type",
        ["task", "idea", "meeting_note", "project_context", "personal"],
    )
    def test_entry_types(self, mock_anthropic_client, entry_type):
        expected = _make_enrichment_result(entry_type=entry_type)
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("some text")

        assert result.entry_type == entry_type

    def test_task_entry_type(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            clean_text="Need to send the proposal to Dave by Friday.",
            entry_type="task",
            is_open_loop=True,
            follow_up_date="2026-03-06",
            tags=["proposal", "dave"],
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("need to send the proposal to dave by friday")

        assert result.entry_type == "task"
        assert result.is_open_loop is True
        assert result.follow_up_date == "2026-03-06"

    def test_idea_entry_type(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            clean_text="What if we used RAG for the knowledge base search?",
            entry_type="idea",
            entities=[ExtractedEntity(name="RAG", type="technology")],
            tags=["rag", "knowledge-base"],
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("what if we used RAG for the knowledge base search")

        assert result.entry_type == "idea"


class TestOpenLoopDetection:
    """Tests for open loop detection."""

    def test_open_loop_detected(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            clean_text="I need to follow up with Dave about the contract.",
            is_open_loop=True,
            follow_up_date="2026-03-05",
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("I need to follow up with Dave about the contract")

        assert result.is_open_loop is True
        assert result.follow_up_date == "2026-03-05"

    def test_no_open_loop(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            clean_text="The meeting went well, everyone was aligned.",
            is_open_loop=False,
            follow_up_date=None,
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("The meeting went well, everyone was aligned")

        assert result.is_open_loop is False
        assert result.follow_up_date is None


class TestCalendarEvents:
    """Tests for enrichment with calendar events context."""

    def test_with_calendar_events_passes_context(self, mock_anthropic_client):
        expected = _make_enrichment_result(calendar_event_id="evt_abc")
        mock_anthropic_client.call_haiku.return_value = expected

        calendar_events = [
            {
                "id": "evt_abc",
                "title": "Reynolds Project Sync",
                "start_time": "2026-03-01T10:00:00",
                "attendees": "Dave, Sarah",
                "description": "Weekly sync on the Reynolds project",
            }
        ]

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text(
            "Just finished the Reynolds sync with Dave",
            calendar_events=calendar_events,
        )

        call_kwargs = mock_anthropic_client.call_haiku.call_args
        user_prompt = call_kwargs.kwargs["user_prompt"]
        assert "CALENDAR EVENTS" in user_prompt
        assert "evt_abc" in user_prompt
        assert "Reynolds Project Sync" in user_prompt
        assert result.calendar_event_id == "evt_abc"

    def test_without_calendar_events(self, mock_anthropic_client):
        expected = _make_enrichment_result(calendar_event_id=None)
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("Just a random thought")

        call_kwargs = mock_anthropic_client.call_haiku.call_args
        user_prompt = call_kwargs.kwargs["user_prompt"]
        assert "CALENDAR EVENTS" not in user_prompt
        assert result.calendar_event_id is None


class TestEntityExtraction:
    """Tests for entity extraction in enrichment results."""

    def test_entities_extracted(self, mock_anthropic_client):
        expected = _make_enrichment_result(
            entities=[
                ExtractedEntity(name="Dave", type="person"),
                ExtractedEntity(name="Reynolds Electric", type="company"),
                ExtractedEntity(name="Python", type="technology"),
            ]
        )
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("some text")

        assert len(result.entities) == 3
        assert result.entities[0].name == "Dave"
        assert result.entities[0].type == "person"
        assert result.entities[1].type == "company"
        assert result.entities[2].type == "technology"

    def test_no_entities(self, mock_anthropic_client):
        expected = _make_enrichment_result(entities=[])
        mock_anthropic_client.call_haiku.return_value = expected

        service = EnrichmentService(mock_anthropic_client)
        result = service.enrich_text("Nice weather today")

        assert result.entities == []


class TestBuildEnrichmentUserPrompt:
    """Tests for the prompt builder function."""

    def test_basic_prompt(self):
        prompt = build_enrichment_user_prompt(
            raw_text="Hello world",
            current_date="2026-03-01",
        )
        assert "Hello world" in prompt
        assert "2026-03-01" in prompt
        assert "RAW TEXT:" in prompt

    def test_prompt_with_calendar_events(self):
        events = [
            {
                "id": "evt1",
                "title": "Standup",
                "start_time": "2026-03-01T09:00:00",
                "attendees": "Alice, Bob",
                "description": "Daily standup meeting",
            }
        ]
        prompt = build_enrichment_user_prompt(
            raw_text="standup notes",
            calendar_events=events,
            current_date="2026-03-01",
        )
        assert "CALENDAR EVENTS" in prompt
        assert "evt1" in prompt
        assert "Standup" in prompt
        assert "Alice, Bob" in prompt

    def test_prompt_without_calendar_events(self):
        prompt = build_enrichment_user_prompt(
            raw_text="some text",
            calendar_events=None,
        )
        assert "CALENDAR EVENTS" not in prompt
        assert "some text" in prompt

    def test_prompt_without_current_date(self):
        prompt = build_enrichment_user_prompt(raw_text="hello")
        assert "RAW TEXT:" in prompt
        assert "hello" in prompt
