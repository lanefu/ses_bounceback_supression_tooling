from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from bounceback_store import build_triage_report, connect_db, insert_bounce_event
from ses_config import AwsConfig, DatabaseConfig, ImapConfig, ServiceConfig, WebConfig
from fetch_bouncebacks import emit_report
import web_service
from fastapi.testclient import TestClient


def _insert_bounce(
    conn,
    *,
    email_address: str,
    bounce_type: str,
    timestamp: str,
    message_id: str,
    sns_message_id: str,
) -> None:
    payload = {
        "mail": {"messageId": f"ses-{message_id}"},
        "bounce": {
            "timestamp": timestamp,
            "bounceType": bounce_type,
            "bounceSubType": "General",
            "bouncedRecipients": [
                {
                    "emailAddress": email_address,
                    "action": "failed",
                    "status": "5.1.1",
                    "diagnosticCode": "smtp; 550 5.1.1 user unknown",
                }
            ],
        },
    }
    event_id, recipient_count = insert_bounce_event(
        conn,
        label="test-label",
        source="sns",
        imap_uid=None,
        sns_message_id=sns_message_id,
        message_id=message_id,
        subject="Bounce notice",
        from_header="MAILER-DAEMON@example.com",
        email_date=timestamp,
        raw_body=json.dumps(payload),
        raw_json=payload,
    )
    assert event_id > 0
    assert recipient_count == 1
    conn.commit()


def _triage_config(db_path: str, *, report_token: str | None = None) -> ServiceConfig:
    return ServiceConfig(
        database=DatabaseConfig(path=db_path),
        imap=ImapConfig(label="test-label"),
        aws=AwsConfig(region="us-east-1", retry_count=0, delay_seconds=0.0),
        web=WebConfig(verify_sns=False, unsafe_skip_sns_verify=True, report_token=report_token),
    )


def test_triage_report_groups_decisions_and_orders_rows(tmp_path):
    db_path = str(tmp_path / "bouncebacks.sqlite3")
    conn = connect_db(db_path, "test-label")
    try:
        _insert_bounce(
            conn,
            email_address="remove@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:00:00Z",
            message_id="message-remove-1",
            sns_message_id="sns-remove-1",
        )
        _insert_bounce(
            conn,
            email_address="remove@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:10:00Z",
            message_id="message-remove-2",
            sns_message_id="sns-remove-2",
        )
        _insert_bounce(
            conn,
            email_address="remove@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:20:00Z",
            message_id="message-remove-3",
            sns_message_id="sns-remove-3",
        )
        _insert_bounce(
            conn,
            email_address="perm@example.com",
            bounce_type="Permanent",
            timestamp="2026-05-09T12:30:00Z",
            message_id="message-perm-1",
            sns_message_id="sns-perm-1",
        )
        _insert_bounce(
            conn,
            email_address="watch@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:40:00Z",
            message_id="message-watch-1",
            sns_message_id="sns-watch-1",
        )
        _insert_bounce(
            conn,
            email_address="watch@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:50:00Z",
            message_id="message-watch-2",
            sns_message_id="sns-watch-2",
        )
        _insert_bounce(
            conn,
            email_address="ignore@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T13:00:00Z",
            message_id="message-ignore-1",
            sns_message_id="sns-ignore-1",
        )

        report = build_triage_report(conn)
    finally:
        conn.close()

    assert report["summary"] == {
        "total_addresses": 4,
        "remove now": 2,
        "watch": 1,
        "ignore for now": 1,
    }
    assert [section["key"] for section in report["sections"]] == [
        "remove-now-permanent",
        "remove-now-transient",
        "watch",
        "ignore",
    ]
    assert [row["email_address"] for row in report["sections"][0]["rows"]] == ["perm@example.com"]
    assert [row["email_address"] for row in report["sections"][1]["rows"]] == ["remove@example.com"]
    assert report["sections"][1]["rows"][0]["decision_bucket"] == "remove now"
    assert report["sections"][1]["rows"][0]["transient_count"] == 3
    assert report["sections"][2]["rows"][0]["decision_bucket"] == "watch"
    assert report["sections"][3]["rows"][0]["decision_bucket"] == "ignore for now"


def test_cli_report_supports_json_and_csv_formats(tmp_path):
    db_path = str(tmp_path / "bouncebacks.sqlite3")
    conn = connect_db(db_path, "test-label")
    try:
        _insert_bounce(
            conn,
            email_address="perm@example.com",
            bounce_type="Permanent",
            timestamp="2026-05-09T12:30:00Z",
            message_id="message-perm-1",
            sns_message_id="sns-perm-1",
        )
    finally:
        conn.close()

    json_buffer = io.StringIO()
    with connect_db(db_path, "test-label") as cli_conn, redirect_stdout(json_buffer):
        emit_report(cli_conn, db_path, limit=10, fmt="json")
    payload = json.loads(json_buffer.getvalue())
    assert payload["summary"]["remove now"] == 1
    assert payload["sections"][0]["rows"][0]["email_address"] == "perm@example.com"

    csv_buffer = io.StringIO()
    with connect_db(db_path, "test-label") as cli_conn, redirect_stdout(csv_buffer):
        emit_report(cli_conn, db_path, limit=10, fmt="csv")
    csv_output = csv_buffer.getvalue()
    assert "section,decision_bucket,decision_reason,email_address" in csv_output
    assert "perm@example.com" in csv_output

    filtered_buffer = io.StringIO()
    with connect_db(db_path, "test-label") as cli_conn, redirect_stdout(filtered_buffer):
        emit_report(cli_conn, db_path, limit=10, fmt="json", bucket="remove-now")
    filtered_payload = json.loads(filtered_buffer.getvalue())
    assert filtered_payload["bucket_filter"] == "remove now"
    assert filtered_payload["summary"]["total_addresses"] == 1
    assert filtered_payload["sections"][0]["rows"][0]["email_address"] == "perm@example.com"


def test_report_endpoints_require_token_and_return_matching_data(tmp_path):
    db_path = str(tmp_path / "bouncebacks.sqlite3")
    conn = connect_db(db_path, "test-label")
    try:
        _insert_bounce(
            conn,
            email_address="perm@example.com",
            bounce_type="Permanent",
            timestamp="2026-05-09T12:30:00Z",
            message_id="message-perm-1",
            sns_message_id="sns-perm-1",
        )
        _insert_bounce(
            conn,
            email_address="watch@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:40:00Z",
            message_id="message-watch-1",
            sns_message_id="sns-watch-1",
        )
        _insert_bounce(
            conn,
            email_address="watch@example.com",
            bounce_type="Transient",
            timestamp="2026-05-09T12:50:00Z",
            message_id="message-watch-2",
            sns_message_id="sns-watch-2",
        )
    finally:
        conn.close()

    client = TestClient(web_service.create_app(_triage_config(db_path, report_token="report-secret")))

    denied = client.get("/reports/triage")
    assert denied.status_code == 401

    html = client.get("/reports/triage", headers={"Authorization": "Bearer report-secret"})
    assert html.status_code == 200
    assert "SES Bounce Triage Recommendations" in html.text
    assert "These are the app's recommended dispositions" in html.text
    assert "perm@example.com" in html.text
    assert "watch@example.com" in html.text
    assert "recommendation=remove-now" in html.text or "App recommendation: remove now" in html.text
    assert "recommendation=watch" in html.text or "App recommendation: watch" in html.text
    assert "Recommended ignores" in html.text

    tokenized_html = client.get("/reports/triage?token=report-secret")
    assert tokenized_html.status_code == 200
    assert "/reports/triage.csv?token=report-secret" in tokenized_html.text
    assert "/reports/triage.json?token=report-secret" in tokenized_html.text
    assert "App recommendation: remove now" in tokenized_html.text

    dataset = client.get("/reports/triage.json?token=report-secret")
    assert dataset.status_code == 200
    payload = dataset.json()
    assert payload["summary"]["total_addresses"] == 2
    assert payload["sections"][0]["rows"][0]["email_address"] == "perm@example.com"
    assert payload["sections"][2]["rows"][0]["email_address"] == "watch@example.com"
    assert dataset.headers["content-disposition"] == 'attachment; filename="ses-bounce-triage.json"'
    assert payload["sections"][0]["rows"][0]["email_address"] in html.text

    remove_now = client.get("/reports/triage?token=report-secret&bucket=remove-now")
    assert remove_now.status_code == 200
    assert "perm@example.com" in remove_now.text
    assert "watch@example.com" not in remove_now.text
    assert "App recommendation: remove now" in remove_now.text
    assert "/reports/triage.csv?token=report-secret&amp;bucket=remove-now" in remove_now.text

    csv_response = client.get("/reports/triage.csv?token=report-secret")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-disposition"] == 'attachment; filename="ses-bounce-triage.csv"'
    assert "section,decision_bucket,decision_reason,email_address" in csv_response.text
    assert "perm@example.com" in csv_response.text
