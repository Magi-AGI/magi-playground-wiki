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

The FastAPI server runs on the EC2 host alongside Decko/Nginx. Nginx routes `/api/playground/*` to `127.0.0.1:8765`. The host must have a Docker daemon; the sidecar process needs membership in the `docker` group to run `docker run` without sudo.

See `deploy/` (TBD) for systemd unit, Nginx snippet, and Dockerfile for containerizing the FastAPI server itself (DinD/socket-mount).

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
└── tests/
    └── test_run.py
```

## License

MIT (sibling to the Magi-AGI ecosystem).
