#!/usr/bin/env python3

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding


SNS_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
    "UnsubscribeConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
}


def canonical_message(message: dict[str, Any]) -> bytes:
    message_type = str(message.get("Type", ""))
    fields = SNS_FIELDS.get(message_type)
    if not fields:
        raise ValueError(f"Unsupported SNS message type: {message_type}")

    lines: list[str] = []
    for field in fields:
        value = message.get(field)
        if value is None or (field == "Subject" and value == ""):
            continue
        lines.append(field)
        lines.append(str(value))
    return ("\n".join(lines) + "\n").encode("utf-8")


def validate_signing_cert_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https":
        raise ValueError("SNS SigningCertURL must use https")
    if not (host == "sns.amazonaws.com" or host.startswith("sns.") and host.endswith(".amazonaws.com")):
        raise ValueError("SNS SigningCertURL host is not trusted")
    if not parsed.path.startswith("/SimpleNotificationService-"):
        raise ValueError("SNS SigningCertURL path is not trusted")


def verify_sns_signature(message: dict[str, Any]) -> None:
    signature = message.get("Signature")
    cert_url = message.get("SigningCertURL")
    version = str(message.get("SignatureVersion", "1"))
    if not signature or not cert_url:
        raise ValueError("SNS message is missing signature fields")

    validate_signing_cert_url(str(cert_url))
    response = httpx.get(str(cert_url), timeout=5.0)
    response.raise_for_status()
    cert = x509.load_pem_x509_certificate(response.content)
    public_key = cert.public_key()
    digest = hashes.SHA1() if version == "1" else hashes.SHA256()
    public_key.verify(base64.b64decode(signature), canonical_message(message), padding.PKCS1v15(), digest)


def confirm_subscription(message: dict[str, Any], *, profile: str | None, region: str | None) -> dict[str, Any]:
    import boto3

    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("sns")
    return client.confirm_subscription(TopicArn=message["TopicArn"], Token=message["Token"], AuthenticateOnUnsubscribe="true")
