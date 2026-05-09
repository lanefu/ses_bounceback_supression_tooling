from __future__ import annotations

import json

from fastapi.testclient import TestClient

import web_service
from bounceback_store import count_bounce_events, count_successful_suppression_submissions, connect_db
from ses_config import AwsConfig, DatabaseConfig, ImapConfig, ServiceConfig, WebConfig


def _config(db_path: str, *, verify_sns: bool = False, unsafe_skip_sns_verify: bool = True) -> ServiceConfig:
    return ServiceConfig(
        database=DatabaseConfig(path=db_path),
        imap=ImapConfig(label="test-label"),
        aws=AwsConfig(region="us-east-1", retry_count=0, delay_seconds=0.0),
        web=WebConfig(verify_sns=verify_sns, unsafe_skip_sns_verify=unsafe_skip_sns_verify),
    )


def _sns_notification(message_id: str = "sns-message-1") -> dict[str, str]:
    payload = {
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
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:bounces",
        "Subject": "Amazon SES Notification",
        "Timestamp": "2026-05-09T12:01:03Z",
        "Message": json.dumps(payload),
    }


def test_webhook_stores_permanent_bounce_and_suppresses_once(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bouncebacks.sqlite3")
    calls: list[dict[str, str]] = []

    class FakeSesClient:
        def put_suppressed_destination(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(web_service, "ses_client", lambda profile, region: FakeSesClient())
    client = TestClient(web_service.create_app(_config(db_path)))

    response = client.post("/sns/bounce", json=_sns_notification())
    duplicate = client.post("/sns/bounce", json=_sns_notification())

    assert response.status_code == 200
    assert response.json()["status"] == "inserted"
    assert response.json()["recipients"] == 1
    assert response.json()["suppression_results"][0]["result"] == "success"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert calls == [{"EmailAddress": "user@example.com", "Reason": "BOUNCE"}]

    conn = connect_db(db_path, "test-label")
    try:
        assert count_bounce_events(conn) == 1
        assert count_successful_suppression_submissions(conn) == 1
    finally:
        conn.close()


def test_webhook_rejects_bad_sns_signature(tmp_path, monkeypatch):
    db_path = str(tmp_path / "bouncebacks.sqlite3")

    def fail_verify(message):
        raise ValueError("bad signature")

    monkeypatch.setattr(web_service, "verify_sns_signature", fail_verify)
    client = TestClient(web_service.create_app(_config(db_path, verify_sns=True, unsafe_skip_sns_verify=False)))

    response = client.post("/sns/bounce", json=_sns_notification())

    assert response.status_code == 403
    assert "Invalid SNS signature" in response.json()["detail"]


def test_create_app_honors_root_path(tmp_path):
    db_path = str(tmp_path / "bouncebacks.sqlite3")
    config = ServiceConfig(
        database=DatabaseConfig(path=db_path),
        imap=ImapConfig(label="test-label"),
        aws=AwsConfig(region="us-east-1", retry_count=0, delay_seconds=0.0),
        web=WebConfig(root_path="/proxy-prefix", verify_sns=False, unsafe_skip_sns_verify=True),
    )

    app = web_service.create_app(config)
    client = TestClient(app)

    assert app.root_path == "/proxy-prefix"
    assert client.app.root_path == "/proxy-prefix"
    assert client.get("/healthz").status_code == 200
    assert client.get("/proxy-prefix/healthz").status_code == 200
    assert client.get("/healthz").json() == {"status": "ok"}
