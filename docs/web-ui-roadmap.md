# Web UI Roadmap

## Purpose

This document tracks web UI work that is not yet fully implemented, intentionally deferred, or worth revisiting in a later refactor.

## Remaining Work

### Job Runtime Refactor

- Replace direct `supercronic -> python -m src.main` execution with an app-level queued job model
- Introduce a durable worker process that claims queued jobs and owns all `running -> terminal` transitions
- Add lease-based liveness fields to `job_runs`: `worker_id`, `claimed_at`, `heartbeat_at`, `lease_expires_at`, `attempt`, `idempotency_key`, and `scheduled_for`
- Treat expired `running` jobs as `lost` and surface that state explicitly in the UI
- Move manual triggers onto the same queue path so web requests never execute digest jobs directly
- Refactor `run_tracked_schedule()` into a worker-owned execution path instead of a cron/web dual-use helper

### Scheduler Replacement

- Replace shell-generated crontab management with an internal scheduler loop that reads `config.yaml` and enqueues due schedule slots
- Add deterministic idempotency keys per schedule slot to avoid double-enqueue on restart
- Persist enough scheduler cursor state to recover cleanly after restarts without skipping or replaying jobs
- Simplify deployment modes around `web`, `worker`, and optional dedicated `scheduler`

### Item Browser Improvements

- Add custom date-range filtering for `/items`
- Add score-range filtering for `/items`
- Show the earliest available indexed item date in the UI so users understand the historical backfill boundary

### Re-entrancy Hardening

- Tighten schedule-level duplicate-run protection to reduce the current TOCTOU window
- Prefer a more atomic create-if-not-active write path inside SQLite for queued job creation and worker claims

### Content Fetching Improvements

- Improve GitHub README fetching to better handle default branch differences and edge cases
- Add source-content caching to avoid repeated deep-summary fetches for the same item
- Add more resilient retry handling around source fetch failures

## Low-Priority Follow-Up

- Further align the web UI with the original digest HTML styling and spacing
- Reduce template drift by extracting shared presentational styles or shared render components
- Revisit whether digest-detail pages can reuse more of the original digest rendering path directly

## Potential Future Enhancements

- Add `HTMX` or similar partial-update behavior for live job refresh and smoother log viewing
- Consider moving from SQLite to PostgreSQL if the web layer ever needs horizontal scaling or stronger concurrency guarantees
- Consider a historical item backfill path only if a trustworthy raw-data source becomes available
