# Agent Notes: SES Bounceback Tooling

## Purpose

This repo maintains a local SQLite-backed pipeline for SES bounceback processing and suppression seeding.

## Architecture

- `fetch_bouncebacks.py`
  - IMAP/Gmail ingest
  - SES bounce JSON extraction
  - normalized bounce/recipient storage
  - report and export commands
- `aws_suppression_sync.py`
  - reads suppression candidates from SQLite
  - writes them to the SES account-level suppression list
  - records success/failure locally
- `bounceback_store.py`
  - shared schema, counts, and query helpers

## SQLite Contract

Current tables:

- `scan_state`
  - last scanned UID per label
- `bounce_events`
  - raw ingested SES event rows
- `bounce_recipients`
  - one row per bounced recipient
- `event_identifiers`
  - durable dedupe helpers for message IDs
- `aws_suppression_submissions`
  - local record of AWS suppression writes and failures

The SQLite database is the source of truth for reporting and for avoiding duplicate suppression writes.

## Suppression Policy

- Seed AWS with `Permanent` bouncebacks.
- Use AWS suppression reason `BOUNCE`.
- Treat `Transient` as review-only unless a separate rule is explicitly added.
- Current review rule for transient bounces:
  - `1` occurrence: ignore
  - `2` occurrences: watch
  - `3+` occurrences: suppress now

## Idempotency Rules

- Ingest is idempotent via UID tracking and event identifiers.
- AWS sync is idempotent locally via `aws_suppression_submissions`.
- Reruns should skip addresses already recorded as successful submissions.
- Dry-run must remain offline and must not call AWS.

## Safe Defaults

- Keep AWS sync sequential.
- Keep a small inter-call delay.
- Keep retry/backoff enabled for throttled SES API calls.
- Prefer modest batches for live seeding runs.

## When Extending

- Add new data access in `bounceback_store.py` first.
- Prefer read-only reporting helpers over ad hoc SQL in the scripts.
- Preserve local submission tracking when changing AWS sync behavior.
- If adding new suppression eligibility rules, make them visible in `status` and `report` before changing live writes.

## Useful Commands

```bash
source .venv/bin/activate
python fetch_bouncebacks.py report
python aws_suppression_sync.py status --recent 10
python aws_suppression_sync.py sync --dry-run --limit 10
```
