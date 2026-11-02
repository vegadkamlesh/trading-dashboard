#!/usr/bin/env bash
set -Eeuo pipefail

bucket="${1:?Artifact bucket required}"
key="${2:?Artifact key required}"
expected="${3:?Artifact digest required}"
volume_id="${4:?Data volume ID required}"
if [[ ! "$bucket" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] ||
   [[ ! "$key" =~ ^[A-Za-z][A-Za-z0-9-]{0,31}/app-[a-f0-9]{20}$ ]] ||
   [[ ! "$expected" =~ ^[a-f0-9]{64}$ ]] ||
   [[ ! "$volume_id" =~ ^vol-[a-f0-9]+$ ]] ||
   [[ "${key##*/app-}" != "${expected:0:20}" ]]; then
    echo "Invalid mock release coordinates" >&2
    exit 1
fi

verify_mock_state() {
    cd /opt/trading/backend
    /opt/trading/venv/bin/python - <<'PY'
import http.cookiejar
import json
import urllib.request

from app.accounts import ACCOUNT_IDS, account_credentials
from app.config import get_settings

settings = get_settings()
if not settings.dhan_client_id or not settings.dhan_access_token.lower().startswith("mock"):
    raise RuntimeError("Mock data credentials are required")
if not settings.app_dry_run or settings.app_fast_orders:
    raise RuntimeError("Mock configuration must default to dry-run and require the PIN")
for account in ACCOUNT_IDS:
    client_id, token = account_credentials(account)
    if (client_id or token) and not (client_id and token.lower().startswith("mock")):
        raise RuntimeError(f"{account} must be unconfigured or use mock trading credentials")

base = "http://127.0.0.1:8000"
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)
with opener.open(base + "/api/health", timeout=10) as response:
    health = json.load(response)
if not health["ok"] or not health["dryRun"] or health["fastOrders"] or health["algo"]["enabled"]:
    raise RuntimeError("The mock backend is not dry-run, PIN-protected and disarmed")
request = urllib.request.Request(
    base + "/api/auth/login",
    data=b"{}",
    headers={"Content-Type": "application/json"},
    method="POST",
)
with opener.open(request, timeout=10) as response:
    if not json.load(response)["ok"]:
        raise RuntimeError("Mock dashboard login failed")
with opener.open(base + "/api/trading/accounts", timeout=10) as response:
    accounts = json.load(response)["accounts"]
if any(account["algoEnabled"] or account["positionState"] is not None for account in accounts):
    raise RuntimeError("A mock algo is armed or has a tracked position")
print("Mock credentials, dry-run, disarmed engines and flat tracked state verified")
PY
}

verify_mock_state
mountpoint -q /var/lib/trading
source="$(findmnt -rn -o SOURCE --target /var/lib/trading)"
serial="$(lsblk -dno SERIAL "$source" | tr -d '[:space:]')"
if [[ "$serial" != "${volume_id//-/}" ]]; then
    echo "The mounted data filesystem is not the expected volume" >&2
    exit 1
fi
work="/run/trading-mock-release-${expected:0:20}"
rollback="/opt/trading/mock-release-rollback-${expected:0:20}"
marker="/opt/trading/.mock-release-sha256"
if [[ -f "$marker" && "$(cat "$marker")" == "$expected" ]]; then
    echo "The requested mock release is already installed"
    exit 0
fi
if [[ -e "$work" || -e "$rollback" ]]; then
    echo "A release or rollback directory already exists; inspect it before retrying" >&2
    exit 1
fi
mkdir -m 0700 "$work" "$rollback"
aws s3 cp "s3://${bucket}/${key}" "$work/app.zip" --only-show-errors
printf '%s  %s\n' "$expected" "$work/app.zip" | sha256sum --check

python3 - "$work/app.zip" <<'PY'
import stat
import sys
from pathlib import PurePosixPath
from zipfile import ZipFile

required = {
    "backend/app/dhan_client.py",
    "backend/app/routers/trading_accounts.py",
    "backend/requirements.txt",
    "backend/run.py",
    "frontend/dist/index.html",
}
with ZipFile(sys.argv[1]) as archive:
    names = archive.namelist()
    if len(names) != len(set(names)) or not required.issubset(names):
        raise ValueError("Release has duplicate or missing files")
    for item in archive.infolist():
        path = PurePosixPath(item.filename)
        allowed = (
            item.filename.startswith("backend/app/")
            or item.filename in ("backend/requirements.txt", "backend/run.py")
            or item.filename.startswith("frontend/dist/")
            or item.filename in (
                "aws/nginx.conf", "aws/trading-dashboard.service",
                "aws/cloudwatch-agent.json", "aws/write_config.py",
            )
        )
        if (
            not allowed or path.is_absolute() or ".." in path.parts
            or "\\" in item.filename or stat.S_ISLNK(item.external_attr >> 16)
        ):
            raise ValueError(f"Unsafe release entry: {item.filename}")
print("Release contents verified")
PY
unzip -oq "$work/app.zip" -d "$work"

for file in requirements.txt run.py; do
    if ! cmp -s "$work/backend/$file" "/opt/trading/backend/$file"; then
        echo "Backend dependency or launcher changed; use a separately reviewed upgrade" >&2
        exit 1
    fi
done
for file in trading-dashboard.service cloudwatch-agent.json write_config.py; do
    if ! cmp -s "$work/aws/$file" "/opt/trading/aws/$file"; then
        echo "Infrastructure configuration changed; use a separately reviewed upgrade" >&2
        exit 1
    fi
done
if ! cmp -s /opt/trading/aws/nginx.conf /etc/nginx/nginx.conf; then
    echo "Installed Nginx configuration differs from the original release" >&2
    exit 1
fi
nginx -t -c "$work/aws/nginx.conf"
if [[ ! -d /opt/trading/backend/app || -L /opt/trading/backend/app ||
      ! -d /opt/trading/frontend/dist || -L /opt/trading/frontend/dist ]]; then
    echo "Existing application paths are not ordinary directories" >&2
    exit 1
fi

backend_saved=0
backend_installed=0
frontend_saved=0
frontend_installed=0
nginx_saved=0
nginx_installed=0
restore_on_error() {
    status=$?
    trap - ERR
    echo "Mock release failed; restoring the previous application" >&2
    if ! systemctl stop trading-dashboard nginx; then
        echo "Could not stop services during rollback" >&2
    fi
    if [[ "$backend_installed" == 1 ]]; then
        mv /opt/trading/backend/app "$work/failed-app" ||
            { echo "Could not preserve failed backend" >&2; exit "$status"; }
    fi
    if [[ "$backend_saved" == 1 ]]; then
        mv "$rollback/app" /opt/trading/backend/app ||
            { echo "Could not restore previous backend" >&2; exit "$status"; }
    fi
    if [[ "$frontend_installed" == 1 ]]; then
        mv /opt/trading/frontend/dist "$work/failed-dist" ||
            { echo "Could not preserve failed frontend" >&2; exit "$status"; }
    fi
    if [[ "$frontend_saved" == 1 ]]; then
        mv "$rollback/dist" /opt/trading/frontend/dist ||
            { echo "Could not restore previous frontend" >&2; exit "$status"; }
    fi
    if [[ "$nginx_installed" == 1 && "$nginx_saved" == 1 ]]; then
        install -m 0644 "$rollback/nginx.conf" /etc/nginx/nginx.conf ||
            { echo "Could not restore Nginx configuration" >&2; exit "$status"; }
        install -m 0644 "$rollback/nginx.conf" /opt/trading/aws/nginx.conf ||
            { echo "Could not restore the Nginx release reference" >&2; exit "$status"; }
    fi
    if ! systemctl start nginx trading-dashboard; then
        echo "Previous services did not restart; manual recovery is required" >&2
    fi
    exit "$status"
}
trap restore_on_error ERR
cp -p /etc/nginx/nginx.conf "$rollback/nginx.conf"
nginx_saved=1
systemctl stop trading-dashboard nginx
mv /opt/trading/backend/app "$rollback/app"
backend_saved=1
mv "$work/backend/app" /opt/trading/backend/app
backend_installed=1
mv /opt/trading/frontend/dist "$rollback/dist"
frontend_saved=1
mv "$work/frontend/dist" /opt/trading/frontend/dist
frontend_installed=1
nginx_installed=1
install -m 0644 "$work/aws/nginx.conf" /etc/nginx/nginx.conf
install -m 0644 "$work/aws/nginx.conf" /opt/trading/aws/nginx.conf
systemctl start nginx trading-dashboard
ready=0
for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 http://127.0.0.1:8080/api/health > /dev/null; then
        ready=1
        break
    fi
    sleep 2
done
if [[ "$ready" != 1 ]]; then
    echo "Mock dashboard did not become ready after the release" >&2
    false
fi
verify_mock_state
if ! curl -fsSI --max-time 5 http://127.0.0.1:8080/ | tr -d '\r' | grep -i '^Cache-Control: no-store$' > /dev/null; then
    echo "The frontend entry point is not protected against stale caching" >&2
    false
fi
printf '%s\n' "$expected" > "$marker.new"
mv "$marker.new" "$marker"
trap - ERR
echo "Mock release installed; the previous app and frontend are retained for rollback"
