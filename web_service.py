#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import logging
from io import StringIO
from html import escape
from urllib.parse import urlencode
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from aws_suppression import ses_client, sync_candidate_rows
from bounceback_store import (
    connect_db,
    count_bounce_events,
    build_triage_report,
    insert_bounce_event,
    latest_suppression_row_for_email,
    normalize_triage_bucket_filter,
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


def authorize_report_access(request: Request, report_token: str | None) -> None:
    if not report_token:
        return

    presented_token = request.query_params.get("token")
    if not presented_token:
        auth_header = request.headers.get("authorization", "")
        scheme, _, value = auth_header.partition(" ")
        if scheme.lower() == "bearer":
            presented_token = value.strip()

    if presented_token != report_token:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid report token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_report_token(request: Request, report_token: str | None) -> str | None:
    if not report_token:
        return None
    token = request.query_params.get("token")
    if token:
        return token
    auth_header = request.headers.get("authorization", "")
    scheme, _, value = auth_header.partition(" ")
    if scheme.lower() == "bearer":
        candidate = value.strip()
        if candidate:
            return candidate
    return None


def build_report_href(
    path: str,
    *,
    token: str | None = None,
    bucket: str | None = None,
    limit: int | None = None,
) -> str:
    params: dict[str, Any] = {}
    if token:
        params["token"] = token
    if bucket:
        params["bucket"] = bucket
    if limit is not None:
        params["limit"] = limit
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


def resolve_triage_bucket(bucket: str | None) -> str | None:
    if bucket is None:
        return None
    try:
        return normalize_triage_bucket_filter(bucket)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def triage_bucket_slug(bucket: str | None) -> str | None:
    if bucket is None:
        return None
    normalized = normalize_triage_bucket_filter(bucket)
    if normalized == "remove now":
        return "remove-now"
    if normalized == "watch":
        return "watch"
    if normalized == "ignore for now":
        return "ignore-for-now"
    return None


def render_triage_html(report: dict[str, Any], *, report_token: str | None = None) -> str:
    summary = report.get("summary", {})
    sections = report.get("sections", [])
    current_bucket = report.get("bucket_filter")
    current_bucket_slug = triage_bucket_slug(current_bucket)
    csv_href = build_report_href("/reports/triage.csv", token=report_token, bucket=current_bucket_slug)
    json_href = build_report_href("/reports/triage.json", token=report_token, bucket=current_bucket_slug)
    filter_links = [
        ("All recommendations", None),
        ("Recommended removals", "remove-now"),
        ("Recommended watch", "watch"),
        ("Recommended ignores", "ignore-for-now"),
    ]
    lines = [
        "<!doctype html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>SES Bounce Triage Recommendations</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;margin:2rem;color:#1f2937;background:#f8fafc;}",
        "main{max-width:1400px;margin:0 auto;}",
        "h1,h2,h3,p{margin:0 0 0.75rem 0;}",
        ".summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:0.75rem;margin:1rem 0 2rem;}",
        ".card{background:#fff;border:1px solid #dbe2ea;border-radius:12px;padding:1rem;box-shadow:0 1px 2px rgba(15,23,42,0.05);}",
        ".card strong{display:block;font-size:1.6rem;line-height:1.1;margin-top:0.35rem;}",
        ".cardlink{display:block;text-decoration:none;color:inherit;}",
        ".cardlink.active{border-color:#7c3aed;box-shadow:0 0 0 1px rgba(124,58,237,0.25),0 1px 2px rgba(15,23,42,0.05);}",
        ".filters{display:flex;gap:0.5rem;flex-wrap:wrap;margin:0 0 1rem 0;}",
        ".filters a{display:inline-block;padding:0.45rem 0.75rem;border-radius:999px;border:1px solid #dbe2ea;background:#fff;color:#334155;text-decoration:none;}",
        ".filters a.active{background:#0f172a;color:#fff;border-color:#0f172a;}",
        ".section{margin:1.5rem 0 2rem;}",
        ".section header{display:flex;flex-direction:column;gap:0.25rem;margin-bottom:0.75rem;}",
        ".section-actions{display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center;}",
        ".section-actions a{display:inline-block;padding:0.35rem 0.6rem;border-radius:0.5rem;border:1px solid #dbe2ea;background:#fff;color:#334155;text-decoration:none;font-size:0.85rem;}",
        ".section table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dbe2ea;border-radius:12px;overflow:hidden;}",
        "th,td{padding:0.7rem 0.8rem;border-bottom:1px solid #e5e7eb;vertical-align:top;text-align:left;font-size:0.93rem;}",
        "th{background:#eef2f7;font-size:0.82rem;text-transform:uppercase;letter-spacing:0.04em;}",
        "tr:last-child td{border-bottom:none;}",
        ".muted{color:#6b7280;}",
        ".badge{display:inline-block;padding:0.16rem 0.45rem;border-radius:999px;font-size:0.78rem;font-weight:600;background:#e5e7eb;}",
        ".remove{background:#fee2e2;color:#991b1b;}",
        ".watch{background:#fef3c7;color:#92400e;}",
        ".ignore{background:#dbeafe;color:#1d4ed8;}",
        "a.download{display:inline-block;margin-right:0.75rem;padding:0.5rem 0.8rem;border-radius:0.6rem;background:#0f172a;color:#fff;text-decoration:none;font-size:0.9rem;}",
        "a.download:hover{background:#1e293b;}",
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;}",
        "</style>",
        "</head>",
        "<body>",
        "<main>",
        "<h1>SES Bounce Triage Recommendations</h1>",
        "<p class='muted'>These are the app's recommended dispositions, not click-to-act controls.</p>",
        f"<p class='muted'>Generated at {escape(str(report.get('generated_at', '')))}</p>",
        "<div class='filters'>",
    ]

    for label, bucket_value in filter_links:
        href = build_report_href("/reports/triage", token=report_token, bucket=bucket_value)
        active = " active" if ((bucket_value is None and current_bucket is None) or bucket_value == current_bucket) else ""
        lines.append(f"<a class='{active.strip()}' href='{escape(href)}'>{escape(label)}</a>")
    lines.append("</div>")
    lines.append("<p>")
    lines.append(f"<a class='download' href='{escape(csv_href)}'>Download CSV</a>")
    lines.append(f"<a class='download' href='{escape(json_href)}'>Download JSON</a>")
    lines.append("</p>")
    lines.append("<div class='summary'>")

    summary_items = [
        ("Total addresses", summary.get("total_addresses", 0), None),
        ("Recommended removals", summary.get("remove now", 0), "remove-now"),
        ("Recommended watch", summary.get("watch", 0), "watch"),
        ("Recommended ignores", summary.get("ignore for now", 0), "ignore-for-now"),
    ]
    for label, value, bucket_value in summary_items:
        href = build_report_href("/reports/triage", token=report_token, bucket=bucket_value)
        active = " active" if ((bucket_value is None and current_bucket is None) or bucket_value == current_bucket) else ""
        lines.extend(
            [
                f"<a class='card cardlink{active}' href='{escape(href)}'>",
                f"<span class='muted'>{escape(str(label))}</span>",
                f"<strong>{escape(str(value))}</strong>",
                "</a>",
            ]
        )

    lines.append("</div>")
    for section in sections:
        rows = section.get("rows", [])
        badge_class = "remove" if section.get("bucket") == "remove now" else "watch" if section.get("bucket") == "watch" else "ignore"
        bucket_slug = "remove-now" if section.get("bucket") == "remove now" else "watch" if section.get("bucket") == "watch" else "ignore-for-now"
        view_href = build_report_href("/reports/triage", token=report_token, bucket=bucket_slug)
        csv_section_href = build_report_href("/reports/triage.csv", token=report_token, bucket=bucket_slug)
        json_section_href = build_report_href("/reports/triage.json", token=report_token, bucket=bucket_slug)
        lines.extend(
            [
                f"<section class='section' id='{escape(str(section.get('key', '')))}'>",
                "<header>",
                f"<h2>{escape(str(section.get('title', '')))}</h2>",
                f"<p class='muted'>{escape(str(section.get('subtitle', '')))}</p>",
                "<div class='section-actions'>",
                f"<span><span class='badge {badge_class}'>{escape(str(section.get('bucket', '')))}</span> <span class='muted'>Rows shown: {len(rows)}</span></span>",
                f"<a href='{escape(view_href)}'>View bucket</a>",
                f"<a href='{escape(csv_section_href)}'>CSV</a>",
                f"<a href='{escape(json_section_href)}'>JSON</a>",
                "</div>",
                "</header>",
            ]
        )
        if not rows:
            lines.append("<p class='muted'>No rows in this section.</p>")
        else:
            lines.extend(
                [
                    "<table>",
                    "<thead>",
                    "<tr>",
                    "<th>Email</th>",
                    "<th>Recommendation</th>",
                    "<th>Counts</th>",
                    "<th>Last seen</th>",
                    "<th>Bounce type</th>",
                    "<th>Subtype</th>",
                    "<th>Diagnostic</th>",
                    "<th>Last subject</th>",
                    "<th>References</th>",
                    "</tr>",
                    "</thead>",
                    "<tbody>",
                ]
            )
            for row in rows:
                refs = " / ".join(
                    part
                    for part in (
                        str(row.get("last_message_id") or "").strip(),
                        str(row.get("last_ses_message_id") or "").strip(),
                    )
                    if part
                )
                lines.extend(
                    [
                        "<tr>",
                        f"<td><code>{escape(str(row.get('email_address', '')))}</code></td>",
                        f"<td><span class='badge {badge_class}'>App recommendation: {escape(str(row.get('decision_bucket', '')))}</span><br><span class='muted'>{escape(str(row.get('decision_reason', '')))}</span></td>",
                        f"<td>total {escape(str(row.get('total_count', 0)))}<br>permanent {escape(str(row.get('permanent_count', 0)))}<br>transient {escape(str(row.get('transient_count', 0)))}</td>",
                        f"<td>{escape(str(row.get('last_seen', '')))}</td>",
                        f"<td>{escape(str(row.get('bounce_type', '')))}</td>",
                        f"<td>{escape(str(row.get('bounce_subtype', '')))}</td>",
                        f"<td>{escape(str(row.get('diagnostic_code', '')))}</td>",
                        f"<td>{escape(str(row.get('last_subject', '')))}</td>",
                        f"<td>{escape(refs)}</td>",
                        "</tr>",
                    ]
                )
            lines.extend(["</tbody>", "</table>"])
        lines.append("</section>")

    lines.extend(["</main>", "</body>", "</html>"])
    return "".join(lines)


def render_triage_csv(report: dict[str, Any]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
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
    for section in report.get("sections", []):
        for row in section.get("rows", []):
            writer.writerow(
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
    return buffer.getvalue()


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

    def load_triage_report(limit: int | None = None, bucket: str | None = None) -> dict[str, Any]:
        conn = connect_db(config.database.path, config.imap.label)
        try:
            return build_triage_report(conn, limit=limit, bucket=resolve_triage_bucket(bucket))
        finally:
            conn.close()

    @app.get("/reports/triage", response_class=HTMLResponse)
    def triage_report(request: Request, limit: int | None = None, bucket: str | None = None) -> HTMLResponse:
        authorize_report_access(request, config.web.report_token)
        report = load_triage_report(limit=limit, bucket=bucket)
        return HTMLResponse(render_triage_html(report, report_token=get_report_token(request, config.web.report_token)))

    @app.get("/reports/triage.json")
    def triage_report_json(request: Request, limit: int | None = None, bucket: str | None = None) -> JSONResponse:
        authorize_report_access(request, config.web.report_token)
        report = load_triage_report(limit=limit, bucket=bucket)
        return JSONResponse(
            content=report,
            headers={"Content-Disposition": f'attachment; filename="ses-bounce-triage{("-" + bucket.replace(" ", "-") if bucket else "")}.json"'},
        )

    @app.get("/reports/triage.csv")
    def triage_report_csv(request: Request, limit: int | None = None, bucket: str | None = None) -> Response:
        authorize_report_access(request, config.web.report_token)
        report = load_triage_report(limit=limit, bucket=bucket)
        return Response(
            content=render_triage_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="ses-bounce-triage{("-" + bucket.replace(" ", "-") if bucket else "")}.csv"'},
        )

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
