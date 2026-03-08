# Slack Conversation History for Query Context — Requirements Specification

## Overview

Replace the in-memory `QuerySessionManager` (single Q&A exchange, 10-minute timeout) with Slack channel history. Before answering a query, the bot fetches the last N messages from the channel and includes them as conversation context in the LLM prompt.

## Core Mechanics

### History Fetching
- Use `conversations.history(channel=<channel_from_event>, limit=<conversation_history_messages>)` to fetch recent messages
- Use the channel ID from the incoming message event, not the configured `SLACK_CHANNEL_ID`
- Include all messages (user and bot) — no filtering by message type
- Format as a conversation transcript in the prompt:
  ```
  CONVERSATION HISTORY:
  You: <user message>
  Assistant: <bot response>
  You: <user message>
  ...
  ```

### Token Management
- Total conversation history is capped at `conversation_history_max_chars` characters (default: 1000)
- Bot responses within history are truncated to `conversation_history_bot_truncate_chars` characters each (default: 200)
- User messages are included at full length (they are typically short)
- When the cap is reached, older messages are trimmed first (most recent messages preserved)

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `conversation_history_messages` | 10 | Number of messages to fetch from Slack |
| `conversation_history_max_chars` | 1000 | Max total characters for history in prompt |
| `conversation_history_bot_truncate_chars` | 200 | Max characters per bot message in history |

These replace the removed `query_session_timeout_minutes` setting.

## Integration Points

### Handlers (`message.py`, `commands.py`)
- Fetch Slack history using `context["services"]["slack_client"]`
- Extract channel ID from the incoming event (not from config)
- Pass history as `list[dict]` to `query_engine.handle_query()`:
  ```python
  [
      {"role": "user", "text": "..."},
      {"role": "assistant", "text": "..."},
  ]
  ```
- Both free-form message queries and `/ask` command queries receive conversation history

### Query Engine (`query_engine.py`)
- `handle_query()` accepts a new `conversation_history: list[dict] | None` parameter
- Replaces the existing `session_context: QuerySession | None` parameter
- `_build_user_prompt()` renders history as a `CONVERSATION HISTORY:` section, replacing `PREVIOUS QUERY CONTEXT:`
- Query engine remains Slack-agnostic — it receives pre-formatted message dicts

### Prompt Changes
- Replace `PREVIOUS QUERY CONTEXT:` block with `CONVERSATION HISTORY:` block in the user prompt
- The `CONVERSATION HISTORY:` section is placed before `CALENDAR EVENTS:` and `KNOWLEDGE BASE ENTRIES:`

## Removals

| Item | Location | Action |
|------|----------|--------|
| `QuerySessionManager` class | `services/query_session.py` | Delete file |
| `QuerySession` dataclass | `services/query_session.py` | Delete file |
| Service registration | `main.py` (`build_services`) | Remove `query_session_manager` block |
| Session reads/updates | `bot/handlers/message.py` | Remove session manager usage |
| Session reads/updates | `bot/handlers/commands.py` | Remove session manager usage |
| `session_context` parameter | `services/query_engine.py` | Replace with `conversation_history` |
| `query_session_timeout_minutes` | `config.py` (`CONFIG_DEFAULTS`) | Remove key |

## Edge Cases

| Case | Behavior |
|------|----------|
| Empty channel (first message) | No history passed, query works as today |
| Bot nudges/briefs fill history | Included as context — useful for relevance |
| Long bot responses in history | Truncated to `conversation_history_bot_truncate_chars` |
| Total history exceeds char cap | Oldest messages trimmed first |
| DMs to bot | Works — channel ID comes from the event |

## Testing Requirements

### Unit Tests for History Fetching
- Test formatting of Slack messages into `[{"role": ..., "text": ...}]` dicts
- Test bot message truncation at configured limit
- Test total character cap with oldest-first trimming
- Test empty history (no messages)
- Test mixed user/bot messages ordering

### Unit Tests for Query Engine
- Test `_build_user_prompt` with conversation history present
- Test `_build_user_prompt` with no conversation history (None)
- Test that history appears before knowledge base entries in prompt

### Integration Tests
- Test end-to-end query with mocked Slack `conversations.history` response
- Test `/ask` command with mocked history

## Existing Patterns to Follow
- Service access via `context["services"]` dict (see `bot/handlers/message.py`)
- Config defaults in `config.py:CONFIG_DEFAULTS` dict
- Slack API calls use the async `slack_client` from services
- Tests use `pytest` with `pytest-asyncio`, mocking with `unittest.mock`
- Test fixtures in `tests/conftest.py`
