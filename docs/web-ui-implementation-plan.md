# Web UI Implementation Plan

## Goals

Add a practical web management UI for SignalNest while keeping the current `supercronic`-based scheduling model.

Primary goals:
- View service status, current running job, and next scheduled run
- Manually trigger an existing schedule
- View job execution logs
- Browse all collected RSS, GitHub, and YouTube items with keyword and time filters
- View latest digest by default and browse digest history
- Generate deeper summaries for high-scoring items and manually selected items
- Improve logging
- Improve tests

Non-goals for the first version:
- Replace `supercronic`
- Full online editing of `config.yaml` or `.env`
- Complex SPA frontend or separate Node.js app
- Multi-user auth/permissions
- Deep repository code analysis for GitHub items

## Product Direction

Constraints and decisions:
- Keep `supercronic` as the scheduler
- Prefer practical internal-tool UX over polished product UI
- Reuse existing Python codepaths instead of rewriting the execution model
- Add a small web layer on top of current CLI and storage flow

Recommended stack:
- Backend: `FastAPI`
- HTML rendering: `Jinja2`
- Interaction: server-rendered pages with light progressive enhancement (`HTMX` optional)
- Storage: existing SQLite plus a new app-oriented SQLite database

## Current Architecture Summary

Current execution flow:
- `docker/entrypoint.sh` generates crontab from `config/config.yaml`
- `supercronic` runs `python -m src.main --schedule-name <name>`
- `src.main.run_schedule()` invokes the agent kernel
- Agent sessions, turns, tool calls, and session state are persisted in `data/agent_sessions.db`
- Latest and historical digest outputs are stored in:
  - `data/last_digest.json`
  - `data/history/*.json`
  - `data/outputs/latest.json`

Important gap:
- The system currently persists selected digest items, but not a queryable index of all collected items across sources.

## Target Architecture

Keep the existing runtime path and add a web application layer.

Target shape:
- `supercronic` remains responsible for scheduled execution
- Web app provides status pages, history views, logs, and manual triggers
- New app database stores web-facing operational and content indexes
- Existing `agent_sessions.db` remains the source of low-level agent execution history

Execution responsibilities:
- Scheduled runs: still started by `supercronic`
- Manual runs: started by the web app in a background worker/thread
- Deep summary jobs: started by the web app in a background worker/thread

## Current Status

Implementation status as of now:
- Phase 1 is completed
- Phase 2 is completed
- Phase 3 has not started

Currently implemented:
- FastAPI web app with server-rendered pages
- `job_runs`, `job_logs`, `digests`, `collected_items`, and `deep_summaries` in `data/app.db`
- Dashboard, jobs, digest history/detail, items, item detail, and deep summary detail pages
- Manual schedule trigger with optional `dry_run`
- Structured job logging for tracked runs
- Item indexing from tracked run session state
- Manual deep summary generation
- Web article extraction using `trafilatura` with fallback extraction
- Basic repository/service/API coverage for implemented web flows

Current limitation:
- `/items` only shows items collected by runs executed after Phase 2 was introduced, because historical digest archives do not contain full `raw_items` needed for backfilling the item index

## Data Model Plan

Use a new database, recommended path: `data/app.db`.

Reason:
- Avoid coupling web-facing state to the agent kernel's internal persistence schema
- Keep operational projections and content indexes stable even if the agent store evolves

### 1. `job_runs`

Purpose:
- Power service status, running job state, manual trigger history, and job detail pages

Suggested fields:
- `id`
- `schedule_name`
- `trigger_type` (`cron`, `manual`, `deep_summary`)
- `status` (`queued`, `running`, `succeeded`, `failed`)
- `dry_run`
- `session_id`
- `current_stage`
- `current_message`
- `error_message`
- `started_at`
- `ended_at`
- `created_at`
- `updated_at`

### 2. `job_logs`

Purpose:
- Store structured business logs for UI display

Suggested fields:
- `id`
- `job_run_id`
- `ts`
- `level`
- `component`
- `event_type`
- `message`
- `extra_json`

### 3. `collected_items`

Purpose:
- Queryable index of all collected RSS, GitHub, and YouTube items

Suggested fields:
- `id`
- `job_run_id`
- `source`
- `external_id`
- `title`
- `url`
- `author`
- `feed_title`
- `language`
- `published_at`
- `collected_at`
- `selected_for_digest`
- `ai_score`
- `ai_summary`
- `raw_json`

Notes:
- Persist items as early as possible after collection or at least after run completion from agent session state
- `raw_json` keeps source-specific fields without premature normalization

### 4. `digests`

Purpose:
- Unified store for latest and historical digests

Suggested fields:
- `id`
- `job_run_id`
- `schedule_name`
- `digest_date`
- `digest_datetime`
- `summary_text`
- `payload_json`
- `created_at`

### 5. `deep_summaries`

Purpose:
- Track deeper source fetches and generated long-form summaries

Suggested fields:
- `id`
- `item_id`
- `job_run_id`
- `trigger_type` (`auto_high_score`, `manual`)
- `status` (`queued`, `running`, `succeeded`, `failed`)
- `source_fetch_status`
- `source_content`
- `source_content_meta_json`
- `deep_summary`
- `model`
- `error_message`
- `created_at`
- `updated_at`

## Backend Module Plan

Suggested new modules:
- `src/web/app.py` - FastAPI app factory and route registration
- `src/web/templates/` - Jinja templates
- `src/web/static/` - minimal CSS/JS
- `src/web/services/status_service.py` - service status and schedule projections
- `src/web/services/job_service.py` - run creation, state transitions, log writing
- `src/web/services/digest_service.py` - latest/history digest queries
- `src/web/services/item_service.py` - item indexing and filtering
- `src/web/services/deep_summary_service.py` - source fetch and deep summary generation
- `src/web/repositories/app_db.py` - SQLite connection and migrations/bootstrap
- `src/web/repositories/*.py` - table-oriented repositories

Potential shared support modules:
- `src/logging_utils.py` - structured logging helpers
- `src/scheduler_utils.py` - next-run calculation from cron expressions
- `src/content_fetcher.py` - fetch original content for RSS/web, YouTube transcript, GitHub README

## Execution Integration Plan

### Scheduled jobs

Keep existing scheduler entrypoint, but wrap scheduled execution with app-level tracking.

Recommended integration:
- Add a service wrapper around `run_schedule()`
- Scheduled path creates a `job_run` before invoking the agent flow
- On completion, it persists digest and collected-item projections
- On failure, it records structured logs and marks `job_run` failed

Possible implementation options:
- Preferred: refactor `src.main.run_schedule()` usage behind a reusable service function
- Acceptable first step: add a thin orchestration layer called by both CLI and web

### Manual runs

Manual run flow:
1. Web request creates a `job_run` with `trigger_type=manual`
2. Background worker starts execution
3. Execution reuses the same shared orchestration path as scheduled runs
4. UI polls job status and logs

First version constraints:
- Only trigger existing schedules
- Support optional `dry_run`
- Do not support arbitrary `--query` through the UI in the first version

### Deep summary jobs

Manual deep summary flow:
1. User opens an item detail page
2. User clicks "generate deep summary"
3. Background worker fetches source content
4. Background worker generates and stores a deep summary
5. UI shows status and latest result

## Original Content Fetch Strategy

Accepted first-version scope:
- RSS and normal web articles: fetch and extract readable article text
- YouTube: transcript first, description fallback
- GitHub: repository metadata, description, topics, and README

Explicitly out of scope for first version:
- Full repository codebase analysis
- Rich media extraction pipelines
- Expensive multi-hop crawling

## UI Plan

The UI should be practical and compact, optimized for desktop first but still usable on mobile.

### 1. Dashboard `/`

Show:
- Service health
- Currently running job
- Next scheduled runs for all schedules
- Recent job outcomes
- Quick trigger controls for existing schedules

### 2. Jobs `/jobs`

Show:
- Job list
- Filters by status, trigger type, schedule, and date range

### 3. Job Detail `/jobs/{id}`

Show:
- Job metadata
- Current or final state
- Structured logs timeline
- Linked agent session information if available

### 4. Latest Digest `/digests/latest`

Show:
- Default landing page for digest browsing
- Summary text
- Items selected for that digest

### 5. Digest History `/digests`

Show:
- Historical digests
- Filters by schedule and date range

### 6. Digest Detail `/digests/{id}`

Show:
- Digest metadata
- Summary text
- Included items

### 7. Items `/items`

Show:
- All collected items
- Filters:
  - keyword
  - source
  - time range (`1d`, `7d`, `30d`, custom)
  - selected for digest
  - score range

### 8. Item Detail `/items/{id}`

Show:
- Normalized metadata
- Stored raw fields summary
- Standard AI summary
- Deep summary state and result
- Manual trigger button

### 9. Config View `/config`

Low priority.

First version:
- Read-only config inspection page

Later version:
- Possibly editable subset with validation and explicit save/reload behavior

## API Plan

Suggested first-version endpoints:

Status and schedules:
- `GET /api/status`
- `GET /api/schedules`
- `POST /api/schedules/{name}/run`

Jobs:
- `GET /api/jobs`
- `GET /api/jobs/{id}`
- `GET /api/jobs/{id}/logs`

Digests:
- `GET /api/digests/latest`
- `GET /api/digests`
- `GET /api/digests/{id}`

Items:
- `GET /api/items`
- `GET /api/items/{id}`

Deep summaries:
- `POST /api/items/{id}/deep-summary`
- `GET /api/deep-summaries/{id}`

## Logging Plan

Keep two logging layers.

### Layer 1: stdout/container logs

Purpose:
- Operational debugging through `docker logs`

Keep existing behavior and improve consistency.

### Layer 2: structured app logs in `job_logs`

Purpose:
- Human-readable logs in the web UI
- Better postmortem visibility than raw stdout alone

Recommended logged events:
- job started
- job stage changed
- source collection started/finished
- item counts per source
- summarization started/finished
- digest persisted
- dispatch started/finished
- deep summary started/finished
- failures with exception context

## Test Plan

The repository currently has no automated tests. Add tests as part of each phase instead of leaving them to the end.

### 1. Repository tests

Cover:
- `job_runs`
- `job_logs`
- `collected_items`
- `digests`
- `deep_summaries`

### 2. Service tests

Cover:
- job state transitions
- manual trigger flow
- scheduled-run projection persistence
- item filtering behavior
- deep summary state transitions

### 3. API tests

Cover:
- status endpoint
- manual trigger endpoint
- jobs list/detail/logs endpoints
- digest endpoints
- item filtering endpoint
- deep summary trigger endpoint

### 4. End-to-end dry-run tests

Use mocked collectors and mocked AI responses to validate:
- one full run creates a job record
- items are indexed
- digest is persisted
- logs are written
- failed jobs are marked correctly

## Phased Delivery Plan

### Phase 1: Operational Console

Status: Completed

Scope:
- App database bootstrap
- `job_runs` and `job_logs`
- Shared execution orchestration for scheduled and manual runs
- Dashboard
- Job list and job detail pages
- Manual trigger for existing schedules with optional `dry_run`
- Latest digest and digest history pages
- Initial tests for repositories, services, and status/job APIs

Acceptance criteria:
- User can see if a job is currently running
- User can see next scheduled run for each configured schedule
- User can manually trigger an existing schedule
- User can inspect structured logs for a run
- User can browse latest and historical digests from the UI

### Phase 2: Item Center and Manual Deep Summary

Status: Completed

Scope:
- `collected_items`
- Item indexing from job outputs
- Item list and item detail pages
- Keyword/time/source filters
- `deep_summaries`
- Manual deep summary generation for one item
- Related API and tests

Acceptance criteria:
- User can browse all collected RSS/GitHub/YouTube items
- User can filter by keyword and time range
- User can open an item and trigger a deep summary
- User can see deep summary status and output

### Phase 3: Automatic Deep Summary and Config View

Status: Pending

Scope:
- Auto-trigger deep summaries for high-scoring items
- Guardrails on count and timeout
- Read-only config view
- More detailed logging and coverage expansion

Acceptance criteria:
- High-scoring items can be deep-summarized automatically without breaking the main digest run
- Config can be inspected from the UI
- Logging and tests cover the main regression paths

## Low-Priority Follow-Up

These items are intentionally deferred until a later refactor cycle.

- Further align the web UI with the original digest HTML styling and spacing
- Reduce template drift by extracting shared presentational styles or shared render components
- Revisit whether digest-detail pages can reuse more of the original digest rendering path directly

## Migration Notes

Recommended sequence for code changes:
1. Introduce `data/app.db` and schema bootstrap
2. Introduce shared orchestration around scheduled runs
3. Add structured log writing
4. Add FastAPI app with status and jobs pages
5. Add digest pages
6. Add item indexing and item pages
7. Add manual deep summary
8. Add automatic deep summary

Compatibility guidance:
- Do not remove current JSON outputs in early phases
- Keep `data/agent_sessions.db` untouched as the agent's internal store
- Treat app DB tables as web-facing projections built from the main run outputs

## Open Questions

Resolved decisions:
- UI style: practical first
- Scheduler: keep `supercronic`
- Deep-summary source scope: limited first version is acceptable

Still optional for later discussion:
- Whether to expose a read-only config page in Phase 1 or Phase 3
- Whether the web app runs in the same container or a second service/container
- Whether to use plain server-rendered pages only or add `HTMX` for partial updates

## Recommended First Build Slice

Start with the smallest meaningful vertical slice:
- app DB bootstrap
- `job_runs`
- `job_logs`
- shared execution wrapper for scheduled/manual runs
- dashboard page
- manual trigger for existing schedules with `dry_run`

This slice validates the core architecture before item indexing and deep-summary work.
