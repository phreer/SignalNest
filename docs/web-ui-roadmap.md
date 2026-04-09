# Web UI Roadmap

## Purpose

This document tracks web UI work that is not yet fully implemented, intentionally deferred, or worth revisiting in a later refactor.

## Remaining Work

### Job Runtime Refactor

- Tighten queued job creation so duplicate-run protection becomes atomic instead of the current check-then-insert flow
- Add explicit cancellation and retry semantics on top of the current queued/leased job model
- Surface `lost`, retryable, and cancelled states more clearly in the UI

### Scheduler Replacement

- Persist an explicit scheduler cursor/checkpoint so recovery does not rely only on the latest `job_runs.scheduled_for` value
- Improve missed-slot catch-up policy for longer outages and make it configurable per deployment
- Decide whether to keep the scheduler embedded in the worker by default or split it into a dedicated long-running process role

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
