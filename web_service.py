#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from aws_suppression import ses_client, sync_candidate_rows
from bounceback_store import (
    connect_db,
    count_bounce_events,
    insert_bounce_event,
    latest_suppression_row_for_email,
    successful_suppression_exists,
)
from logging_config import configure_logging
from ses_bounce import normalize_header_value, parse_ses_bounce
from ses_config import ServiceConfig, load_config
from sns_verifier import confirm_subscription, verify_sns_signature
from telemetry import configure_telemetry, instrument_fastapi, telemetry


logger = logging.getLogger("ses_bounce.web")


class RootPathPrefixMiddleware:
    def __init__(self, app: Any, root_path: str) -> None:
        self.app = app
        self.root_path = root_path.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self.root_path:
            path = str(scope.get("path") or "")
            scope["root_path"] = self.root_path
            if path == self.root_path:
                scope = {**scope, "path": "/"}
            elif path.startswith(self.root_path + "/"):
                scope = {**scope, "path": path[len(self.root_path) :] or "/"}
        await self.app(scope, receive, send)


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    config = config or load_config()
    configure_logging(config.logging)
    configure_telemetry(
        config.otel.service_name,
        endpoint=config.otel.exporter_otlp_endpoint,
        resource_attributes=config.otel.resource_attributes,
    )
    app = FastAPI(title="SES Bounce Webhook", root_path=config.web.root_path)
    if config.web.root_path:
        app.add_middleware(RootPathPrefixMiddleware, root_path=config.web.root_path)
    app.state.config = config
    instrument_fastapi(app)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        conn = connect_db(config.database.path, config.imap.label)
        try:
            return {"status": "ok", "events": count_bounce_events(conn)}
        finally:
            conn.close()

    @app.post("/sns/bounce")
    async def sns_bounce(request: Request) -> dict[str, Any]:
        with telemetry.latency(telemetry.webhook_latency, {"route": "/sns/bounce"}):
            message = await read_sns_message(request)
            message_type = str(message.get("Type", ""))
            sns_message_id = normalize_header_value(message.get("MessageId"))
            telemetry.sns_messages.add(1, {"type": message_type or "unknown"})
            logger.info(
                "SNS message received",
                extra={"sns_type": message_type or "unknown", "sns_message_id": sns_message_id},
            )

            if config.web.verify_sns and not config.web.unsafe_skip_sns_verify:
                try:
                    with telemetry.span("sns.verify", {"sns.type": message_type}):
                        verify_sns_signature(message)
                except Exception as exc:
                    telemetry.sns_verification_failures.add(1, {"type": message_type or "unknown"})
                    logger.warning(
                        "SNS signature verification failed",
                        extra={"sns_type": message_type or "unknown", "sns_message_id": sns_message_id, "error": str(exc)},
                    )
                    raise HTTPException(status_code=403, detail=f"Invalid SNS signature: {exc}") from exc

            if message_type == "SubscriptionConfirmation":
                with telemetry.span("sns.confirm_subscription"):
                    response = confirm_subscription(message, profile=config.aws.profile, region=config.aws.region)
                logger.info("SNS subscription confirmed", extra={"sns_message_id": sns_message_id})
                return {"status": "confirmed", "subscription_arn": response.get("SubscriptionArn")}

            if message_type != "Notification":
                raise HTTPException(status_code=400, detail=f"Unsupported SNS message type: {message_type}")

            raw_inner = message.get("Message")
            if not isinstance(raw_inner, str):
                raise HTTPException(status_code=400, detail="SNS Notification Message must be a JSON string")
            try:
                payload = json.loads(raw_inner)
            except json.JSONDecodeError as exc:
                raise HTTPException(status_code=400, detail="SNS Notification Message is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise HTTPException(status_code=400, detail="SNS Notification Message must decode to a JSON object")

            parsed = parse_ses_bounce(payload)
            if parsed is None:
                raise HTTPException(status_code=400, detail="SNS Notification does not contain an SES bounce payload")

            conn = connect_db(config.database.path, config.imap.label)
            try:
                with telemetry.span("db.insert_bounce"):
                    with conn:
                        event_id, recipient_count = insert_bounce_event(
                            conn,
                            label=config.imap.label,
                            source="sns",
                            imap_uid=None,
                            sns_message_id=sns_message_id,
                            message_id="",
                            subject=str(message.get("Subject") or ""),
                            from_header="",
                            email_date=str(message.get("Timestamp") or ""),
                            raw_body=raw_inner,
                            raw_json=payload,
                        )

                event_outcome = "duplicate" if event_id == 0 else "inserted"
                telemetry.bounce_events.add(1, {"outcome": event_outcome})
                logger.info(
                    "Bounce event processed",
                    extra={
                        "event_outcome": event_outcome,
                        "event_id": event_id or "",
                        "recipient_count": recipient_count,
                        "bounce_type": str(parsed.bounce.get("bounceType") or "Unknown"),
                        "bounce_subtype": str(parsed.bounce.get("bounceSubType") or "Unknown"),
                        "sns_message_id": sns_message_id,
                    },
                )
                for recipient in parsed.recipients:
                    telemetry.bounce_recipients.add(
                        1,
                        {
                            "bounce_type": str(parsed.bounce.get("bounceType") or "Unknown"),
                            "bounce_subtype": str(parsed.bounce.get("bounceSubType") or "Unknown"),
                        },
                    )

                suppression_results: list[dict[str, Any]] = []
                if event_id != 0 and str(parsed.bounce.get("bounceType", "")).lower() == "permanent":
                    client = ses_client(config.aws.profile, config.aws.region)
                    for recipient in parsed.recipients:
                        email_address = normalize_header_value(recipient.get("emailAddress")).lower()
                        if not email_address:
                            continue
                        if successful_suppression_exists(conn, email_address):
                            telemetry.aws_suppression_attempts.add(1, {"result": "skipped"})
                            suppression_results.append({"email_address": email_address, "result": "skipped"})
                            logger.info("Suppression write skipped", extra={"result": "skipped", "email_address": email_address})
                            continue
                        row = latest_suppression_row_for_email(conn, email_address)
                        if not row:
                            continue
                        with telemetry.span("aws.suppress_destination"):
                            results = sync_candidate_rows(
                                conn,
                                rows=[row],
                                client=client,
                                max_retries=config.aws.retry_count,
                                delay_seconds=config.aws.delay_seconds,
                            )
                        for result in results:
                            telemetry.aws_suppression_attempts.add(1, {"result": result.result})
                            logger.info(
                                "Suppression write attempted",
                                extra={
                                    "result": result.result,
                                    "email_address": result.email_address,
                                    "error": result.error or "",
                                },
                            )
                            suppression_results.append(
                                {
                                    "email_address": result.email_address,
                                    "result": result.result,
                                    "error": result.error,
                                }
                            )

                return {
                    "status": event_outcome,
                    "event_id": event_id or None,
                    "recipients": recipient_count,
                    "bounce_type": parsed.bounce.get("bounceType"),
                    "bounce_subtype": parsed.bounce.get("bounceSubType"),
                    "suppression_results": suppression_results,
                }
            finally:
                conn.close()

    return app


async def read_sns_message(request: Request) -> dict[str, Any]:
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Empty request body")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Request body is not JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SES bounce SNS webhook service.")
    parser.add_argument("--config", default=None, help="Optional TOML config path.")
    parser.add_argument("--db", default=None, help="SQLite database path.")
    parser.add_argument("--label", default=None, help="Source label for DB rows.")
    parser.add_argument("--host", default=None, help="Bind host.")
    parser.add_argument("--port", type=int, default=None, help="Bind port.")
    parser.add_argument("--log-level", default=None, help="Logging level override.")
    parser.add_argument("--log-format", choices=("text", "json"), default=None, help="Logging format override.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(
        args.config,
        {
            "db_path": args.db,
            "label": args.label,
            "host": args.host,
            "port": args.port,
            "log_level": args.log_level,
            "log_format": args.log_format,
        },
    )
    import uvicorn

    uvicorn.run(
        create_app(config),
        host=config.web.host,
        port=config.web.port,
        proxy_headers=config.web.proxy_headers,
        forwarded_allow_ips=config.web.forwarded_allow_ips,
        access_log=config.logging.access_log,
        log_level=config.logging.uvicorn_log_level,
    )


app = create_app()


if __name__ == "__main__":
    main()
