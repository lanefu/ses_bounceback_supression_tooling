#!/usr/bin/env python3

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

from bounceback_store import (
    AWS_SUPPRESSION_REASON,
    count_successful_suppression_submissions,
    query_suppression_rows,
    record_suppression_submission,
)


SOURCE_BOUNCE_TYPE = "Permanent"


@dataclass(frozen=True)
class SuppressionResult:
    email_address: str
    result: str
    error: Optional[str] = None


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


def sync_candidate_rows(
    conn,
    *,
    rows: list[dict[str, Any]],
    client: Any,
    max_retries: int,
    delay_seconds: float = 0.0,
) -> list[SuppressionResult]:
    results: list[SuppressionResult] = []
    for row in rows:
        email_address = str(row["email_address"]).strip().lower()
        if not email_address:
            continue

        try:
            add_to_suppression_list(client, email_address, max_retries)
        except ClientError as exc:
            record_error = str(exc)
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
            results.append(SuppressionResult(email_address=email_address, result="error", error=record_error))
            continue

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
        results.append(SuppressionResult(email_address=email_address, result="success"))

        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return results


def sync_pending_permanent(
    conn,
    *,
    client: Any,
    limit: Optional[int],
    max_retries: int,
    delay_seconds: float,
) -> list[SuppressionResult]:
    rows = query_suppression_rows(conn, limit=limit, bounce_type=SOURCE_BOUNCE_TYPE, exclude_submitted=True)
    return sync_candidate_rows(conn, rows=rows, client=client, max_retries=max_retries, delay_seconds=delay_seconds)


def successful_submission_count(conn) -> int:
    return count_successful_suppression_submissions(conn)
