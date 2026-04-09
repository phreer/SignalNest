# Web UI

## Overview

SignalNest now includes a built-in web console for operational visibility and content browsing.

Today the project still uses a `supercronic -> python -m src.main` execution model for scheduled runs. That model is simple, but it leaves the web UI without a durable ownership relationship to the real executor process. If a scheduled child process is killed unexpectedly, the corresponding `job_runs` row can remain stuck in `running` because no process is left alive to write the terminal state.

The long-term direction documented here is to replace that model with a durable `web + worker` architecture where scheduling enqueues jobs and workers claim them with leases and heartbeats.

Current implementation status:
- Operational console: implemented
- Digest history and detail views: implemented
- Item browser and manual deep summaries: implemented
- Automatic deep summaries: implemented
- Read-only config view: implemented

The web layer is built with:
- `FastAPI`
- Server-rendered `Jinja2` templates
- SQLite app state in `data/app.db`

## Runtime Model

### Current Runtime

SignalNest supports these container runtime modes through `RUN_MODE`:
- `all`: web UI + scheduler in one container
- `cron`: scheduler only
- `web`: web UI only
- `once`: run one schedule and exit

Recommended deployment is single-container mode:
- `RUN_MODE=all`

In this mode:
- `supercronic` remains responsible for scheduled execution
- the web UI runs in the same container
- both processes share the same local `data/` directory and SQLite files

Related files:
- `docker/entrypoint.sh`
- `docker/docker-compose.yml`

### Target Runtime

The planned runtime model removes `supercronic` from the critical path and replaces direct process spawning with a durable job queue managed in the application database.

Target process roles:
- `web`: UI, APIs, manual trigger requests, read-only operational views
- `scheduler`: computes due schedules from `config.yaml` and enqueues jobs
- `worker`: claims queued jobs, executes them, refreshes heartbeats, and writes terminal states

Recommended deployment shapes:
- small self-hosted deployment: one `web` process and one `worker` process, with the scheduler loop embedded in the worker process
- clearer separation: independent `web`, `scheduler`, and `worker` processes

The critical change is that scheduling will enqueue work instead of directly executing `python -m src.main`. Workers become the only component allowed to transition a queued job into `running`.

## Refactor Plan

### Why Refactor

The current scheduler model has three architectural weaknesses:
- the job state shown by the web UI is projection-only; it is not backed by a durable executor lease
- a killed child process cannot mark its own job as failed or cancelled, so `running` can become permanently stale
- manual runs and scheduled runs do not yet share a single queueing and claiming protocol

The refactor goal is to make job status derived from durable ownership rather than best-effort cleanup logic.

### Target Execution Contract

All run sources must follow the same lifecycle:
1. A caller creates or enqueues a job record.
2. A worker atomically claims the job.
3. The worker executes the existing schedule orchestration.
4. The worker refreshes a heartbeat while it still owns the job.
5. The worker writes a terminal state when execution finishes.
6. If the worker disappears, the lease expires and the job is recovered as `lost` instead of remaining `running` forever.

### Process Responsibilities

#### Web

- render dashboards, jobs, digests, items, and deep summaries
- accept manual trigger requests and insert queued jobs
- never execute digest jobs inside request threads
- treat `running` as valid only when the active lease has not expired

#### Scheduler

- compute next due times from `config.yaml`
- enqueue scheduled jobs using a deterministic idempotency key
- never execute jobs directly
- tolerate restarts without double-enqueuing the same scheduled slot

#### Worker

- atomically claim the next runnable job
- set `worker_id`, `claimed_at`, `heartbeat_at`, and `lease_expires_at`
- execute `run_schedule()` through a worker-owned orchestration path
- run a background heartbeat loop independent of tool progress callbacks
- write `succeeded`, `failed`, `cancelled`, or `lost` terminal states

### Data Model Changes

The current `job_runs` table should evolve into the durable source of truth for job execution. The minimal planned additions are:

- `scheduled_for`: timestamp for the logical schedule slot being executed
- `worker_id`: identifier of the worker currently holding the lease
- `heartbeat_at`: latest heartbeat timestamp from the worker
- `lease_expires_at`: deadline after which the worker lease is considered dead
- `claimed_at`: timestamp when the worker claimed the job
- `attempt`: retry counter starting from `1`
- `idempotency_key`: unique key for deduplicating schedule submissions
- `final_reason`: normalized end-state reason such as `completed`, `worker_lost`, `cancel_requested`, `dispatch_required_missing`

Recommended status set:
- `queued`
- `running`
- `succeeded`
- `failed`
- `lost`
- `cancelled`

Recommended unique constraints:
- unique `idempotency_key` for scheduled runs
- at most one active queued/running job per schedule when overlap is disallowed

### Lease and Heartbeat Semantics

The web UI must stop treating `status='running'` as sufficient proof that a job is alive.

Planned rules:
- a job is considered actively running only when `status='running'` and `lease_expires_at > now`
- workers refresh `heartbeat_at` and extend `lease_expires_at` on a short interval, for example every 5 to 15 seconds
- the heartbeat must not rely only on agent tool events because a long LLM call or slow network operation can leave progress silent for a while
- when a worker starts, it should sweep expired `running` jobs and convert them to `lost`
- the dashboard should distinguish `running` from `lost` so operator action is obvious

### Queueing and Claiming

Manual and scheduled runs should be unified behind the same enqueue path.

Planned behavior:
- manual trigger: insert a `queued` job and return its `job_run_id`
- scheduled trigger: insert the same `queued` job shape with a deterministic `idempotency_key`
- worker claim: atomically update one eligible `queued` job into `running` if it is still unclaimed
- retries: a future retry policy can requeue `failed` or `lost` jobs by creating a new attempt or by explicitly rescheduling them

This removes the current split where web manual runs use a `ThreadPoolExecutor` while cron runs spawn a separate Python process.

### Scheduler Design

The scheduler should become a long-running loop instead of a shell-generated crontab.

Planned behavior:
- load schedules from `config.yaml`
- calculate due schedule slots using the configured timezone and cron expressions
- enqueue jobs for missed or current due slots using unique idempotency keys such as `<schedule_name>:<scheduled_for_iso>`
- maintain a persisted cursor or last-enqueued marker per schedule so restarts do not skip or replay work incorrectly

The scheduler does not need to be complicated. A single polling loop every 15 to 30 seconds is sufficient at current scale.

### Reuse of Existing Code

The existing agent and digest orchestration should mostly stay intact.

Expected reuse:
- `src.main.run_schedule()` remains the business execution entry point
- `src.web.runtime.run_tracked_schedule()` should be refactored into a worker-owned execution function instead of being called from cron and web request code directly
- existing progress events from `src.agent.kernel` can continue feeding `job_logs`
- existing digest and item projection logic can remain after the worker successfully finishes a job

### Migration Stages

#### Stage 1: Lease-Aware Job Model

- extend `job_runs` with lease and worker ownership fields
- update running-job queries so expired leases are not treated as active
- add recovery logic that marks expired `running` jobs as `lost`
- keep the current scheduler temporarily so stale `running` jobs stop blocking the UI

Acceptance criteria:
- a killed run is eventually shown as `lost` instead of `running`
- duplicate-run protection ignores expired leases

#### Stage 2: Unified Queue for Manual Runs

- replace `ThreadPoolExecutor`-backed manual execution with `queued -> claimed by worker`
- introduce a worker process that polls the queue and runs jobs
- keep cron only as a producer that enqueues jobs instead of executing them directly

Acceptance criteria:
- manual runs and scheduled runs share one execution path after enqueue
- web requests return quickly and no longer own long-running work

#### Stage 3: Replace Supercronic with Internal Scheduler

- add a durable scheduler loop inside a dedicated process role
- remove crontab generation from `docker/entrypoint.sh`
- retire `RUN_MODE=cron` in favor of worker/scheduler-oriented modes

Acceptance criteria:
- scheduled runs are created only through the app-level queue
- no scheduled digest run depends on shell-generated child processes anymore

#### Stage 4: Operational Refinement

- add retry and cancellation semantics where useful
- surface `lost`, retryable, and cancelled states clearly in the UI
- consider PostgreSQL only if deployment grows beyond single-host SQLite comfort

Acceptance criteria:
- operators can distinguish infrastructure loss from business-logic failure
- job recovery behavior is explicit and observable

## Storage

The web console uses `data/app.db` for web-facing state.

Main tables:
- `job_runs`
- `job_logs`
- `digests`
- `collected_items`
- `deep_summaries`

The agent's internal execution store remains separate:
- `data/agent_sessions.db`

SQLite connections are hardened with:
- `PRAGMA journal_mode=WAL`
- `PRAGMA busy_timeout=5000`
- `PRAGMA foreign_keys = ON`

## Implemented Features

### Status and Jobs

Implemented pages:
- `/`
- `/jobs`
- `/jobs/{id}`

Implemented behavior:
- current running job visibility
- next scheduled run projection from cron expressions
- recent job history
- structured job logs
- job pipeline timeline and tool-level execution details
- manual trigger for existing schedules
- duplicate schedule trigger rejection when a queued/running job already exists

### Digests

Implemented pages:
- `/digests/latest`
- `/digests`
- `/digests/{id}`

Implemented behavior:
- latest digest redirect
- digest history browsing
- digest detail rendering aligned with the project digest visual style
- linking digest items back to indexed item detail pages when available
- archive sync from `data/outputs/digest_*.json`

### Items

Implemented pages:
- `/items`
- `/items/{id}`

Implemented behavior:
- indexing collected items from tracked run session state
- source coverage for RSS, GitHub, and YouTube
- filters for:
  - keyword
  - source
  - time range: `1d`, `7d`, `30d`
  - selected-for-digest only
- item detail view with normalized metadata and raw payload display

### Deep Summaries

Implemented pages:
- `/deep-summaries/{id}`

Implemented behavior:
- manual deep summary trigger from item detail pages
- automatic deep summary generation for high-scoring items after successful digest runs
- exclusion rules and per-run cap from config
- timeout guard for automatic deep summary runs
- deep summary failures do not fail the parent digest job

### Config View

Implemented pages:
- `/config`

Implemented API:
- `GET /api/config`

Implemented behavior:
- read-only configuration inspection
- masking of sensitive values
- env override annotation for selected config fields

## APIs

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

Config:
- `GET /api/config`

## Deep Summary Source Fetching

Current source fetch strategy:
- RSS/web pages:
  - use `trafilatura` first for article extraction
  - fallback to `requests + BeautifulSoup` text extraction
  - fallback again to stored item snippets when needed
- YouTube:
  - transcript first
  - description fallback
- GitHub:
  - README first
  - metadata fallback

Out of scope in the current implementation:
- full repository code analysis
- multi-hop crawling
- advanced content caching

## Configuration

Automatic deep summary behavior is controlled by `config.yaml`:

```yaml
deep_summary:
  auto_enabled: false
  score_threshold: 8
  max_per_run: 5
  timeout_per_item: 120
  exclude_sources: []
```

## Tests

Current automated coverage includes:
- web repository/store behavior
- tracked run persistence
- item indexing and filters
- manual deep summary flow
- WAL mode behavior
- re-entrancy guard behavior
- automatic deep summary behavior
- config API masking and rendering

Relevant test files:
- `tests/test_web_phase1.py`
- `tests/test_web_phase3.py`

## Known Limitations

- `/items` only includes runs executed after item indexing was introduced; historical digest archives do not contain enough raw data to reconstruct all prior collected items
- item filters do not yet support custom date ranges or score-range filtering
- the schedule re-entrancy guard still has a narrow TOCTOU window because the active-job check and insert are not fully atomic in one SQL statement
- scheduled execution still depends on `supercronic` spawning one-shot Python processes; this is the root cause behind stale `running` jobs after unexpected process death and is the main architecture refactor target
- the web UI and digest HTML are visually closer now, but template sharing is not yet unified

## Key Files

- `src/web/app.py`
- `src/web/runtime.py`
- `src/web/store.py`
- `src/web/content.py`
- `src/web/templates/`
- `src/web_main.py`
- `docker/entrypoint.sh`
- `docker/docker-compose.yml`
