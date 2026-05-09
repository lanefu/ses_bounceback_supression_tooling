#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from bounceback_store import connect_db, count_table_rows, database_is_empty, utc_now
from ses_config import load_config


SEED_SCHEMA_VERSION = 1
SEED_TABLES = [
    "scan_state",
    "bounce_events",
    "bounce_recipients",
    "event_identifiers",
    "aws_suppression_submissions",
]


def seed_manifest(db_path: str) -> dict[str, Any]:
    conn = connect_db(db_path)
    try:
        return {
            "schema_version": SEED_SCHEMA_VERSION,
            "exported_at": utc_now(),
            "source_db_path": os.path.abspath(db_path),
            "tables": {table: count_table_rows(conn, table) for table in SEED_TABLES},
        }
    finally:
        conn.close()


def export_seed(db_path: str, output: str) -> dict[str, Any]:
    manifest = seed_manifest(db_path)
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db = Path(tmpdir) / "bouncebacks.sqlite3"
        source = sqlite3.connect(db_path)
        dest = sqlite3.connect(tmp_db)
        try:
            source.backup(dest)
        finally:
            dest.close()
            source.close()
        manifest_path = Path(tmpdir) / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(tmp_db, "bouncebacks.sqlite3")
            archive.write(manifest_path, "manifest.json")
    return manifest


def read_seed_manifest(seed_path: str) -> dict[str, Any]:
    with zipfile.ZipFile(seed_path) as archive:
        with archive.open("manifest.json") as fh:
            data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Seed manifest is not a JSON object")
    return data


def validate_seed(seed_path: str) -> dict[str, Any]:
    manifest = read_seed_manifest(seed_path)
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(seed_path) as archive:
            archive.extract("bouncebacks.sqlite3", tmpdir)
        db_path = str(Path(tmpdir) / "bouncebacks.sqlite3")
        conn = connect_db(db_path)
        try:
            counts = {table: count_table_rows(conn, table) for table in SEED_TABLES}
        finally:
            conn.close()
    expected = manifest.get("tables", {})
    if counts != expected:
        raise ValueError(f"Seed table counts do not match manifest: expected={expected} actual={counts}")
    return manifest


def import_seed(seed_path: str, db_path: str, *, force: bool = False) -> dict[str, Any]:
    manifest = validate_seed(seed_path)
    target_path = Path(db_path)
    if target_path.exists():
        conn = connect_db(str(target_path))
        try:
            if not force and not database_is_empty(conn):
                raise ValueError(f"Target database is not empty: {db_path}")
        finally:
            conn.close()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(seed_path) as archive:
            archive.extract("bouncebacks.sqlite3", tmpdir)
        shutil.copy2(Path(tmpdir) / "bouncebacks.sqlite3", target_path)
    return manifest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export, validate, and import SES bounce SQLite seed bundles.")
    parser.add_argument("--config", default=None, help="Optional TOML config path.")
    parser.add_argument("--db", default=None, help="SQLite database path.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export-seed")
    export_parser.add_argument("--output", required=True)

    validate_parser = subparsers.add_parser("validate-seed")
    validate_parser.add_argument("--input", required=True)

    import_parser = subparsers.add_parser("import-seed")
    import_parser.add_argument("--input", required=True)
    import_parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config, {"db_path": args.db})
    if args.command == "export-seed":
        manifest = export_seed(config.database.path, args.output)
    elif args.command == "validate-seed":
        manifest = validate_seed(args.input)
    elif args.command == "import-seed":
        manifest = import_seed(args.input, config.database.path, force=args.force)
    else:
        raise SystemExit(f"Unknown command: {args.command}")
    print(json.dumps(manifest, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
