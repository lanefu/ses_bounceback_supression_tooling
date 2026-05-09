# SES Bounceback Suppression Tooling

This repo ingests SES bouncebacks into SQLite, then uses that database to seed and maintain the SES account-level suppression list. It supports the original Gmail/IMAP workflow and a FastAPI webhook for Amazon SNS HTTP/S bounce notifications.

## What’s Here

- `fetch_bouncebacks.py`
  - scans the Gmail label/folder for SES bounceback notifications
  - extracts SES JSON
  - stores normalized bounce data in SQLite
  - generates reports and CSV/JSON exports
- `aws_suppression_sync.py`
  - reads eligible rows from SQLite
  - pushes them into the SES account-level suppression list
  - records successful and failed submissions locally
- `bounceback_store.py`
  - shared SQLite schema and query helpers
- `web_service.py`
  - FastAPI SNS webhook service
  - stores bounce notifications in SQLite
  - writes `Permanent` bounces to SES account-level suppression inline
- `seed_store.py`
  - exports, validates, and imports full SQLite seed bundles
- `ses_config.py`
  - shared TOML/env/CLI configuration loader
- `requirements.txt`
  - Python dependencies for the tooling

## Setup

Create and populate the virtualenv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Run local commands from the activated `.venv`.

## Configuration

Configuration can come from built-in defaults, a TOML file, environment variables, and CLI args. Precedence is:

```text
CLI args > environment variables > config file > built-in defaults
```

Start with:

```bash
cp config.example.toml config.local.toml
```

Use `--config config.local.toml` with the CLI tools or set:

```bash
export SES_BOUNCE_CONFIG=config.local.toml
```

Do not put committed secrets in config files. Prefer environment variables for mailbox credentials and the normal AWS credential chain for AWS access.

## Environment

The ingest script reads IMAP settings from environment variables or flags:

- `SES_BOUNCE_CONFIG`
- `SES_BOUNCE_IMAP_USER`
- `SES_BOUNCE_IMAP_PASS`
- `SES_BOUNCE_IMAP_HOST` defaults to `imap.gmail.com`
- `SES_BOUNCE_LABEL` defaults to the config file value or `ses_bounce_notifications`
- `SES_BOUNCE_DB` defaults to `bouncebacks.sqlite3`

The AWS sync script uses the active AWS credential/profile chain by default. You can also pass:

- `--profile`
- `--region`

The webhook service also reads:

- `SES_BOUNCE_WEB_HOST` defaults to `127.0.0.1`
- `SES_BOUNCE_WEB_PORT` defaults to `8000`
- `SES_BOUNCE_WEB_ROOT_PATH` defaults to empty
- `SES_BOUNCE_PROXY_HEADERS` defaults to `false`
- `SES_BOUNCE_FORWARDED_ALLOW_IPS` defaults to `127.0.0.1`
- `SES_BOUNCE_VERIFY_SNS` defaults to `true`
- `SES_BOUNCE_UNSAFE_SKIP_SNS_VERIFY` defaults to `false`
- `SES_BOUNCE_LOG_LEVEL` defaults to `INFO`
- `SES_BOUNCE_LOG_FORMAT` defaults to `text`
- `SES_BOUNCE_ACCESS_LOG` defaults to `true`
- `SES_BOUNCE_UVICORN_LOG_LEVEL` defaults to `info`
- standard OpenTelemetry variables such as `OTEL_SERVICE_NAME`, `OTEL_EXPORTER_OTLP_ENDPOINT`, and `OTEL_RESOURCE_ATTRIBUTES`

## Common Commands

Ingest bouncebacks:

```bash
python fetch_bouncebacks.py sync
```

Report on the database:

```bash
python fetch_bouncebacks.py report
```

Export suppression candidates:

```bash
python fetch_bouncebacks.py export-suppressions --output /tmp/suppressions.csv --format csv
```

Dry-run the AWS sync:

```bash
python aws_suppression_sync.py sync --dry-run --limit 10
```

Push eligible suppression candidates:

```bash
python aws_suppression_sync.py sync --limit 100
```

Check local submission status:

```bash
python aws_suppression_sync.py status --recent 10
```

Run the webhook service locally:

```bash
python web_service.py --config config.local.toml
```

With uvicorn directly:

```bash
uvicorn web_service:app --host 127.0.0.1 --port 8000
```

Health checks:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
```

SNS should deliver HTTP/S notifications to:

```text
POST /sns/bounce
```

For local-only unsigned testing, set `SES_BOUNCE_UNSAFE_SKIP_SNS_VERIFY=true`. Do not use that setting for an internet-facing endpoint.

## Database Seeding

The current SQLite DB is production state. Use a seed bundle to refresh a Kubernetes PVC or another service database without losing dedupe and AWS submission history.

Export from the trusted local DB:

```bash
python seed_store.py --db bouncebacks.sqlite3 export-seed --output /tmp/ses-bounce-seed.zip
```

Validate the bundle:

```bash
python seed_store.py validate-seed --input /tmp/ses-bounce-seed.zip
```

Import into an empty target DB:

```bash
python seed_store.py --db /data/db/bouncebacks.sqlite3 import-seed --input /tmp/ses-bounce-seed.zip
```

Use `--force` only when intentionally replacing an existing target DB.

Suggested Kubernetes rollout:

1. Export and validate a seed from the current trusted DB.
2. Copy or mount the seed artifact into the environment that can write the SQLite volume.
3. Import the seed into the service DB path before enabling SNS traffic.
4. Check `/readyz`, `python fetch_bouncebacks.py report`, and `python aws_suppression_sync.py status`.
5. Wire SNS to the ingress path.

## Observability

The service uses OpenTelemetry when the dependencies are installed. Configure OTLP export with standard environment variables, for example:

```bash
export OTEL_SERVICE_NAME=ses-bounce-webhook
export OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
export OTEL_RESOURCE_ATTRIBUTES=deployment.environment=dev
```

The app records golden-signal HTTP telemetry through FastAPI instrumentation plus application metrics for SNS message types, signature failures, inserted/duplicate events, bounce type/subtype counts, and AWS suppression outcomes.

## Container Image

Build the image:

```bash
docker build -t ses-bounce-webhook:local .
```

Published images live at:

```text
ghcr.io/lanefu/ses-bounce-webhook
```

GitHub Actions publishing behavior:

- pull requests build the image for validation only
- pushes to `main` publish rolling `edge` and `sha-<commit>` tags
- pushes of semver tags like `v0.1.0` publish version tags plus `latest`

Run it with a mounted config and data directory:

```bash
mkdir -p /tmp/ses-bounce-config /tmp/ses-bounce-data
cp config.example.toml /tmp/ses-bounce-config/ses-bounce.toml
# Edit /tmp/ses-bounce-config/ses-bounce.toml and set web.host = "0.0.0.0".
docker run --rm -p 8000:8000 \
  -v /tmp/ses-bounce-config:/data/config:ro \
  -v /tmp/ses-bounce-data:/data/db \
  ses-bounce-webhook:local
```

Pull and run the published image:

```bash
docker pull ghcr.io/lanefu/ses-bounce-webhook:edge
docker run --rm -p 8000:8000 \
  -v /tmp/ses-bounce-config:/data/config:ro \
  -v /tmp/ses-bounce-data:/data/db \
  ghcr.io/lanefu/ses-bounce-webhook:edge
```

For container/Kubernetes use, set the TOML file to:

```toml
[database]
path = "/data/db/bouncebacks.sqlite3"

[web]
host = "0.0.0.0"
port = 8000
root_path = "/ses-bounce"
proxy_headers = true
forwarded_allow_ips = "*"

[logging]
level = "INFO"
format = "json"
access_log = true
```

`forwarded_allow_ips = "*"` is intended for the common cluster shape where only the trusted ingress/gateway can reach the pod. Tighten it if pods are directly reachable from less-trusted networks.

## Kubernetes

Static manifests live in `deploy/k8s/`. Edit the image name, host, AWS region, and `web.root_path` in the manifests before applying them or pointing Flux at the directory.

The default manifests now reference:

```text
ghcr.io/lanefu/ses-bounce-webhook:latest
```

```bash
kubectl apply -f deploy/k8s/
```

For Flux, point a Flux `Kustomization` at the `deploy/k8s` path. The included `kustomization.yaml` intentionally excludes `deploy/k8s/examples/`, so the seed import job and example secret are never reconciled unless you opt into them separately.

The default deployment uses:

- one replica
- `Recreate` strategy for the PVC-backed SQLite database
- ConfigMap mounted at `/data/config`
- PVC mounted at `/data/db`
- liveness probe `/healthz`
- readiness and startup probes `/readyz`

## Release Flow

For the GitHub Actions-based container publishing flow:

```bash
git push origin main
```

That publishes refreshed `edge` and `sha-*` images from the current `main` commit.

For a versioned release image:

```bash
git tag v0.1.0
git push origin v0.1.0
```

That publishes semver tags for the image, including `0.1.0`, `0.1`, `0`, and `latest`.

The public SNS endpoint for the included ingress example is:

```text
https://example.org/ses-bounce/sns/bounce
```

Kubernetes probes use internal paths without the subpath because kubelet talks directly to the pod.

### Seed Import Job

Export a seed from the current trusted DB:

```bash
python seed_store.py --db bouncebacks.sqlite3 export-seed --output /tmp/ses-bounce-seed.zip
```

For small seed bundles, create a Secret and run the optional import Job:

```bash
kubectl -n ses-bounce create secret generic ses-bounce-seed \
  --from-file=ses-bounce-seed.zip=/tmp/ses-bounce-seed.zip
kubectl apply -f deploy/k8s/examples/job-import-seed.yaml
```

Large seed bundles may exceed Kubernetes Secret size limits. In that case, use an object-store/download init pattern or copy the seed into a temporary admin pod that mounts the same PVC, then run `seed_store.py import-seed`.

## Operational Rules

- `Permanent` bounces are the default suppression seed.
- `Transient` bounces are not auto-synced by default.
- The AWS writer uses `Reason=BOUNCE`.
- Live sync is sequential and throttled with backoff and a small inter-call delay.
- Successful AWS submissions are recorded locally so reruns skip them.

## Suggested Workflow

1. Keep ingest running until the mailbox is fully processed.
2. Check `python aws_suppression_sync.py status`.
3. Dry-run the suppression sync for a small sample.
4. Run the live sync in modest chunks.
5. Re-run `status` to watch the pending count drop.

## Files To Know

- [`fetch_bouncebacks.py`](./fetch_bouncebacks.py)
- [`aws_suppression_sync.py`](./aws_suppression_sync.py)
- [`bounceback_store.py`](./bounceback_store.py)
