#!/usr/bin/env python3

from __future__ import annotations

import argparse
import random
import time
import logging
from dataclasses import dataclass
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from bounceback_store import (
    AWS_SUPPRESSION_REASON,
    connect_db,
    count_failed_suppression_submissions,
    count_pending_suppression_candidates,
    count_successful_suppression_submissions,
    query_suppression_rows,
    recent_suppression_submissions,
    record_suppression_submission,
)
from ses_config import load_config


DEFAULT_BATCH_SIZE = 25
DEFAULT_DELAY_SECONDS = 0.25
DEFAULT_MAX_RETRIES = 6
SOURCE_BOUNCE_TYPE = "Permanent"


@dataclass(frozen=True)
class SyncConfig:
    db_path: str
    label: str
    command: str
    dry_run: bool
    limit: Optional[int]
    batch_size: int
    delay_seconds: float
    max_retries: int
    profile: Optional[str]
    region: Optional[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync bounceback-derived suppression candidates into AWS SES global suppression."
    )
    parser.add_argument("--config", default=None, help="Optional TOML config path.")
    parser.add_argument("--db", default=None, help="SQLite database path.")
    parser.add_argument(
        "--label",
        default=None,
        help="Label/folder name to use for DB state and source filtering.",
    )
    parser.add_argument("--profile", default=None, help="AWS profile name to use.")
    parser.add_argument("--region", default=None, help="AWS region name to use.")
    parser.set_defaults(command="sync")

    subparsers = parser.add_subparsers(dest="command")

    sync_parser = subparsers.add_parser("sync", help="Push pending suppression rows to AWS SES.")
    sync_parser.add_argument("--dry-run", action="store_true", help="Show what would be suppressed without writing to AWS.")
    sync_parser.add_argument("--limit", type=int, default=None, help="Only process the first N eligible rows.")
    sync_parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Process candidate rows in batches of this size.",
    )
    sync_parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help="Seconds to sleep between AWS writes/lookups.",
    )
    sync_parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="Maximum retry attempts for throttled AWS calls.",
    )
    sync_parser.set_defaults(command="sync")

    status_parser = subparsers.add_parser("status", help="Show local suppression submission status.")
    status_parser.add_argument(
        "--recent",
        type=int,
        default=10,
        help="How many recent local submission attempts to show.",
    )
    status_parser.set_defaults(command="status")

    return parser.parse_args()


def resolve_config(args: argparse.Namespace) -> SyncConfig:
    shared = load_config(
        args.config,
        {
            "db_path": args.db,
            "label": args.label,
            "profile": args.profile,
            "region": args.region,
            "batch_size": getattr(args, "batch_size", None),
            "delay_seconds": getattr(args, "delay", None),
            "max_retries": getattr(args, "max_retries", None),
        },
    )
    return SyncConfig(
        db_path=shared.database.path,
        label=shared.imap.label,
        command=args.command or "sync",
        dry_run=bool(getattr(args, "dry_run", False)),
        limit=getattr(args, "limit", None),
        batch_size=shared.aws.batch_size,
        delay_seconds=shared.aws.delay_seconds,
        max_retries=shared.aws.retry_count,
        profile=shared.aws.profile,
        region=shared.aws.region,
    )


def ses_client(profile: Optional[str], region: Optional[str]) -> Any:
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("sesv2")


def is_throttled(exc: ClientError) -> bool:
    error_code = exc.response.get("Error", {}).get("Code", "")
    http_status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) or 0)
    return error_code in {"TooManyRequestsException", "ThrottlingException", "Throttling"} or http_status == 429


def call_with_retry(action: str, max_retries: int, func: Any, *args: Any, **kwargs: Any) -> Any:
    delay = 0.5
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except ClientError as exc:
            if not is_throttled(exc) or attempt >= max_retries:
                raise
            sleep_for = delay + random.uniform(0, delay * 0.25)
            logging.warning(
                "%s throttled (attempt %s/%s); sleeping %.2fs before retry.",
                action,
                attempt + 1,
                max_retries + 1,
                sleep_for,
            )
            time.sleep(sleep_for)
            delay = min(delay * 2, 8.0)
    raise RuntimeError(f"{action} retry loop exited unexpectedly")


def add_to_suppression_list(client: Any, email_address: str, max_retries: int) -> None:
    call_with_retry(
        "ses.put_suppressed_destination",
        max_retries,
        client.put_suppressed_destination,
        EmailAddress=email_address,
        Reason=AWS_SUPPRESSION_REASON,
    )


def run_sync(config: SyncConfig) -> None:
    conn = connect_db(config.db_path, config.label)
    try:
        candidates = query_suppression_rows(
            conn,
            limit=config.limit,
            bounce_type=SOURCE_BOUNCE_TYPE,
            exclude_submitted=True,
        )
        successful_submissions = count_successful_suppression_submissions(conn)
        client = None if config.dry_run else ses_client(config.profile, config.region)

        logging.info(
            "Loaded %s candidate addresses from %s (label=%s, already_submitted=%s).",
            len(candidates),
            config.db_path,
            config.label,
            successful_submissions,
        )
        if config.dry_run:
            logging.info("Dry run mode is offline: no AWS API calls will be made.")

        stats = {
            "eligible": 0,
            "added": 0,
            "dry_run": 0,
            "errors": 0,
        }

        for start in range(0, len(candidates), config.batch_size):
            batch = candidates[start : start + config.batch_size]
            logging.info("Processing batch %s-%s of %s.", start + 1, start + len(batch), len(candidates))

            for row in batch:
                email_address = str(row["email_address"]).strip().lower()
                if not email_address:
                    continue

                stats["eligible"] += 1

                if config.dry_run:
                    stats["dry_run"] += 1
                    logging.info(
                        "Dry run would add: %s (source=%s/%s aws_reason=%s last_seen=%s count=%s)",
                        email_address,
                        SOURCE_BOUNCE_TYPE,
                        row.get("bounce_subtype"),
                        AWS_SUPPRESSION_REASON,
                        row.get("last_seen"),
                        row.get("bounce_count"),
                    )
                    continue

                try:
                    add_to_suppression_list(client, email_address, config.max_retries)
                except ClientError as exc:
                    stats["errors"] += 1
                    record_error = str(exc)
                    logging.error("SES insert failed for %s: %s", email_address, exc)
                    record_suppression_submission(
                        conn,
                        email_address=email_address,
                        source_bounce_type=SOURCE_BOUNCE_TYPE,
                        source_bounce_subtype=str(row.get("bounce_subtype") or ""),
                        aws_reason=AWS_SUPPRESSION_REASON,
                        bounce_count=int(row.get("bounce_count") or 0),
                        last_seen=row.get("last_seen"),
                        status="error",
                        last_error=record_error,
                    )
                    conn.commit()
                    continue

                stats["added"] += 1
                record_suppression_submission(
                    conn,
                    email_address=email_address,
                    source_bounce_type=SOURCE_BOUNCE_TYPE,
                    source_bounce_subtype=str(row.get("bounce_subtype") or ""),
                    aws_reason=AWS_SUPPRESSION_REASON,
                    bounce_count=int(row.get("bounce_count") or 0),
                    last_seen=row.get("last_seen"),
                    status="success",
                    last_error=None,
                )
                conn.commit()
                logging.info(
                    "Added to SES global suppression: %s (source=%s/%s aws_reason=%s last_seen=%s count=%s)",
                    email_address,
                    SOURCE_BOUNCE_TYPE,
                    row.get("bounce_subtype"),
                    AWS_SUPPRESSION_REASON,
                    row.get("last_seen"),
                    row.get("bounce_count"),
                )

                if config.delay_seconds > 0:
                    time.sleep(config.delay_seconds)

        logging.info(
            "Done. eligible=%s added=%s dry_run=%s errors=%s",
            stats["eligible"],
            stats["added"],
            stats["dry_run"],
            stats["errors"],
        )
    finally:
        conn.close()


def run_status(config: SyncConfig, recent: int) -> None:
    conn = connect_db(config.db_path, config.label)
    try:
        pending = count_pending_suppression_candidates(
            conn,
            bounce_type=SOURCE_BOUNCE_TYPE,
        )
        success = count_successful_suppression_submissions(conn)
        failed = count_failed_suppression_submissions(conn)
        recent_rows = recent_suppression_submissions(conn, limit=recent)

        print("AWS Suppression Sync Status")
        print("===========================")
        print(f"Database: {config.db_path}")
        print(f"Label: {config.label}")
        print(f"Pending candidates: {pending}")
        print(f"Successful submissions: {success}")
        print(f"Failed submissions: {failed}")
        print()
        print(f"Recent submission attempts (showing up to {recent})")
        print("--------------------------------------------------")
        if not recent_rows:
            print("No submission attempts recorded yet.")
        else:
            for row in recent_rows:
                print(
                    f"{row['email_address']} | status={row['status']} | "
                    f"source={row['source_bounce_type']}/{row['source_bounce_subtype']} | "
                    f"reason={row['aws_reason']} | last_seen={row['last_seen']} | "
                    f"attempted={row['last_attempt_at']} | error={row['last_error'] or ''}"
                )
    finally:
        conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = parse_args()
    config = resolve_config(args)
    if config.command == "status":
        run_status(config, recent=int(getattr(args, "recent", 10)))
    else:
        run_sync(config)


if __name__ == "__main__":
    main()
