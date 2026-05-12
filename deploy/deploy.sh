#!/usr/bin/env bash
# Deploy / update the MeTTa Playground sidecar on the EC2 host.
#
# Idempotent: safe to re-run for updates. First run installs the systemd unit,
# creates the service user, builds the hyperon-runtime image, and starts the
# service. Subsequent runs git-pull, rebuild Python deps, rebuild the image if
# runtime/ changed, and restart.
#
# Prereqs (one-time, run manually as root before first deploy):
#   1. Docker installed and running.
#   2. Python 3.11+ installed (python3.11 binary on PATH).
#   3. git installed.
#   4. /opt/magi-playground-wiki cloned from GitHub.
#   5. Nginx snippets in /etc/nginx/snippets/ (see nginx-*.conf in this dir).
#
# Usage: sudo bash deploy/deploy.sh

set -euo pipefail

REPO_DIR=/opt/magi-playground-wiki
SERVICE_USER=magi-playground
SERVICE_NAME=magi-playground-wiki
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
RUNTIME_IMAGE_TAG="hyperon-runtime:0.2.10"

if [ "$(id -u)" != "0" ]; then
    echo "must run as root (use sudo)" >&2
    exit 1
fi

if [ ! -d "$REPO_DIR" ]; then
    echo "$REPO_DIR not found — clone the repo first" >&2
    exit 1
fi

# 1. Service user — create if missing; ensure in the docker group either way.
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
usermod -aG docker "$SERVICE_USER"

# 2. Ensure the repo (and everything inside it) is owned by the service user
# so subsequent git/pip/build steps can run as that user. Idempotent.
chown -R "$SERVICE_USER":"$SERVICE_USER" "$REPO_DIR"

# 3. Sync code from git.
cd "$REPO_DIR"
sudo -u "$SERVICE_USER" git fetch --all --prune
sudo -u "$SERVICE_USER" git checkout main
sudo -u "$SERVICE_USER" git pull --ff-only

# 3. Build / refresh the hyperon-runtime sandbox image.
# Skip rebuild if neither runtime/Dockerfile nor runtime/entrypoint.py changed
# since the last successful build (rough heuristic: compare mtimes against the
# image's created time).
IMAGE_CREATED=$(docker image inspect "$RUNTIME_IMAGE_TAG" \
    --format '{{.Created}}' 2>/dev/null || echo "")
NEEDS_BUILD=1
if [ -n "$IMAGE_CREATED" ]; then
    IMAGE_TS=$(date -d "$IMAGE_CREATED" +%s 2>/dev/null || echo 0)
    SRC_TS=$(stat -c '%Y' runtime/Dockerfile runtime/entrypoint.py 2>/dev/null \
        | sort -nr | head -1)
    if [ "$SRC_TS" -le "$IMAGE_TS" ]; then
        NEEDS_BUILD=0
    fi
fi
if [ "$NEEDS_BUILD" = "1" ]; then
    echo "building $RUNTIME_IMAGE_TAG..."
    docker build -t "$RUNTIME_IMAGE_TAG" runtime/
else
    echo "$RUNTIME_IMAGE_TAG up to date — skipping rebuild"
fi

# 4. Python venv + dependencies.
if [ ! -d "$REPO_DIR/.venv" ]; then
    sudo -u "$SERVICE_USER" python3.11 -m venv "$REPO_DIR/.venv"
fi
sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$REPO_DIR/.venv/bin/pip" install --upgrade -e .

# 5. Permissions on .env (optional file, contains tuning knobs only — no secrets).
if [ -f "$REPO_DIR/.env" ]; then
    chown "$SERVICE_USER":"$SERVICE_USER" "$REPO_DIR/.env"
    chmod 640 "$REPO_DIR/.env"
fi

# 6. Install / refresh the systemd unit.
if ! cmp -s "$REPO_DIR/deploy/${SERVICE_NAME}.service" "$UNIT_FILE"; then
    cp "$REPO_DIR/deploy/${SERVICE_NAME}.service" "$UNIT_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
fi

# 7. Restart and verify.
systemctl restart "$SERVICE_NAME"
sleep 2
systemctl is-active --quiet "$SERVICE_NAME" || {
    echo "service failed to start — check: journalctl -u ${SERVICE_NAME} -n 50" >&2
    exit 1
}

# 8. Health endpoint.
curl -fsS http://127.0.0.1:8765/api/playground/health
echo
echo "deploy ok"
