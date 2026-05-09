#!/usr/bin/env python3

from __future__ import annotations

import logging
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ses_bounce import normalize_header_value, parse_ses_bounce


DEFAULT_DB_PATH = "bouncebacks.sqlite3"
DEFAULT_LABEL = "ses_bounce_notifications"
LEGACY_PROCESSED_UIDS = "processed_uids.txt"
AWS_SUPPRESSION_REASON = "BOUNCE"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_db(db_path: str, label: str = DEFAULT_LABEL) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError:
        logging.debug("Could not set WAL mode for %s; continuing with default journal mode.", db_path)
    conn.execute("PRAGMA synchronous = NORMAL")
    initialize_schema(conn)
    migrate_legacy_state(conn, label)
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS scan_state (
            label TEXT PRIMARY KEY,
            last_uid INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bounce_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'imap',
            imap_uid INTEGER,
            sns_message_id TEXT,
            message_id TEXT,
            ses_message_id TEXT,
            subject TEXT,
            from_header TEXT,
            email_date TEXT,
            bounce_timestamp TEXT,
            raw_body TEXT,
            raw_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_imap_uid
            ON bounce_events(imap_uid)
            WHERE imap_uid IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_message_id
            ON bounce_events(message_id)
            WHERE message_id IS NOT NULL;

        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_ses_message_id
            ON bounce_events(ses_message_id)
            WHERE ses_message_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS event_identifiers (
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            event_id INTEGER NOT NULL,
            PRIMARY KEY (kind, value),
            FOREIGN KEY (event_id) REFERENCES bounce_events(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS bounce_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            email_address TEXT NOT NULL,
            bounce_type TEXT,
            bounce_subtype TEXT,
            action TEXT,
            status TEXT,
            diagnostic_code TEXT,
            recipient_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES bounce_events(id) ON DELETE CASCADE,
            UNIQUE (event_id, email_address)
        );

        CREATE INDEX IF NOT EXISTS idx_bounce_recipients_email
            ON bounce_recipients(email_address);

        CREATE INDEX IF NOT EXISTS idx_bounce_recipients_created_at
            ON bounce_recipients(created_at);

        CREATE TABLE IF NOT EXISTS aws_suppression_submissions (
            email_address TEXT PRIMARY KEY,
            source_bounce_type TEXT NOT NULL,
            source_bounce_subtype TEXT NOT NULL,
            aws_reason TEXT NOT NULL,
            status TEXT NOT NULL,
            bounce_count INTEGER NOT NULL DEFAULT 0,
            last_seen TEXT,
            submitted_at TEXT,
            last_attempt_at TEXT NOT NULL,
            last_error TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_aws_suppression_submissions_status
            ON aws_suppression_submissions(status);

        CREATE INDEX IF NOT EXISTS idx_aws_suppression_submissions_updated_at
            ON aws_suppression_submissions(updated_at);
        """
    )
    migrate_schema(conn)
    conn.commit()


def column_info(conn: sqlite3.Connection, table_name: str) -> dict[str, sqlite3.Row]:
    return {row["name"]: row for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}


def index_exists(conn: sqlite3.Connection, index_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
        (index_name,),
    ).fetchone()
    return row is not None


def migrate_schema(conn: sqlite3.Connection) -> None:
    columns = column_info(conn, "bounce_events")
    if "source" not in columns:
        conn.execute("ALTER TABLE bounce_events ADD COLUMN source TEXT NOT NULL DEFAULT 'imap'")
    if "sns_message_id" not in columns:
        conn.execute("ALTER TABLE bounce_events ADD COLUMN sns_message_id TEXT")

    columns = column_info(conn, "bounce_events")
    if columns.get("imap_uid") and int(columns["imap_uid"]["notnull"]) == 1:
        rebuild_bounce_events_for_nullable_imap_uid(conn)

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_imap_uid
            ON bounce_events(imap_uid)
            WHERE imap_uid IS NOT NULL
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_sns_message_id
            ON bounce_events(sns_message_id)
            WHERE sns_message_id IS NOT NULL
        """
    )


def rebuild_bounce_events_for_nullable_imap_uid(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            DROP INDEX IF EXISTS idx_bounce_events_message_id;
            DROP INDEX IF EXISTS idx_bounce_events_ses_message_id;
            DROP INDEX IF EXISTS idx_bounce_events_imap_uid;
            DROP INDEX IF EXISTS idx_bounce_events_sns_message_id;

            CREATE TABLE bounce_events_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'imap',
                imap_uid INTEGER,
                sns_message_id TEXT,
                message_id TEXT,
                ses_message_id TEXT,
                subject TEXT,
                from_header TEXT,
                email_date TEXT,
                bounce_timestamp TEXT,
                raw_body TEXT,
                raw_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            INSERT INTO bounce_events_new (
                id, label, source, imap_uid, sns_message_id, message_id, ses_message_id,
                subject, from_header, email_date, bounce_timestamp, raw_body, raw_json, created_at
            )
            SELECT
                id, label, COALESCE(source, 'imap'), imap_uid, sns_message_id, message_id, ses_message_id,
                subject, from_header, email_date, bounce_timestamp, raw_body, raw_json, created_at
            FROM bounce_events;

            DROP TABLE bounce_events;
            ALTER TABLE bounce_events_new RENAME TO bounce_events;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_message_id
                ON bounce_events(message_id)
                WHERE message_id IS NOT NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_bounce_events_ses_message_id
                ON bounce_events(ses_message_id)
                WHERE ses_message_id IS NOT NULL;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def migrate_legacy_state(conn: sqlite3.Connection, label: str) -> None:
    legacy = Path(LEGACY_PROCESSED_UIDS)
    if not legacy.exists():
        return

    row = conn.execute(
        "SELECT last_uid FROM scan_state WHERE label = ?",
        (label,),
    ).fetchone()
    if row and row["last_uid"] > 0:
        return

    uids: list[int] = []
    for line in legacy.read_text(encoding="utf-8").splitlines():
        try:
            uids.append(int(line.strip()))
        except ValueError:
            continue

    if not uids:
        return

    last_uid = max(uids)
    conn.execute(
        """
        INSERT INTO scan_state (label, last_uid, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(label) DO UPDATE SET
            last_uid = excluded.last_uid,
            updated_at = excluded.updated_at
        """,
        (label, last_uid, utc_now()),
    )
    conn.commit()
    logging.info("Seeded scan state from legacy %s up to UID %s", legacy, last_uid)


def load_scan_state(conn: sqlite3.Connection, label: str) -> int:
    row = conn.execute(
        "SELECT last_uid FROM scan_state WHERE label = ?",
        (label,),
    ).fetchone()
    return int(row["last_uid"]) if row else 0


def save_scan_state(conn: sqlite3.Connection, label: str, last_uid: int) -> None:
    conn.execute(
        """
        INSERT INTO scan_state (label, last_uid, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(label) DO UPDATE SET
            last_uid = excluded.last_uid,
            updated_at = excluded.updated_at
        """,
        (label, last_uid, utc_now()),
    )


def seen_event(
    conn: sqlite3.Connection,
    *,
    message_id: str = "",
    ses_message_id: str = "",
    sns_message_id: str = "",
    imap_uid: Optional[int] = None,
) -> bool:
    identifiers = [
        ("message_id", message_id),
        ("ses_message_id", ses_message_id),
        ("sns_message_id", sns_message_id),
    ]
    if imap_uid is not None:
        identifiers.append(("imap_uid", str(imap_uid)))
    for kind, value in identifiers:
        if not value:
            continue
        row = conn.execute(
            "SELECT 1 FROM event_identifiers WHERE kind = ? AND value = ?",
            (kind, value),
        ).fetchone()
        if row:
            return True
    return False


def insert_bounce_event(
    conn: sqlite3.Connection,
    *,
    label: str,
    source: str,
    imap_uid: Optional[int],
    sns_message_id: str,
    message_id: str,
    subject: str,
    from_header: str,
    email_date: str,
    raw_body: str,
    raw_json: dict[str, Any],
) -> tuple[int, int]:
    parsed = parse_ses_bounce(raw_json)
    if parsed is None:
        return 0, 0

    ses_message_id = parsed.ses_message_id
    if seen_event(
        conn,
        message_id=message_id,
        ses_message_id=ses_message_id,
        sns_message_id=sns_message_id,
        imap_uid=imap_uid,
    ):
        return 0, 0

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO bounce_events (
            label, source, imap_uid, sns_message_id, message_id, ses_message_id, subject,
            from_header, email_date, bounce_timestamp, raw_body, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            label,
            source,
            imap_uid,
            sns_message_id or None,
            message_id or None,
            ses_message_id or None,
            subject,
            from_header,
            email_date,
            parsed.bounce_timestamp,
            raw_body,
            json.dumps(parsed.payload, ensure_ascii=False, sort_keys=True),
        ),
    )
    if cursor.rowcount <= 0:
        return 0, 0

    event_id = int(cursor.lastrowid)
    identifiers = [
        ("message_id", message_id),
        ("ses_message_id", ses_message_id),
        ("sns_message_id", sns_message_id),
    ]
    if imap_uid is not None:
        identifiers.append(("imap_uid", str(imap_uid)))
    for kind, value in identifiers:
        if value:
            conn.execute(
                "INSERT OR IGNORE INTO event_identifiers (kind, value, event_id) VALUES (?, ?, ?)",
                (kind, value, event_id),
            )

    recipient_count = insert_recipients(conn, event_id, parsed.payload)
    return event_id, recipient_count


def insert_recipients(conn: sqlite3.Connection, event_id: int, bounce_json: dict[str, Any]) -> int:
    parsed = parse_ses_bounce(bounce_json)
    if parsed is None:
        return 0
    bounce_type = parsed.bounce.get("bounceType")
    bounce_subtype = parsed.bounce.get("bounceSubType")
    inserted = 0
    for recipient in parsed.recipients:
        email_address = normalize_header_value(recipient.get("emailAddress", "")).lower()
        if not email_address:
            continue
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO bounce_recipients (
                event_id, email_address, bounce_type, bounce_subtype,
                action, status, diagnostic_code, recipient_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                email_address,
                bounce_type,
                bounce_subtype,
                recipient.get("action"),
                recipient.get("status"),
                recipient.get("diagnosticCode"),
                json.dumps(recipient, ensure_ascii=False, sort_keys=True),
            ),
        )
        if cursor.rowcount > 0:
            inserted += 1
    return inserted


def latest_suppression_row_for_email(conn: sqlite3.Connection, email_address: str) -> Optional[dict[str, Any]]:
    rows = query_suppression_rows(conn, limit=None, bounce_type="Permanent", exclude_submitted=True)
    normalized = email_address.strip().lower()
    for row in rows:
        if row.get("email_address") == normalized:
            return row
    return None


def successful_suppression_exists(conn: sqlite3.Connection, email_address: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM aws_suppression_submissions WHERE email_address = lower(?) AND status = 'success'",
        (email_address,),
    ).fetchone()
    return row is not None


def query_suppression_rows(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
    bounce_type: Optional[str] = None,
    bounce_subtype: Optional[str] = None,
    exclude_submitted: bool = False,
) -> list[dict[str, Any]]:
    where_clauses = []
    params: list[Any] = []

    if bounce_type:
        where_clauses.append("lower(br.bounce_type) = lower(?)")
        params.append(bounce_type)
    if bounce_subtype:
        where_clauses.append("lower(br.bounce_subtype) = lower(?)")
        params.append(bounce_subtype)
    if exclude_submitted:
        where_clauses.append(
            "NOT EXISTS (SELECT 1 FROM aws_suppression_submissions s WHERE s.email_address = lower(br.email_address) AND s.status = 'success')"
        )

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    limit_sql = "LIMIT ?" if limit is not None else ""
    if limit is not None:
        params.append(limit)

    sql = f"""
        WITH ranked AS (
            SELECT
                lower(br.email_address) AS email_address,
                COALESCE(be.bounce_timestamp, be.created_at) AS seen_at,
                br.bounce_type,
                br.bounce_subtype,
                br.diagnostic_code,
                be.subject,
                be.message_id,
                be.ses_message_id,
                COUNT(*) OVER (PARTITION BY lower(br.email_address)) AS bounce_count,
                ROW_NUMBER() OVER (
                    PARTITION BY lower(br.email_address)
                    ORDER BY COALESCE(be.bounce_timestamp, be.created_at) DESC, be.id DESC
                ) AS rn
            FROM bounce_recipients br
            JOIN bounce_events be ON be.id = br.event_id
            {where_sql}
        )
        SELECT
            email_address,
            seen_at AS last_seen,
            bounce_count,
            bounce_type,
            bounce_subtype,
            diagnostic_code,
            subject AS last_subject,
            message_id AS last_message_id,
            ses_message_id AS last_ses_message_id
        FROM ranked
        WHERE rn = 1
        ORDER BY last_seen DESC, email_address ASC
        {limit_sql}
    """
    return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]


def scalar_count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    return int(row[0])


def record_suppression_submission(
    conn: sqlite3.Connection,
    *,
    email_address: str,
    source_bounce_type: str,
    source_bounce_subtype: str,
    aws_reason: str,
    bounce_count: int,
    last_seen: Optional[str],
    status: str,
    last_error: Optional[str] = None,
) -> None:
    now = utc_now()
    existing = conn.execute(
        "SELECT submitted_at FROM aws_suppression_submissions WHERE email_address = ?",
        (email_address,),
    ).fetchone()
    submitted_at = existing["submitted_at"] if existing and existing["submitted_at"] else (now if status == "success" else None)
    conn.execute(
        """
        INSERT INTO aws_suppression_submissions (
            email_address, source_bounce_type, source_bounce_subtype, aws_reason,
            status, bounce_count, last_seen, submitted_at, last_attempt_at, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(email_address) DO UPDATE SET
            source_bounce_type = excluded.source_bounce_type,
            source_bounce_subtype = excluded.source_bounce_subtype,
            aws_reason = excluded.aws_reason,
            status = excluded.status,
            bounce_count = excluded.bounce_count,
            last_seen = excluded.last_seen,
            submitted_at = COALESCE(aws_suppression_submissions.submitted_at, excluded.submitted_at),
            last_attempt_at = excluded.last_attempt_at,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (
            email_address,
            source_bounce_type,
            source_bounce_subtype,
            aws_reason,
            status,
            bounce_count,
            last_seen,
            submitted_at,
            now,
            last_error,
            now,
        ),
    )


def count_successful_suppression_submissions(conn: sqlite3.Connection) -> int:
    return scalar_count(
        conn,
        "SELECT COUNT(*) FROM aws_suppression_submissions WHERE status = 'success'",
    )


def count_failed_suppression_submissions(conn: sqlite3.Connection) -> int:
    return scalar_count(
        conn,
        "SELECT COUNT(*) FROM aws_suppression_submissions WHERE status = 'error'",
    )


def count_pending_suppression_candidates(
    conn: sqlite3.Connection,
    *,
    bounce_type: str,
    bounce_subtype: Optional[str] = None,
) -> int:
    where_clauses = [
        "lower(br.bounce_type) = lower(?)",
    ]
    params: list[Any] = [bounce_type]

    if bounce_subtype:
        where_clauses.append("lower(br.bounce_subtype) = lower(?)")
        params.append(bounce_subtype)

    row = conn.execute(
        """
        SELECT COUNT(DISTINCT lower(br.email_address)) AS count
        FROM bounce_recipients br
        JOIN bounce_events be ON be.id = br.event_id
        WHERE """
        + " AND ".join(where_clauses)
        + """
          AND NOT EXISTS (
              SELECT 1
              FROM aws_suppression_submissions s
              WHERE s.email_address = lower(br.email_address)
                AND s.status = 'success'
          )
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return 0
    return int(row[0])


def count_distinct_bounce_emails(conn: sqlite3.Connection) -> int:
    return scalar_count(
        conn,
        "SELECT COUNT(DISTINCT lower(email_address)) FROM bounce_recipients",
    )


def count_bounce_events(conn: sqlite3.Connection) -> int:
    return scalar_count(conn, "SELECT COUNT(*) FROM bounce_events")


def count_bounce_recipients(conn: sqlite3.Connection) -> int:
    return scalar_count(conn, "SELECT COUNT(*) FROM bounce_recipients")


def recent_suppression_submissions(
    conn: sqlite3.Connection,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                email_address,
                source_bounce_type,
                source_bounce_subtype,
                aws_reason,
                status,
                bounce_count,
                last_seen,
                submitted_at,
                last_attempt_at,
                last_error,
                updated_at
            FROM aws_suppression_submissions
            ORDER BY datetime(last_attempt_at) DESC, email_address ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def query_transient_review_rows(
    conn: sqlite3.Connection,
    *,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    sql = """
        WITH ranked AS (
            SELECT
                lower(br.email_address) AS email_address,
                COALESCE(be.bounce_timestamp, be.created_at) AS seen_at,
                br.bounce_subtype,
                br.diagnostic_code,
                be.subject,
                be.message_id,
                be.ses_message_id,
                COUNT(*) OVER (PARTITION BY lower(br.email_address)) AS transient_count,
                ROW_NUMBER() OVER (
                    PARTITION BY lower(br.email_address)
                    ORDER BY COALESCE(be.bounce_timestamp, be.created_at) DESC, be.id DESC
                ) AS rn
            FROM bounce_recipients br
            JOIN bounce_events be ON be.id = br.event_id
            WHERE lower(br.bounce_type) = lower('Transient')
        )
        SELECT
            email_address,
            seen_at AS last_seen,
            transient_count,
            bounce_subtype,
            diagnostic_code,
            subject AS last_subject,
            message_id AS last_message_id,
            ses_message_id AS last_ses_message_id
        FROM ranked
        WHERE rn = 1
        ORDER BY transient_count DESC, last_seen DESC, email_address ASC
    """
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (limit,)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count_transient_bounce_emails(conn: sqlite3.Connection) -> int:
    return scalar_count(
        conn,
        """
        SELECT COUNT(DISTINCT lower(email_address))
        FROM bounce_recipients
        WHERE lower(bounce_type) = lower('Transient')
        """,
    )


def count_table_rows(conn: sqlite3.Connection, table_name: str) -> int:
    allowed = {
        "scan_state",
        "bounce_events",
        "bounce_recipients",
        "event_identifiers",
        "aws_suppression_submissions",
    }
    if table_name not in allowed:
        raise ValueError(f"Unsupported table for counting: {table_name}")
    return scalar_count(conn, f"SELECT COUNT(*) FROM {table_name}")


def database_is_empty(conn: sqlite3.Connection) -> bool:
    return all(
        count_table_rows(conn, table_name) == 0
        for table_name in (
            "scan_state",
            "bounce_events",
            "bounce_recipients",
            "event_identifiers",
            "aws_suppression_submissions",
        )
    )
