#!/usr/bin/env python3

from __future__ import annotations

import json
from dataclasses import dataclass
from email.message import Message
from typing import Any, Optional


@dataclass(frozen=True)
class ParsedBounce:
    payload: dict[str, Any]
    mail: dict[str, Any]
    bounce: dict[str, Any]
    ses_message_id: str
    bounce_timestamp: str
    recipients: list[dict[str, Any]]


def normalize_header_value(value: Optional[Any]) -> str:
    if not value:
        return ""
    return " ".join(str(value).split()).strip()


def extract_text_payload(msg: Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get_content_disposition()
            if disposition == "attachment":
                continue
            if content_type not in {"text/plain", "text/html", "application/json"}:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
        return "\n".join(parts)

    payload = msg.get_payload(decode=True)
    if payload:
        charset = msg.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    payload_text = msg.get_payload()
    return payload_text if isinstance(payload_text, str) else ""


def extract_json_from_text(text: str) -> Optional[dict[str, Any]]:
    if not text:
        return None
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx == -1 or end_idx == -1 or end_idx <= start_idx:
        return None
    candidate = text[start_idx : end_idx + 1].strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def normalize_ses_payload(payload_json: dict[str, Any]) -> dict[str, Any]:
    if "bounce" in payload_json:
        return payload_json

    inner = payload_json.get("Message")
    if isinstance(inner, str):
        try:
            decoded = json.loads(inner)
        except json.JSONDecodeError:
            return payload_json
        if isinstance(decoded, dict):
            return decoded
    return payload_json


def parse_ses_bounce(payload_json: dict[str, Any]) -> Optional[ParsedBounce]:
    payload = normalize_ses_payload(payload_json)
    mail_json = payload.get("mail", {})
    bounce_json = payload.get("bounce", {})
    if not isinstance(mail_json, dict):
        mail_json = {}
    if not isinstance(bounce_json, dict) or not bounce_json:
        return None

    recipients = bounce_json.get("bouncedRecipients", [])
    if not isinstance(recipients, list):
        recipients = []

    return ParsedBounce(
        payload=payload,
        mail=mail_json,
        bounce=bounce_json,
        ses_message_id=normalize_header_value(mail_json.get("messageId")),
        bounce_timestamp=normalize_header_value(bounce_json.get("timestamp")),
        recipients=[recipient for recipient in recipients if isinstance(recipient, dict)],
    )
