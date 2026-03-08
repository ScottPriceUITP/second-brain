"""Tests for fetch_conversation_history — Slack history parsing and truncation."""

import pytest
from unittest.mock import AsyncMock

from second_brain.bot.history import fetch_conversation_history


@pytest.fixture
def mock_slack_client():
    client = AsyncMock()
    client.conversations_history.return_value = {"messages": []}
    return client


class TestFetchConversationHistory:

    @pytest.mark.asyncio
    async def test_formats_user_and_bot_messages(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "bot reply", "bot_id": "B123", "ts": "2"},
                {"text": "hello", "user": "U123", "ts": "1"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000
        )
        assert result == [
            {"role": "user", "text": "hello"},
            {"role": "assistant", "text": "bot reply"},
        ]

    @pytest.mark.asyncio
    async def test_bot_message_truncation(self, mock_slack_client):
        long_text = "x" * 300
        mock_slack_client.conversations_history.return_value = {
            "messages": [{"text": long_text, "bot_id": "B123", "ts": "1"}]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", bot_truncate_chars=50, max_chars=5000
        )
        assert len(result) == 1
        assert len(result[0]["text"]) == 53  # 50 + "..."
        assert result[0]["text"].endswith("...")

    @pytest.mark.asyncio
    async def test_total_char_cap_trims_oldest(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "newest msg", "user": "U1", "ts": "3"},
                {"text": "middle msg", "user": "U1", "ts": "2"},
                {"text": "oldest msg", "user": "U1", "ts": "1"},
            ]
        }
        # max_chars=20 should keep only the newest message(s)
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=20
        )
        # "newest msg" is 10 chars, "middle msg" is 10 chars = 20 total
        assert len(result) <= 2
        # The oldest should have been trimmed
        texts = [m["text"] for m in result]
        assert "oldest msg" not in texts
        assert "newest msg" in texts

    @pytest.mark.asyncio
    async def test_empty_history(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {"messages": []}
        result = await fetch_conversation_history(mock_slack_client, "C123")
        assert result == []

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self, mock_slack_client):
        mock_slack_client.conversations_history.side_effect = Exception("API error")
        result = await fetch_conversation_history(mock_slack_client, "C123")
        assert result == []

    @pytest.mark.asyncio
    async def test_messages_reversed_to_chronological(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "third", "user": "U1", "ts": "3"},
                {"text": "second", "user": "U1", "ts": "2"},
                {"text": "first", "user": "U1", "ts": "1"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000
        )
        assert [m["text"] for m in result] == ["first", "second", "third"]

    @pytest.mark.asyncio
    async def test_excludes_message_by_ts(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "current query", "user": "U1", "ts": "1234.5678"},
                {"text": "older message", "user": "U1", "ts": "1234.0000"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000, exclude_latest_ts="1234.5678"
        )
        assert len(result) == 1
        assert result[0]["text"] == "older message"

    @pytest.mark.asyncio
    async def test_filters_system_subtypes(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "real message", "user": "U1", "ts": "3"},
                {"text": "joined #channel", "subtype": "channel_join", "ts": "2"},
                {"text": "set topic", "subtype": "channel_topic", "ts": "1"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000
        )
        assert len(result) == 1
        assert result[0]["text"] == "real message"

    @pytest.mark.asyncio
    async def test_skips_empty_messages(self, mock_slack_client):
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "real message", "user": "U1", "ts": "2"},
                {"text": "", "user": "U1", "ts": "1"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000
        )
        assert len(result) == 1
        assert result[0]["text"] == "real message"

    @pytest.mark.asyncio
    async def test_keeps_bot_messages_with_subtype(self, mock_slack_client):
        """Bot messages have subtype='bot_message' but should be kept."""
        mock_slack_client.conversations_history.return_value = {
            "messages": [
                {"text": "bot reply", "bot_id": "B123", "subtype": "bot_message", "ts": "1"},
            ]
        }
        result = await fetch_conversation_history(
            mock_slack_client, "C123", max_chars=5000
        )
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
