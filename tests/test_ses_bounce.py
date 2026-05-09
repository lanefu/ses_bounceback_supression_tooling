from __future__ import annotations

import json
from email.message import EmailMessage

from ses_bounce import (
    extract_json_from_text,
    extract_text_payload,
    normalize_header_value,
    normalize_ses_payload,
    parse_ses_bounce,
)


def test_extract_text_payload_and_json_round_trip():
    msg = EmailMessage()
    msg["Subject"] = "  bounce report  "
    msg.set_content("plain text line\n")
    msg.add_attachment(
        b"ignore me",
        maintype="application",
        subtype="octet-stream",
        filename="payload.bin",
    )

    text = extract_text_payload(msg)

    assert "plain text line" in text
    assert "ignore me" not in text
    assert normalize_header_value(msg["Subject"]) == "bounce report"
    assert extract_json_from_text(f"noise {json.dumps({'hello': 'world'})} tail") == {"hello": "world"}


def test_parse_ses_bounce_normalizes_wrapped_payload_and_filters_recipients():
    wrapped_payload = {
        "Message": json.dumps(
            {
                "mail": {
                    "messageId": " ses-message-123 ",
                    "timestamp": " 2026-05-09T12:00:00Z ",
                },
                "bounce": {
                    "timestamp": " 2026-05-09T12:01:00Z ",
                    "bounceType": "Permanent",
                    "bounceSubType": "General",
                    "bouncedRecipients": [
                        {
                            "emailAddress": "User@Example.com",
                            "action": "failed",
                            "status": "5.1.1",
                        },
                        "skip-me",
                    ],
                },
            }
        )
    }

    normalized = normalize_ses_payload(wrapped_payload)
    parsed = parse_ses_bounce(wrapped_payload)

    assert normalized["bounce"]["bounceType"] == "Permanent"
    assert parsed is not None
    assert parsed.ses_message_id == "ses-message-123"
    assert parsed.bounce_timestamp == "2026-05-09T12:01:00Z"
    assert parsed.mail["timestamp"] == " 2026-05-09T12:00:00Z "
    assert parsed.recipients == [
        {
            "emailAddress": "User@Example.com",
            "action": "failed",
            "status": "5.1.1",
        }
    ]

