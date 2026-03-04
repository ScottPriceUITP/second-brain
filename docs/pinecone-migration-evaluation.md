# Pinecone Migration Evaluation for Second Brain

## Current Architecture

The app uses **SQLite + FTS5** (full-text search) as the database layer:

- **11 tables** with relational data (entries, entities, tags, relations, calendar events, nudge history, config)
- **BM25-ranked keyword search** via FTS5 virtual table for all retrieval
- An `embedding` column exists on `Entry` but is **unused** — no vector embeddings are generated or queried today
- ~30 entries are pulled as context for query answering, found via FTS keyword matching + entity-graph expansion
- All data is local in a single SQLite file

## What "Migrating to Pinecone" Would Actually Mean

Pinecone is a **vector database** — it stores embeddings and performs similarity search. It is **not** a general-purpose relational database.

**Pinecone would NOT replace SQLite.** It would **supplement** it. SQLite is still needed for:

- Relational data (entities, tags, entry_relations, junction tables)
- Calendar events, nudge history, config
- Status tracking, follow-up dates, entry metadata
- Entity resolution and merge chain logic

**Pinecone would replace FTS5** for the search/retrieval layer, giving semantic search instead of keyword search.

## What the Migration Involves

1. **Embedding generation** — Add a step in the enrichment pipeline to generate embeddings (e.g., via OpenAI `text-embedding-3-small` or a Pinecone integrated model) for each entry's `clean_text`
2. **Pinecone index setup** — Create a serverless index with appropriate dimensions (e.g., 1536 for OpenAI embeddings)
3. **Upsert pipeline** — After enrichment, upsert the vector + metadata (entry_id, entry_type, entity names, tags, status, dates) to Pinecone
4. **Query rewrite** — Replace FTS5 queries in `QueryEngine` and `ConnectionScoringService` with Pinecone similarity search + metadata filters
5. **Backfill** — Generate embeddings for all existing entries and bulk upsert
6. **Dependency addition** — Add `pinecone` Python SDK

## Pros

| Pro | Details |
|-----|---------|
| **Semantic search** | Finds conceptually related entries even without keyword overlap. "What did I discuss about scaling the team?" would match entries about "hiring plans" or "headcount growth" — FTS5 would miss these |
| **Better connection scoring** | Vector similarity provides a natural relevance score, potentially reducing or replacing the LLM-based connection scoring (saving Haiku API calls) |
| **Improved query context** | The query engine would retrieve more relevant entries, leading to better answers from Claude |
| **Zero-ops infrastructure** | Fully managed, auto-scaling, no tuning required |
| **Fast** | ~7ms p99 query latency at low scale |
| **Metadata filtering** | Can filter by entry_type, status, date ranges, entities — combining semantic + structured search |
| **Hybrid search** | Supports both dense (semantic) and sparse (keyword/BM25) vectors with configurable blending (`alpha` parameter) |
| **Free tier available** | Starter plan: 2GB storage, 2M write units/month, 1M read units/month — likely sufficient for a personal knowledge base |

## Cons

| Con | Details |
|-----|---------|
| **Added complexity** | Two data stores to keep in sync (SQLite + Pinecone). Upserts must happen atomically with DB writes, or you risk drift |
| **Embedding cost** | Every entry needs an embedding generated via an external API. OpenAI's `text-embedding-3-small` is ~$0.02/1M tokens, but it's another API dependency |
| **Vendor lock-in** | Pinecone is closed-source SaaS. No self-hosting option. If they change pricing or go down, you're stuck |
| **Network dependency** | Currently the app works with a local SQLite file. Adding Pinecone means search requires internet connectivity |
| **Overkill for current scale?** | A personal knowledge base likely has hundreds to low thousands of entries. FTS5 handles this fine. Semantic search shines at 10K+ entries |
| **Free tier limitations** | Indexes pause after 3 weeks of inactivity. Only AWS us-east-1. Limited to 1 project, 2 users |
| **Doesn't replace SQLite** | Still need relational storage. This is purely additive infrastructure |
| **Write latency** | Each new entry now requires an embedding API call + Pinecone upsert, adding ~200-500ms to the enrichment pipeline |
| **Eventually consistent** | Slight delay before new/changed records are visible to queries |
| **Cold starts** | Serverless deployments can have higher latency on first query after inactivity |

## Pinecone Pricing (as of early 2026)

| Plan | Price | Key Details |
|------|-------|-------------|
| **Starter (Free)** | $0 | 5 indexes, 2GB storage, 2M writes/month, 1M reads/month. AWS us-east-1 only. Pauses after 3 weeks inactivity. |
| **Standard** | From $25-50/month | Pay-as-you-go. Read units ~$16/million. HIPAA add-on available ($190/month). |
| **Enterprise** | Custom | Read units ~$24/million. HIPAA included. Higher compliance and support. |
| **Dedicated (BYOC)** | Custom | Your own cloud account. Custom compliance arrangements. |

## Alternatives Worth Considering

| Alternative | Why Consider It |
|-------------|-----------------|
| **sqlite-vss** | Add vector search to existing SQLite DB. No new infrastructure. Uses the existing `embedding` column. Good for <100K vectors. |
| **Chroma (embedded)** | Runs in-process like SQLite. Zero infrastructure. Great for prototyping. Handles embedding generation too. |
| **pgvector (PostgreSQL)** | If you outgrow SQLite, PostgreSQL + pgvector gives relational + vector in one DB. Eliminates sync issues. |
| **Qdrant** | Open-source, Rust-based. Can run locally or as managed cloud. Faster benchmarks than Pinecone (8ms p50 vs 20ms). More control, lower cost. |
| **Turbopuffer** | Serverless like Pinecone but cheaper. Supports both vector and BM25 search. $64/month minimum though. |
| **Improve FTS5** | Use Claude to expand queries into keyword variants before FTS5 search. Much simpler, no new infrastructure. |

## Recommendation

For a **personal knowledge base** at current scale, Pinecone is likely **premature**. A phased approach:

1. **Short term**: Improve retrieval by using Claude to expand queries before FTS5 search (generate keyword variants)
2. **Medium term**: Add embedded vector search via **sqlite-vss** or **Chroma** — no new infrastructure, uses the existing `embedding` column
3. **Long term**: If the app outgrows SQLite entirely, migrate to **PostgreSQL + pgvector** for a single unified database with both relational and vector capabilities

Pinecone becomes the right choice if scaling to **thousands of users** or **millions of entries** where managed infrastructure and performance guarantees matter.
