#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import imaplib
import json
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass
from email import message_from_bytes
from email.message import Message
from typing import Any, Optional

from bounceback_store import (
    connect_db,
    build_triage_report,
    insert_bounce_event as store_insert_bounce_event,
    load_scan_state,
    query_suppression_rows,
    normalize_triage_bucket_filter,
    save_scan_state,
    seen_event as store_seen_event,
)
from ses_bounce import (
    extract_json_from_text,
    extract_text_payload,
    normalize_header_value,
    normalize_ses_payload,
    parse_ses_bounce,
)
from ses_config import DEFAULT_IMAP_HOST, load_config, require_imap_credentials


ENV_IMAP_USER = "SES_BOUNCE_IMAP_USER"
ENV_IMAP_PASS = "SES_BOUNCE_IMAP_PASS"
ENV_IMAP_HOST = "SES_BOUNCE_IMAP_HOST"
ENV_LABEL = "SES_BOUNCE_LABEL"
ENV_DB_PATH = "SES_BOUNCE_DB"


@dataclass(frozen=True)
class AppConfig:
    db_path: str
    imap_user: Optional[str]
    imap_pass: Optional[str]
    imap_host: str
    label: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest SES bounceback emails from Gmail into SQLite."
    )
    parser.add_argument("--config", default=None, help="Optional TOML config path.")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite database path.",
    )
    parser.add_argument(
        "--imap-user",
        default=None,
        help="IMAP username. Defaults to SES_BOUNCE_IMAP_USER.",
    )
    parser.add_argument(
        "--imap-pass",
        default=None,
        help="IMAP password/app password. Defaults to SES_BOUNCE_IMAP_PASS.",
    )
    parser.add_argument(
        "--imap-host",
        default=None,
        help="IMAP host. Defaults to SES_BOUNCE_IMAP_HOST or imap.gmail.com.",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Gmail label/folder to scan. Defaults to SES_BOUNCE_LABEL, config file, or ses_bounce_notifications.",
    )

    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Scan the mailbox and ingest new bouncebacks.")
    sync_parser.set_defaults(command="sync")

    report_parser = subparsers.add_parser("report", help="Print a decision-oriented bounceback triage report from SQLite.")
    report_parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="How many rows to show per report section.",
    )
    report_parser.add_argument(
        "--format",
        choices=("text", "json", "csv"),
        default="text",
        help="Report output format.",
    )
    report_parser.add_argument(
        "--bucket",
        default=None,
        help="Optional decision bucket filter: remove-now, watch, or ignore-for-now.",
    )
    report_parser.set_defaults(command="report")

    export_parser = subparsers.add_parser(
        "export-suppressions",
        help="Export the deduplicated suppression list from SQLite.",
    )
    export_parser.add_argument(
        "--output",
        required=True,
        help="Output path for the suppression export.",
    )
    export_parser.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
        help="Export format.",
    )
    export_parser.set_defaults(command="export-suppressions")

    parser.set_defaults(command="sync")
    return parser.parse_args()


def resolve_config(args: argparse.Namespace) -> AppConfig:
    shared = load_config(
        args.config,
        {
            "db_path": args.db,
            "imap_user": args.imap_user,
            "imap_pass": args.imap_pass,
            "imap_host": args.imap_host,
            "label": args.label,
        },
    )
    if args.command == "sync":
        require_imap_credentials(shared)

    return AppConfig(
        db_path=shared.database.path,
        imap_user=shared.imap.user,
        imap_pass=shared.imap.password,
        imap_host=shared.imap.host,
        label=shared.imap.label,
    )


def require_sync_credentials(config: AppConfig) -> tuple[str, str]:
    if not config.imap_user or not config.imap_pass:
        raise SystemExit("IMAP credentials are required for sync.")
    return config.imap_user, config.imap_pass


def imap_connect(imap_host: str, imap_user: str, imap_pass: str) -> imaplib.IMAP4_SSL:
    mail = imaplib.IMAP4_SSL(imap_host)
    mail.login(imap_user, imap_pass)
    return mail


def fetch_uid_list(mail: imaplib.IMAP4_SSL, last_uid: int) -> list[bytes]:
    if last_uid > 0:
        status, data = mail.uid("search", None, "UID", f"{last_uid + 1}:*")
    else:
        status, data = mail.uid("search", None, "ALL")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def fetch_headers(mail: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    status, data = mail.uid(
        "fetch",
        uid,
        "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE SUBJECT FROM)])",
    )
    if status != "OK" or not data or not data[0]:
        return None
    payload = data[0][1]
    if not payload:
        return None
    return message_from_bytes(payload)


def fetch_full_message(mail: imaplib.IMAP4_SSL, uid: str) -> Message | None:
    status, data = mail.uid("fetch", uid, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return None
    payload = data[0][1]
    if not payload:
        return None
    return message_from_bytes(payload)


def seen_event(conn, message_id: str, ses_message_id: str) -> bool:
    if message_id:
        row = conn.execute(
            "SELECT 1 FROM event_identifiers WHERE kind = 'message_id' AND value = ?",
            (message_id,),
        ).fetchone()
        if row:
            return True
    if ses_message_id:
        row = conn.execute(
            "SELECT 1 FROM event_identifiers WHERE kind = 'ses_message_id' AND value = ?",
            (ses_message_id,),
        ).fetchone()
        if row:
            return True
    return False


def insert_event(
    conn,
    *,
    label: str,
    imap_uid: int,
    message_id: str,
    ses_message_id: str,
    subject: str,
    from_header: str,
    email_date: str,
    bounce_timestamp: str,
    raw_body: str,
    raw_json: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO bounce_events (
            label, imap_uid, message_id, ses_message_id, subject,
            from_header, email_date, bounce_timestamp, raw_body, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            label,
            imap_uid,
            message_id or None,
            ses_message_id or None,
            subject,
            from_header,
            email_date,
            bounce_timestamp,
            raw_body,
            json.dumps(raw_json, ensure_ascii=False, sort_keys=True),
        ),
    )
    if cursor.lastrowid is None:
        return 0
    event_id = int(cursor.lastrowid)
    if message_id:
        conn.execute(
            "INSERT OR IGNORE INTO event_identifiers (kind, value, event_id) VALUES ('message_id', ?, ?)",
            (message_id, event_id),
        )
    if ses_message_id:
        conn.execute(
            "INSERT OR IGNORE INTO event_identifiers (kind, value, event_id) VALUES ('ses_message_id', ?, ?)",
            (ses_message_id, event_id),
        )
    return event_id


def insert_recipients(conn, event_id: int, bounce_json: dict[str, Any]) -> int:
    bounce = bounce_json.get("bounce", {})
    bounce_type = bounce.get("bounceType")
    bounce_subtype = bounce.get("bounceSubType")
    inserted = 0
    for recipient in bounce.get("bouncedRecipients", []):
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


def sync(config: AppConfig) -> None:
    imap_user, imap_pass = require_sync_credentials(config)
    conn = connect_db(config.db_path, config.label)
    mail = imap_connect(config.imap_host, imap_user, imap_pass)
    try:
        status, _ = mail.select(config.label)
        if status != "OK":
            raise SystemExit(f"Could not select label '{config.label}'.")

        last_uid = load_scan_state(conn, config.label)
        uid_bytes_list = fetch_uid_list(mail, last_uid)
        total_candidates = len(uid_bytes_list)

        logging.info(
            "Scanning label %s from UID %s with %s candidate messages",
            config.label,
            last_uid + 1,
            total_candidates,
        )

        stats = Counter()
        highest_uid = last_uid

        for uid_bytes in uid_bytes_list:
            uid = uid_bytes.decode("ascii", errors="ignore")
            if not uid:
                continue
            uid_int = int(uid)
            highest_uid = max(highest_uid, uid_int)

            headers = fetch_headers(mail, uid)
            if headers is None:
                logging.warning("UID %s: could not fetch headers", uid)
                continue

            message_id = normalize_header_value(headers.get("Message-ID"))
            email_date = normalize_header_value(headers.get("Date"))
            subject = normalize_header_value(headers.get("Subject"))
            from_header = normalize_header_value(headers.get("From"))

            if seen_event(conn, message_id, ""):
                stats["duplicate_headers"] += 1
                save_scan_state(conn, config.label, uid_int)
                conn.commit()
                continue

            full_msg = fetch_full_message(mail, uid)
            if full_msg is None:
                logging.warning("UID %s: could not fetch full message", uid)
                continue

            raw_body = extract_text_payload(full_msg)
            payload_json = extract_json_from_text(raw_body)
            if not payload_json:
                stats["non_json"] += 1
                save_scan_state(conn, config.label, uid_int)
                conn.commit()
                continue

            payload_json = normalize_ses_payload(payload_json)
            parsed = parse_ses_bounce(payload_json)
            if parsed is None:
                stats["non_bounce"] += 1
                save_scan_state(conn, config.label, uid_int)
                conn.commit()
                continue
            ses_message_id = parsed.ses_message_id

            if store_seen_event(conn, message_id=message_id, ses_message_id=ses_message_id, imap_uid=uid_int):
                stats["duplicate_events"] += 1
                save_scan_state(conn, config.label, uid_int)
                conn.commit()
                continue

            with conn:
                event_id, recipient_count = store_insert_bounce_event(
                    conn,
                    label=config.label,
                    source="imap",
                    imap_uid=uid_int,
                    sns_message_id="",
                    message_id=message_id,
                    subject=subject,
                    from_header=from_header,
                    email_date=email_date,
                    raw_body=raw_body,
                    raw_json=payload_json,
                )
                if event_id == 0:
                    stats["duplicate_events"] += 1
                    save_scan_state(conn, config.label, uid_int)
                    continue
                save_scan_state(conn, config.label, uid_int)

            stats["events"] += 1
            stats["recipients"] += recipient_count

        if highest_uid > last_uid:
            save_scan_state(conn, config.label, highest_uid)
            conn.commit()

        logging.info(
            "Sync complete: %s new events, %s recipients, %s duplicate headers, %s duplicate events, %s non-bounce, %s non-json.",
            stats["events"],
            stats["recipients"],
            stats["duplicate_headers"],
            stats["duplicate_events"],
            stats["non_bounce"],
            stats["non_json"],
        )
    finally:
        mail.logout()
        conn.close()


def report_csv_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for section in report.get("sections", []):
        for row in section.get("rows", []):
            rows.append(
                {
                    "section": section.get("title", ""),
                    "decision_bucket": row.get("decision_bucket", ""),
                    "decision_reason": row.get("decision_reason", ""),
                    "email_address": row.get("email_address", ""),
                    "total_count": row.get("total_count", 0),
                    "permanent_count": row.get("permanent_count", 0),
                    "transient_count": row.get("transient_count", 0),
                    "last_seen": row.get("last_seen", ""),
                    "bounce_type": row.get("bounce_type", ""),
                    "bounce_subtype": row.get("bounce_subtype", ""),
                    "diagnostic_code": row.get("diagnostic_code", ""),
                    "last_subject": row.get("last_subject", ""),
                    "last_message_id": row.get("last_message_id", ""),
                    "last_ses_message_id": row.get("last_ses_message_id", ""),
                }
            )
    return rows


def emit_report(conn, db_path: str, limit: int, fmt: str, bucket: Optional[str] = None) -> None:
    report = build_triage_report(conn, limit=limit, bucket=normalize_triage_bucket_filter(bucket))
    last_sync = conn.execute(
        "SELECT label, last_uid, updated_at FROM scan_state ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()

    if fmt == "json":
        json.dump(
            {
                "db_path": db_path,
                "last_scan": (
                    {"label": last_sync[0], "last_uid": int(last_sync[1]), "updated_at": last_sync[2]}
                    if last_sync is not None
                    else None
                ),
                **report,
            },
            sys.stdout,
            indent=2,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return

    if fmt == "csv":
        writer = csv.DictWriter(
            sys.stdout,
            fieldnames=[
                "section",
                "decision_bucket",
                "decision_reason",
                "email_address",
                "total_count",
                "permanent_count",
                "transient_count",
                "last_seen",
                "bounce_type",
                "bounce_subtype",
                "diagnostic_code",
                "last_subject",
                "last_message_id",
                "last_ses_message_id",
            ],
        )
        writer.writeheader()
        writer.writerows(report_csv_rows(report))
        return

    print("SES Bounceback Triage Recommendations")
    print("===========================")
    print(f"Database: {db_path}")
    print(f"Generated at: {report['generated_at']}")
    summary = report["summary"]
    print(f"Total addresses: {summary['total_addresses']}")
    print(f"Recommended removals: {summary['remove now']}")
    print(f"Recommended watch: {summary['watch']}")
    print(f"Recommended ignores: {summary['ignore for now']}")
    if last_sync is not None:
        label, last_uid, updated_at = last_sync
        print(f"Last scan: label={label} last_uid={last_uid} updated_at={updated_at}")
    print()

    for section in report["sections"]:
        rows = section["rows"]
        print(section["title"])
        print("-" * len(section["title"]))
        print(section["subtitle"])
        print(f"Showing up to {limit} rows.")
        if not rows:
            print("No rows in this section.")
            print()
            continue
        for row in rows:
            refs = " / ".join(
                value
                for value in (
                    str(row.get("last_message_id") or "").strip(),
                    str(row.get("last_ses_message_id") or "").strip(),
                )
                if value
            )
            print(
                f"{row['email_address']} | recommendation={row['decision_bucket']} | reason={row['decision_reason']} | "
                f"total={row['total_count']} | permanent={row['permanent_count']} | transient={row['transient_count']} | "
                f"last_seen={row['last_seen']} | type={row['bounce_type']} | subtype={row['bounce_subtype']} | "
                f"diagnostic={row['diagnostic_code']} | subject={row['last_subject']} | refs={refs}"
            )
        print()


def export_suppressions(conn, output: str, fmt: str) -> None:
    rows = query_suppression_rows(conn, limit=None)
    path = os.path.abspath(output)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "email_address",
                    "last_seen",
                    "bounce_count",
                    "bounce_type",
                    "bounce_subtype",
                    "diagnostic_code",
                    "last_subject",
                    "last_message_id",
                    "last_ses_message_id",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
            fh.write("\n")

    logging.info("Wrote %s suppression rows to %s", len(rows), path)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    config = resolve_config(args)

    if args.command == "sync":
        sync(config)
        return

    conn = connect_db(config.db_path, config.label)
    try:
        if args.command == "report":
            emit_report(conn, config.db_path, limit=args.limit, fmt=args.format, bucket=args.bucket)
        elif args.command == "export-suppressions":
            export_suppressions(conn, args.output, args.format)
        else:
            raise SystemExit(f"Unknown command: {args.command}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
