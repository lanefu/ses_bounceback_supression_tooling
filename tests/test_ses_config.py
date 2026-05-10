from __future__ import annotations

from ses_config import load_config


def test_load_config_default_label_is_generic(monkeypatch):
    monkeypatch.delenv("SES_BOUNCE_CONFIG", raising=False)
    monkeypatch.delenv("SES_BOUNCE_LABEL", raising=False)

    config = load_config()

    assert config.imap.label == "ses_bounce_notifications"


def test_load_config_precedence_file_env_overrides(tmp_path, monkeypatch):
    config_file = tmp_path / "ses-bounce.toml"
    config_file.write_text(
        """
[database]
path = "file.sqlite3"

[imap]
user = "file-user"
password = "file-pass"
host = "file.imap.example.com"
label = "file-label"

[aws]
profile = "file-profile"
region = "us-east-1"
retry_count = 3
delay_seconds = 0.5
batch_size = 2

[web]
host = "0.0.0.0"
port = 9000
root_path = "/from-file"
proxy_headers = false
forwarded_allow_ips = "10.0.0.0/8"
verify_sns = false
unsafe_skip_sns_verify = false
report_token = "file-token"

[logging]
level = "warning"
format = "text"
access_log = false
uvicorn_log_level = "warning"

[otel]
service_name = "file-service"
exporter_otlp_endpoint = "http://file-otel:4317"
resource_attributes = "env=file"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SES_BOUNCE_DB", "env.sqlite3")
    monkeypatch.setenv("SES_BOUNCE_IMAP_USER", "env-user")
    monkeypatch.setenv("SES_BOUNCE_IMAP_PASS", "env-pass")
    monkeypatch.setenv("SES_BOUNCE_IMAP_HOST", "env.imap.example.com")
    monkeypatch.setenv("SES_BOUNCE_LABEL", "env-label")
    monkeypatch.setenv("AWS_PROFILE", "env-profile")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SES_BOUNCE_AWS_RETRY_COUNT", "8")
    monkeypatch.setenv("SES_BOUNCE_AWS_DELAY_SECONDS", "1.25")
    monkeypatch.setenv("SES_BOUNCE_AWS_BATCH_SIZE", "11")
    monkeypatch.setenv("SES_BOUNCE_WEB_HOST", "127.0.0.2")
    monkeypatch.setenv("SES_BOUNCE_WEB_PORT", "8123")
    monkeypatch.setenv("SES_BOUNCE_WEB_ROOT_PATH", "/from-env")
    monkeypatch.setenv("SES_BOUNCE_PROXY_HEADERS", "1")
    monkeypatch.setenv("SES_BOUNCE_FORWARDED_ALLOW_IPS", "127.0.0.1,10.0.0.0/8")
    monkeypatch.setenv("SES_BOUNCE_VERIFY_SNS", "1")
    monkeypatch.setenv("SES_BOUNCE_UNSAFE_SKIP_SNS_VERIFY", "no")
    monkeypatch.setenv("SES_BOUNCE_LOG_LEVEL", "debug")
    monkeypatch.setenv("SES_BOUNCE_LOG_FORMAT", "json")
    monkeypatch.setenv("SES_BOUNCE_ACCESS_LOG", "true")
    monkeypatch.setenv("SES_BOUNCE_UVICORN_LOG_LEVEL", "error")
    monkeypatch.setenv("SES_BOUNCE_REPORT_TOKEN", "env-token")
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-service")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://env-otel:4317")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "env=env")

    config = load_config(
        str(config_file),
        {
            "db_path": "override.sqlite3",
            "region": "eu-central-1",
            "port": 9443,
            "imap_user": "override-user",
            "imap_pass": "override-pass",
            "log_level": "critical",
            "log_format": "text",
        },
    )

    assert config.database.path == "override.sqlite3"
    assert config.imap.user == "override-user"
    assert config.imap.password == "override-pass"
    assert config.imap.host == "env.imap.example.com"
    assert config.imap.label == "env-label"
    assert config.aws.profile == "env-profile"
    assert config.aws.region == "eu-central-1"
    assert config.aws.retry_count == 8
    assert config.aws.delay_seconds == 1.25
    assert config.aws.batch_size == 11
    assert config.web.host == "127.0.0.2"
    assert config.web.port == 9443
    assert config.web.root_path == "/from-env"
    assert config.web.proxy_headers is True
    assert config.web.forwarded_allow_ips == "127.0.0.1,10.0.0.0/8"
    assert config.web.verify_sns is True
    assert config.web.unsafe_skip_sns_verify is False
    assert config.web.report_token == "env-token"
    assert config.logging.level == "critical"
    assert config.logging.format == "text"
    assert config.logging.access_log is True
    assert config.logging.uvicorn_log_level == "error"
    assert config.otel.service_name == "env-service"
    assert config.otel.exporter_otlp_endpoint == "http://env-otel:4317"
    assert config.otel.resource_attributes == "env=env"


def test_load_config_uses_web_root_path_from_file_and_env_override(tmp_path, monkeypatch):
    config_file = tmp_path / "ses-bounce.toml"
    config_file.write_text(
        """
[web]
root_path = "/from-file"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = load_config(str(config_file))
    assert config.web.root_path == "/from-file"

    monkeypatch.setenv("SES_BOUNCE_WEB_ROOT_PATH", "/from-env")
    overridden = load_config(str(config_file))
    assert overridden.web.root_path == "/from-env"


def test_load_config_logging_precedence_env_overrides(tmp_path, monkeypatch):
    config_file = tmp_path / "ses-bounce.toml"
    config_file.write_text(
        """
[logging]
level = "warning"
format = "text"
access_log = false
uvicorn_log_level = "warning"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("SES_BOUNCE_LOG_LEVEL", "debug")
    monkeypatch.setenv("SES_BOUNCE_LOG_FORMAT", "json")
    monkeypatch.setenv("SES_BOUNCE_ACCESS_LOG", "1")
    monkeypatch.setenv("SES_BOUNCE_UVICORN_LOG_LEVEL", "error")

    config = load_config(
        str(config_file),
        {
            "log_level": "critical",
            "log_format": "text",
        },
    )

    assert config.logging.level == "critical"
    assert config.logging.format == "text"
    assert config.logging.access_log is True
    assert config.logging.uvicorn_log_level == "error"
