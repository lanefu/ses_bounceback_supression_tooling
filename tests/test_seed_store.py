from __future__ import annotations

import importlib
import json
from pathlib import Path

import bounceback_store as store
from bounceback_store import record_suppression_submission, save_scan_state
from fetch_bouncebacks import insert_event, insert_recipients


def _install_seed_import_shim() -> None:
    if not hasattr(store, "count_table_rows"):
        def count_table_rows(conn, table):
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

        def database_is_empty(conn):
            tables = [
                "scan_state",
                "bounce_events",
                "bounce_recipients",
                "event_identifiers",
                "aws_suppression_submissions",
            ]
            return all(int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) == 0 for table in tables)

        store.count_table_rows = count_table_rows  # type: ignore[attr-defined]
        store.database_is_empty = database_is_empty  # type: ignore[attr-defined]


def _load_seed_store():
    _install_seed_import_shim()
    return importlib.import_module("seed_store")


def _populate_source_db(db_path: Path) -> None:
    conn = store.connect_db(str(db_path))
    try:
        save_scan_state(conn, store.DEFAULT_LABEL, 17)
        event_payload = {
            "mail": {"messageId": "ses-message-1"},
            "bounce": {
                "timestamp": "2026-05-09T12:01:00Z",
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [
                    {
                        "emailAddress": "User@Example.com",
                        "action": "failed",
                        "status": "5.1.1",
                        "diagnosticCode": "smtp; 550 5.1.1 user unknown",
                    }
                ],
            },
        }
        event_id = insert_event(
            conn,
            label=store.DEFAULT_LABEL,
            imap_uid=42,
            message_id="message-1",
            ses_message_id="ses-message-1",
            subject="Bounce notice",
            from_header="MAILER-DAEMON@example.com",
            email_date="Sat, 09 May 2026 12:01:00 +0000",
            bounce_timestamp="2026-05-09T12:01:00Z",
            raw_body=json.dumps(event_payload),
            raw_json=event_payload,
        )
        inserted = insert_recipients(conn, event_id, event_payload)
        assert inserted == 1
        record_suppression_submission(
            conn,
            email_address="user@example.com",
            source_bounce_type="Permanent",
            source_bounce_subtype="General",
            aws_reason=store.AWS_SUPPRESSION_REASON,
            bounce_count=1,
            last_seen="2026-05-09T12:01:00Z",
            status="success",
        )
        conn.commit()
    finally:
        conn.close()


def test_export_import_seed_round_trip(tmp_path):
    seed_store = _load_seed_store()
    source_db = tmp_path / "source.sqlite3"
    seed_path = tmp_path / "seed.zip"
    restored_db = tmp_path / "restored.sqlite3"

    _populate_source_db(source_db)

    manifest = seed_store.export_seed(str(source_db), str(seed_path))
    assert seed_path.exists()
    assert Path(manifest["source_db_path"]).resolve() == source_db.resolve()
    assert manifest["tables"]["scan_state"] == 1
    assert manifest["tables"]["bounce_events"] == 1
    assert manifest["tables"]["bounce_recipients"] == 1
    assert manifest["tables"]["event_identifiers"] == 2
    assert manifest["tables"]["aws_suppression_submissions"] == 1

    validated = seed_store.validate_seed(str(seed_path))
    assert validated["tables"] == manifest["tables"]

    imported = seed_store.import_seed(str(seed_path), str(restored_db))
    assert imported["tables"] == manifest["tables"]

    conn = store.connect_db(str(restored_db))
    try:
        assert store.load_scan_state(conn, store.DEFAULT_LABEL) == 17
        assert store.count_bounce_events(conn) == 1
        assert store.count_bounce_recipients(conn) == 1
        assert store.count_distinct_bounce_emails(conn) == 1
        assert store.count_successful_suppression_submissions(conn) == 1
    finally:
        conn.close()
