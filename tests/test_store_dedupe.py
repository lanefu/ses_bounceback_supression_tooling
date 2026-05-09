from __future__ import annotations

from fetch_bouncebacks import insert_event, seen_event
from bounceback_store import connect_db


def test_event_identifier_dedupe_blocks_duplicate_messages(tmp_path):
    conn = connect_db(str(tmp_path / "bouncebacks.sqlite3"))
    try:
        payload = {
            "mail": {"messageId": "ses-message-1"},
            "bounce": {
                "timestamp": "2026-05-09T12:01:00Z",
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "bouncedRecipients": [],
            },
        }

        first_event_id = insert_event(
            conn,
            label="armbian_email_delivery",
            imap_uid=10,
            message_id="message-1",
            ses_message_id="ses-message-1",
            subject="Bounce notice",
            from_header="MAILER-DAEMON@example.com",
            email_date="Sat, 09 May 2026 12:01:00 +0000",
            bounce_timestamp="2026-05-09T12:01:00Z",
            raw_body="{}",
            raw_json=payload,
        )
        conn.commit()

        assert first_event_id > 0
        assert seen_event(conn, "message-1", "")
        assert seen_event(conn, "", "ses-message-1")
        assert seen_event(conn, "message-1", "ses-message-1")
        assert conn.execute("SELECT COUNT(*) FROM bounce_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM event_identifiers").fetchone()[0] == 2
    finally:
        conn.close()
