# Second Brain — Requirements Document

## Project Overview

A personal AI-powered knowledge management system ("second brain") that allows the user to verbally or textually capture notes, ideas, tasks, and context via Slack, store and enrich them intelligently, and receive proactive nudges when relevant information should be surfaced.

---

## Goals

- Capture any type of personal/professional knowledge through a single low-friction interface (Slack)
- Automatically enrich entries with entities, classifications, and connections to existing knowledge
- Surface relevant past entries proactively at the right moment without requiring the user to search
- Provide calendar-aware context before meetings
- Remain simple, self-contained, and cheap to run

---

## User

- Single user (Scott)
- Personal Slack org (to be created, separate from work)
- Technical background — comfortable with Python, APIs, cloud deployment

---

## Interface

### Slack Channels
- **`#second-brain` channel** — primary capture surface. User posts voice-to-text or typed notes here. Bot reacts with ✅ on routine captures. Only replies in text if strong connections to existing entries are found.
- **DMs with the bot** — proactive nudges from the bot appear here. Natural back-and-forth for queries and follow-ups. Replies in DMs are captured back into the brain.

### Input Methods
- Slack mobile voice-to-text (primary)
- Typed Slack messages
- Future: email forwarding (deferred to v2)

---

## Entry Types

All of the following should be captured and classified automatically:

- Tasks / todos
- Ideas / insights
- Meeting notes
- Project context
- Personal notes

---

## Entry Enrichment (on every capture)

When a new entry arrives, Claude should:

1. Clean and summarize the raw text
2. Extract entities: people, companies, projects, technologies
3. Classify entry type (from list above)
4. Flag as open loop if it implies a task or follow-up
5. Find and link related existing entries
6. Associate with a current or recent calendar event if timing suggests it

---

## Data Model

### Entries
- `id`, `created_at`, `updated_at`
- `raw_text`, `clean_text`
- `entry_type` (idea / task / meeting / project_context / personal)
- `status` (open / resolved / archived)
- `source` (slack)
- `follow_up` (boolean), `follow_up_date`
- `slack_ts` (for threading)

### Entities
- `id`, `name`, `type` (person / company / project / technology)
- Many-to-many relationship with entries

### Entry Relations (graph layer)
- `from_entry`, `to_entry`, `relation` (related / follow_up_of / contradicts / resolves)

### Tags
- Flexible labeling, many-to-many with entries

### Search
- Full-text search via SQLite FTS
- No vector embeddings in v1 — Claude's context window used for semantic reasoning over search results instead

---

## Bot Behavior

### Capture Mode (`#second-brain`)
- Enrich and store entry silently
- React with ✅
- Post a text reply only if 2+ strong connections to existing entries found
- Auto-associate with calendar event if timing matches

### Query Mode (DM or @mention)
- Accept natural language queries
- Use full-text search + recent entries as context
- Claude synthesizes a coherent answer, not a raw data dump
- Examples: "What do I have on Reynolds?", "What are my open loops this week?"

### Reply Handling (DM replies to nudges)
- Determine intent: resolving, adding a note, or new capture
- Route accordingly — updates existing entry or creates new one

---

## Proactive Scheduler

- Runs every 2 hours, weekdays 8am–6pm (user's timezone)
- Calls `get_open_loops` + `get_brain_summary` + upcoming calendar events
- Passes to Claude with strict prompt: only surface something if it is genuinely time-sensitive, overdue, or a connection the user would want to know about. If nothing qualifies, stay silent.
- Most runs result in no message (Claude acts as the filter)
- When firing: single focused DM, not a list

### Pre-meeting Brief (calendar-triggered)
- Each morning, check calendar for meetings in next 24 hours
- Query brain for entries related to attendees, companies, or meeting title
- If relevant entries exist, DM user a pre-meeting brief
- If nothing relevant, stay silent

---

## Google Calendar Integration

- Read-only access via Google Calendar API
- OAuth2 authentication, token stored in Fly.io secrets
- Used for:
  - Pre-meeting briefs (morning scheduler)
  - Contextual tagging of captures (associate note with recent/current meeting)
  - Load-aware nudging (avoid interrupting on heavy meeting days)

---

## Storage

- **SQLite** — single file, plain (no vector extensions)
- Persisted on a Fly.io volume
- Backed up via standard Fly volume snapshots

---

## LLM Usage

| Task | Model |
|---|---|
| Entry enrichment | Claude Haiku (fast, cheap) |
| Query synthesis | Claude Haiku |
| Proactive reasoning / scheduler | Claude Sonnet (needs judgment) |
| Pre-meeting brief generation | Claude Sonnet |

- API key from Anthropic Console (separate from Team seat subscription)
- Estimated cost: $1–5/month at personal brain scale

---

## Tech Stack

| Layer | Technology |
|---|---|
| Interface | Slack (personal org) |
| Bot runtime | Slack Bolt for Python |
| LLM | Anthropic API (Haiku + Sonnet) |
| Calendar | Google Calendar API (read-only) |
| Storage | SQLite |
| Hosting | Fly.io (single container + persistent volume) |

---

## Deployment

- Single Python process (Slack bot + APScheduler)
- Dockerfile + `fly.toml`
- SQLite file on a mounted Fly volume
- Secrets: `ANTHROPIC_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `GOOGLE_OAUTH_TOKEN`

---

## Out of Scope (v1)

- Email integration (deferred to v2)
- Obsidian sync
- MCP server
- Multi-user support
- Vector embeddings / pgvector
- PostgreSQL
- Local/on-prem hosting

---

## Build Order

1. SQLite schema
2. Core bot with capture → enrich → store pipeline
3. Query mode in DMs
4. Proactive scheduler (open loops)
5. Google Calendar integration + pre-meeting briefs
6. Calendar-aware contextual tagging on capture
