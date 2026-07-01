# magi-playground-wiki

MeTTa Playground sidecar for the Hyperon Wiki right column. Accepts MeTTa source from the browser, evaluates inside a Docker-per-request sandbox, returns output + stdout/stderr.

**Status**: scaffold (R2 — MeTTa sidecar MVP, per [docs/RIGHT-COLUMN-IMPLEMENTATION-PLAN.md](../hyperon-wiki/docs/RIGHT-COLUMN-IMPLEMENTATION-PLAN.md) in the `hyperon-wiki` sibling repo).

## Architecture

```
Browser → POST /api/playground/run {code} → FastAPI → docker run hyperon-runtime:0.2.10 → JSON
```

The FastAPI process is long-lived. Each request shells out to a fresh Docker container running the `hyperon-runtime:0.2.10` image (Python 3.11 + `hyperon==0.2.10`). The container is `--rm`, `--network=none`, `--read-only`, memory-capped, and timeout-killed.

**V1 (current scope)**: ephemeral containers per request. No state retention between `Run` clicks.

**V2 (deferred)**: AtomSpace session affinity — signed-in users get a long-lived named container keyed by their Decko session/auth id; state retained across Run clicks within a 30 min idle window. Anonymous users stay ephemeral.

## Invariants (subset of plan I-1..I-10)

- **I-2**: No untrusted MeTTa executes in the FastAPI worker process. Eval happens in a separate sandboxed container.
- **I-5**: Hard 5s wall clock + 256MB memory cap per eval. Enforced by Docker `--memory` + `--stop-timeout`.
- **I-9**: Reversible. If this sidecar is stopped, the wiki's `setupCodePlayground` JS falls back to canned `[Result of …]` outputs.

## Container lifecycle & cleanup (2026-06-30 OOM hardening)

The 2026-06-30 prod outage was a memory-exhaustion/OOM cascade: a burst of playground
requests left orphaned 256m containers that exhausted the ~3.7GiB, no-swap host. Root
cause was relying on `docker run --rm` for timeout cleanup — `--rm` is performed by the
**client** process, so SIGKILL-ing the `docker run` client (or crashing the worker)
leaves the container running. The runner now defends in three layers:

1. **Named + labelled containers.** Every container gets a unique `--name magi-pg-<uuid>`
   plus ownership labels (`magi-playground.managed=true`, `magi-playground.created=<epoch>`).
2. **Explicit force-removal on timeout/cancel/error.** Those paths run
   `docker rm -f <name>` on the actual container instead of trusting `--rm`.
3. **Background stale sweep.** A loop (startup + every `MAGI_CLEANUP_INTERVAL_S`)
   force-removes service-**labelled** containers older than `MAGI_STALE_CONTAINER_TTL_S`,
   reclaiming leaks from a crashed worker. The sweep filters on the ownership label, so it
   can never touch a container this service didn't create, and the TTL exceeds the max eval
   timeout so in-flight evals are never reaped.

An **`asyncio.Semaphore`** caps simultaneous evals at `MAGI_MAX_CONCURRENT_EVALS`
(default 4 ≈ 1GiB peak); excess requests queue rather than over-committing host RAM.
All settings live in `app/config.py` / `.env.example`.

## Hyperon 0.2.x runtime quirks

Users hitting these behaviors are encountering language semantics, not bugs:

- **Q-1 — `bind!` + `new-space`**: rebinding a space name does NOT clear atoms from the previously-bound space. Use explicit `(match &name (pattern) (remove-atom &name (pattern)))` for cleanup. (`[QUIRK-CONFIRMED-AT-HEAD]` at v0.2.10.)
- **Q-2 — `cond`**: NOT a builtin at v0.2.10. Use `if`, `case`, `match`, or define your own. (`[QUIRK-FIXED-VIA-ABSENCE]`.)
- **Q-3 — catchall multi-reduction**: catchall equations (`$_`, `$other`) alongside specific equations produce multiple reductions. This is documented nondeterministic semantics, not a defect. Use mutually exclusive patterns for deterministic dispatch.

## Local dev

Prereqs: Python 3.11+, Docker Desktop (with daemon running), `uv` or `pip`.

```bash
# Build the sandbox image (one-time, ~2 min)
docker build -f Dockerfile.runtime -t hyperon-runtime:0.2.10 ./runtime

# Install deps
pip install -e .

# Run the FastAPI server
uvicorn app.main:app --reload --port 8765

# Sanity test
curl -s http://localhost:8765/api/playground/health | jq
curl -s -X POST http://localhost:8765/api/playground/run \
  -H 'content-type: application/json' \
  -d '{"code": "!(+ 1 2)"}' | jq
```

Expected output:

```json
{
  "status": "ok",
  "output": "[3]",
  "stdout": "",
  "stderr": "",
  "elapsed_ms": 420
}
```

## Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/playground/run` | Evaluate MeTTa source |
| `GET`  | `/api/playground/health` | Health check |

### POST /api/playground/run

**Request**:
```json
{
  "code": "string (MeTTa source)",
  "timeout_ms": 5000
}
```

**Response**:
```json
{
  "status": "ok | error | timeout",
  "output": "string (! query results, one per line)",
  "stdout": "string (any print output from the program)",
  "stderr": "string (errors, formatted)",
  "elapsed_ms": 420
}
```

### GET /api/playground/health

**Response**:
```json
{
  "ok": true,
  "hyperon_version": "0.2.10",
  "uptime_s": 1234
}
```

## Deploy

Runs as a systemd service on the EC2 host alongside Decko/Nginx. Each request shells out to `docker run` against the pre-built `hyperon-runtime:0.2.10` image. All artifacts live under [`deploy/`](./deploy/):

| File | Purpose |
|------|---------|
| [`magi-playground-wiki.service`](./deploy/magi-playground-wiki.service) | systemd unit — uvicorn under user `magi-playground` (in `docker` group), 1 worker, hardened |
| [`nginx-rate-limits.conf`](./deploy/nginx-rate-limits.conf) | `http {}`-level rate-limit zones (tiered, 60/min signed-in, 30/min anon) |
| [`nginx-playground.conf`](./deploy/nginx-playground.conf) | `server {}`-level location block (15s read timeout, 128k body cap) |
| [`deploy.sh`](./deploy/deploy.sh) | idempotent install/update: git pull → docker build (when runtime/ changed) → venv pip install → restart → health-check |

### First-time install (on the EC2 host, as root)

```bash
# 1. Clone the repo.
git clone https://github.com/Magi-AGI/magi-playground-wiki.git /opt/magi-playground-wiki

# 2. (Optional) override defaults via .env. All settings have sensible defaults.
cp /opt/magi-playground-wiki/.env.example /opt/magi-playground-wiki/.env

# 3. Wire Nginx — load rate-limit zones in http {}, location block in server {}.
sudo cp /opt/magi-playground-wiki/deploy/nginx-rate-limits.conf \
        /etc/nginx/conf.d/magi-playground-rate-limits.conf
sudo cp /opt/magi-playground-wiki/deploy/nginx-playground.conf \
        /etc/nginx/snippets/magi-playground.conf
# Then add `include /etc/nginx/snippets/magi-playground.conf;` inside the
# wiki.hyperon.dev server block, and reload:
sudo nginx -t && sudo systemctl reload nginx

# 4. Run the deploy script (creates the service user, adds it to docker group,
#    builds the hyperon-runtime image, installs the systemd unit, starts it).
sudo bash /opt/magi-playground-wiki/deploy/deploy.sh
```

### Updates

```bash
sudo bash /opt/magi-playground-wiki/deploy/deploy.sh
```

The script git-pulls `main`, rebuilds the `hyperon-runtime` image only when `runtime/Dockerfile` or `runtime/entrypoint.py` changed, refreshes the Python venv, restarts, and curls `/api/playground/health`.

### Observability

```bash
# Live structured logs.
journalctl -u magi-playground-wiki -f

# Service status.
systemctl status magi-playground-wiki

# Smoke test from the host.
curl -s http://127.0.0.1:8765/api/playground/health | jq

# Prometheus scrape.
curl -s http://127.0.0.1:8765/metrics | head -20

# End-to-end through Nginx.
curl -s -X POST https://wiki.hyperon.dev/api/playground/run \
    -H 'content-type: application/json' \
    -d '{"code":"!(+ 1 2)"}' | jq
```

### Rate limit tiers (plan A-9 / B-7)

The two zones in `nginx-rate-limits.conf` use `map` directives to short-circuit whichever zone shouldn't apply: requests with `_hyperon_session` cookie hit only the user zone (60/min), requests without hit only the IP zone (30/min). nginx skips `limit_req` when the key evaluates to an empty string.

## Layout

```
magi-playground-wiki/
├── README.md
├── pyproject.toml
├── .gitignore
├── app/                    # FastAPI server (long-lived)
│   ├── __init__.py
│   ├── main.py             # FastAPI app + route handlers
│   ├── runner.py           # Docker-exec wrapper
│   ├── models.py           # Pydantic request/response
│   └── config.py
├── runtime/                # contents of the hyperon-runtime:0.2.10 image
│   ├── Dockerfile          # FROM python:3.11-slim + pip install hyperon
│   └── entrypoint.py       # in-container MeTTa runner
├── deploy/
│   ├── magi-playground-wiki.service   # systemd unit
│   ├── nginx-rate-limits.conf         # http{} rate-limit zones
│   ├── nginx-playground.conf          # server{} location block
│   └── deploy.sh                      # install/update script
└── tests/
    └── test_run.py
```

## License

MIT (sibling to the Magi-AGI ecosystem).
