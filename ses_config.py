#!/usr/bin/env python3

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from bounceback_store import DEFAULT_DB_PATH, DEFAULT_LABEL


DEFAULT_IMAP_HOST = "imap.gmail.com"
ENV_CONFIG = "SES_BOUNCE_CONFIG"


@dataclass(frozen=True)
class DatabaseConfig:
    path: str = DEFAULT_DB_PATH


@dataclass(frozen=True)
class ImapConfig:
    user: Optional[str] = None
    password: Optional[str] = None
    host: str = DEFAULT_IMAP_HOST
    label: str = DEFAULT_LABEL


@dataclass(frozen=True)
class AwsConfig:
    profile: Optional[str] = None
    region: Optional[str] = None
    retry_count: int = 6
    delay_seconds: float = 0.25
    batch_size: int = 10


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    root_path: str = ""
    proxy_headers: bool = False
    forwarded_allow_ips: str = "127.0.0.1"
    verify_sns: bool = True
    unsafe_skip_sns_verify: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    format: str = "text"
    access_log: bool = True
    uvicorn_log_level: str = "info"


@dataclass(frozen=True)
class OtelConfig:
    service_name: str = "ses-bounce-webhook"
    exporter_otlp_endpoint: Optional[str] = None
    resource_attributes: Optional[str] = None


@dataclass(frozen=True)
class ServiceConfig:
    database: DatabaseConfig = DatabaseConfig()
    imap: ImapConfig = ImapConfig()
    aws: AwsConfig = AwsConfig()
    web: WebConfig = WebConfig()
    logging: LoggingConfig = LoggingConfig()
    otel: OtelConfig = OtelConfig()


def _parse_bool(value: Any, *, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{key} must be a boolean value")


def _load_toml(path: Optional[str]) -> dict[str, Any]:
    if not path:
        return {}
    config_path = Path(path)
    if not config_path.exists():
        raise ValueError(f"Config file does not exist: {path}")
    with config_path.open("rb") as fh:
        return tomllib.load(fh)


def _overlay_file(config: ServiceConfig, data: dict[str, Any]) -> ServiceConfig:
    database = data.get("database", {})
    imap = data.get("imap", {})
    aws = data.get("aws", {})
    web = data.get("web", {})
    logging_config = data.get("logging", {})
    otel = data.get("otel", {})

    if not all(isinstance(section, dict) for section in (database, imap, aws, web, logging_config, otel)):
        raise ValueError("Config sections must be TOML tables")

    result = config
    if database:
        result = replace(result, database=replace(result.database, path=str(database.get("path", result.database.path))))
    if imap:
        result = replace(
            result,
            imap=replace(
                result.imap,
                user=imap.get("user", result.imap.user),
                password=imap.get("password", result.imap.password),
                host=str(imap.get("host", result.imap.host)),
                label=str(imap.get("label", result.imap.label)),
            ),
        )
    if aws:
        result = replace(
            result,
            aws=replace(
                result.aws,
                profile=aws.get("profile", result.aws.profile),
                region=aws.get("region", result.aws.region),
                retry_count=int(aws.get("retry_count", result.aws.retry_count)),
                delay_seconds=float(aws.get("delay_seconds", result.aws.delay_seconds)),
                batch_size=int(aws.get("batch_size", result.aws.batch_size)),
            ),
        )
    if web:
        result = replace(
            result,
            web=replace(
                result.web,
                host=str(web.get("host", result.web.host)),
                port=int(web.get("port", result.web.port)),
                root_path=str(web.get("root_path", result.web.root_path)),
                proxy_headers=_parse_bool(web.get("proxy_headers", result.web.proxy_headers), key="web.proxy_headers"),
                forwarded_allow_ips=str(web.get("forwarded_allow_ips", result.web.forwarded_allow_ips)),
                verify_sns=_parse_bool(web.get("verify_sns", result.web.verify_sns), key="web.verify_sns"),
                unsafe_skip_sns_verify=_parse_bool(
                    web.get("unsafe_skip_sns_verify", result.web.unsafe_skip_sns_verify),
                    key="web.unsafe_skip_sns_verify",
                ),
            ),
        )
    if logging_config:
        result = replace(
            result,
            logging=replace(
                result.logging,
                level=str(logging_config.get("level", result.logging.level)),
                format=str(logging_config.get("format", result.logging.format)),
                access_log=_parse_bool(logging_config.get("access_log", result.logging.access_log), key="logging.access_log"),
                uvicorn_log_level=str(logging_config.get("uvicorn_log_level", result.logging.uvicorn_log_level)),
            ),
        )
    if otel:
        result = replace(
            result,
            otel=replace(
                result.otel,
                service_name=str(otel.get("service_name", result.otel.service_name)),
                exporter_otlp_endpoint=otel.get("exporter_otlp_endpoint", result.otel.exporter_otlp_endpoint),
                resource_attributes=otel.get("resource_attributes", result.otel.resource_attributes),
            ),
        )
    return result


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in {"", None} else None


def _overlay_env(config: ServiceConfig) -> ServiceConfig:
    database = replace(config.database, path=_env("SES_BOUNCE_DB") or config.database.path)
    imap = replace(
        config.imap,
        user=_env("SES_BOUNCE_IMAP_USER") or config.imap.user,
        password=_env("SES_BOUNCE_IMAP_PASS") or config.imap.password,
        host=_env("SES_BOUNCE_IMAP_HOST") or config.imap.host,
        label=_env("SES_BOUNCE_LABEL") or config.imap.label,
    )
    aws = replace(
        config.aws,
        profile=_env("AWS_PROFILE") or config.aws.profile,
        region=_env("AWS_REGION") or config.aws.region,
        retry_count=int(_env("SES_BOUNCE_AWS_RETRY_COUNT") or config.aws.retry_count),
        delay_seconds=float(_env("SES_BOUNCE_AWS_DELAY_SECONDS") or config.aws.delay_seconds),
        batch_size=int(_env("SES_BOUNCE_AWS_BATCH_SIZE") or config.aws.batch_size),
    )
    web = replace(
        config.web,
        host=_env("SES_BOUNCE_WEB_HOST") or config.web.host,
        port=int(_env("SES_BOUNCE_WEB_PORT") or config.web.port),
        root_path=_env("SES_BOUNCE_WEB_ROOT_PATH") or config.web.root_path,
        proxy_headers=_parse_bool(_env("SES_BOUNCE_PROXY_HEADERS") or config.web.proxy_headers, key="SES_BOUNCE_PROXY_HEADERS"),
        forwarded_allow_ips=_env("SES_BOUNCE_FORWARDED_ALLOW_IPS") or config.web.forwarded_allow_ips,
        verify_sns=_parse_bool(_env("SES_BOUNCE_VERIFY_SNS") or config.web.verify_sns, key="SES_BOUNCE_VERIFY_SNS"),
        unsafe_skip_sns_verify=_parse_bool(
            _env("SES_BOUNCE_UNSAFE_SKIP_SNS_VERIFY") or config.web.unsafe_skip_sns_verify,
            key="SES_BOUNCE_UNSAFE_SKIP_SNS_VERIFY",
        ),
    )
    logging_config = replace(
        config.logging,
        level=_env("SES_BOUNCE_LOG_LEVEL") or config.logging.level,
        format=_env("SES_BOUNCE_LOG_FORMAT") or config.logging.format,
        access_log=_parse_bool(_env("SES_BOUNCE_ACCESS_LOG") or config.logging.access_log, key="SES_BOUNCE_ACCESS_LOG"),
        uvicorn_log_level=_env("SES_BOUNCE_UVICORN_LOG_LEVEL") or config.logging.uvicorn_log_level,
    )
    otel = replace(
        config.otel,
        service_name=_env("OTEL_SERVICE_NAME") or config.otel.service_name,
        exporter_otlp_endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT") or config.otel.exporter_otlp_endpoint,
        resource_attributes=_env("OTEL_RESOURCE_ATTRIBUTES") or config.otel.resource_attributes,
    )
    return ServiceConfig(database=database, imap=imap, aws=aws, web=web, logging=logging_config, otel=otel)


def _validate(config: ServiceConfig) -> ServiceConfig:
    if config.aws.retry_count < 0:
        raise ValueError("aws.retry_count must be >= 0")
    if config.aws.delay_seconds < 0:
        raise ValueError("aws.delay_seconds must be >= 0")
    if config.aws.batch_size < 1:
        raise ValueError("aws.batch_size must be >= 1")
    if config.web.port < 1 or config.web.port > 65535:
        raise ValueError("web.port must be between 1 and 65535")
    if config.logging.format.lower() not in {"text", "json"}:
        raise ValueError("logging.format must be 'text' or 'json'")
    valid_levels = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
    if config.logging.level.upper() not in valid_levels:
        raise ValueError(f"logging.level must be one of {', '.join(sorted(valid_levels))}")
    if config.logging.uvicorn_log_level.lower() not in {"critical", "error", "warning", "info", "debug", "trace"}:
        raise ValueError("logging.uvicorn_log_level must be critical, error, warning, info, debug, or trace")
    return config


def load_config(config_path: Optional[str] = None, overrides: Optional[dict[str, Any]] = None) -> ServiceConfig:
    path = config_path or _env(ENV_CONFIG)
    config = _overlay_file(ServiceConfig(), _load_toml(path))
    config = _overlay_env(config)
    overrides = overrides or {}

    if "db_path" in overrides and overrides["db_path"] is not None:
        config = replace(config, database=replace(config.database, path=str(overrides["db_path"])))
    if "label" in overrides and overrides["label"] is not None:
        config = replace(config, imap=replace(config.imap, label=str(overrides["label"])))
    if "imap_user" in overrides and overrides["imap_user"] is not None:
        config = replace(config, imap=replace(config.imap, user=overrides["imap_user"]))
    if "imap_pass" in overrides and overrides["imap_pass"] is not None:
        config = replace(config, imap=replace(config.imap, password=overrides["imap_pass"]))
    if "imap_host" in overrides and overrides["imap_host"] is not None:
        config = replace(config, imap=replace(config.imap, host=str(overrides["imap_host"])))
    if "profile" in overrides and overrides["profile"] is not None:
        config = replace(config, aws=replace(config.aws, profile=overrides["profile"]))
    if "region" in overrides and overrides["region"] is not None:
        config = replace(config, aws=replace(config.aws, region=overrides["region"]))
    if "delay_seconds" in overrides and overrides["delay_seconds"] is not None:
        config = replace(config, aws=replace(config.aws, delay_seconds=float(overrides["delay_seconds"])))
    if "max_retries" in overrides and overrides["max_retries"] is not None:
        config = replace(config, aws=replace(config.aws, retry_count=int(overrides["max_retries"])))
    if "batch_size" in overrides and overrides["batch_size"] is not None:
        config = replace(config, aws=replace(config.aws, batch_size=int(overrides["batch_size"])))
    if "host" in overrides and overrides["host"] is not None:
        config = replace(config, web=replace(config.web, host=str(overrides["host"])))
    if "port" in overrides and overrides["port"] is not None:
        config = replace(config, web=replace(config.web, port=int(overrides["port"])))
    if "log_level" in overrides and overrides["log_level"] is not None:
        config = replace(config, logging=replace(config.logging, level=str(overrides["log_level"])))
    if "log_format" in overrides and overrides["log_format"] is not None:
        config = replace(config, logging=replace(config.logging, format=str(overrides["log_format"])))

    return _validate(config)


def require_imap_credentials(config: ServiceConfig) -> tuple[str, str]:
    if not config.imap.user or not config.imap.password:
        raise SystemExit("Missing IMAP credentials. Set SES_BOUNCE_IMAP_USER and SES_BOUNCE_IMAP_PASS or pass CLI flags.")
    return config.imap.user, config.imap.password
