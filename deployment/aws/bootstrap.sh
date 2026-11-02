#!/usr/bin/env bash
set -Eeuo pipefail
trap 'printf "Dashboard bootstrap failed at line %s\n" "$LINENO" >&2' ERR

: "${AWS_REGION:?}"
: "${ARTIFACT_BUCKET:?}"
: "${APP_ARTIFACT_KEY:?}"
: "${DATA_VOLUME_ID:?}"
: "${TELEGRAM_PARAMETER:?}"
: "${APP_LOG_GROUP:?}"
: "${SYSTEM_LOG_GROUP:?}"

export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""
umask 022
secret_file=""
trap 'if [[ -n "$secret_file" ]]; then rm -f -- "$secret_file"; fi' EXIT

if [[ -e /opt/trading/backend/.env || -L /opt/trading/backend/.env ]]; then
    echo "Existing backend configuration found; refusing automatic bootstrap" >&2
    exit 1
fi
if systemctl is-active --quiet trading-dashboard.service; then
    echo "Dashboard is already running; refusing to overwrite its files" >&2
    exit 1
fi

dnf install -y python3.11 python3.11-pip nginx amazon-cloudwatch-agent unzip xfsprogs
timedatectl set-timezone Asia/Kolkata
install -d -m 0755 /opt/trading
aws s3 cp "s3://${ARTIFACT_BUCKET}/${APP_ARTIFACT_KEY}" /var/tmp/trading-app.zip --only-show-errors
unzip -oq /var/tmp/trading-app.zip -d /opt/trading
rm -f -- /var/tmp/trading-app.zip

serial="${DATA_VOLUME_ID//-/}"
device=""
for _ in $(seq 1 180); do
    device="$(lsblk -dno PATH,SERIAL | awk -v serial="$serial" '$2 == serial { print $1; exit }')"
    if [[ -n "$device" ]]; then break; fi
    sleep 5
done
if [[ -z "$device" || ! -b "$device" ]]; then
    echo "Expected data volume did not attach" >&2
    exit 1
fi

filesystem="$(blkid -s TYPE -o value "$device" || true)"
if [[ -z "$filesystem" ]]; then
    if [[ -n "$(wipefs --noheadings "$device")" ]]; then
        echo "Data volume has an unrecognized signature; refusing to format" >&2
        exit 1
    fi
    mkfs.xfs "$device"
elif [[ "$filesystem" != "xfs" ]]; then
    echo "Data volume uses an unexpected filesystem; refusing to format" >&2
    exit 1
fi

uuid="$(blkid -s UUID -o value "$device")"
if [[ -z "$uuid" ]]; then
    echo "Data volume has no filesystem UUID" >&2
    exit 1
fi
install -d -m 0755 /var/lib/trading
if ! grep -Fq "UUID=$uuid /var/lib/trading " /etc/fstab; then
    printf 'UUID=%s /var/lib/trading xfs defaults,nofail 0 2\n' "$uuid" >> /etc/fstab
fi
if ! mountpoint -q /var/lib/trading; then
    mount /var/lib/trading
fi
mountpoint -q /var/lib/trading
if [[ "$(findmnt -rn -o UUID --target /var/lib/trading)" != "$uuid" ]]; then
    echo "An unexpected volume is mounted at the data path" >&2
    exit 1
fi

if ! id trading >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/trading --shell /sbin/nologin trading
fi
chown trading:trading /var/lib/trading
chmod 0700 /var/lib/trading
install -d -o trading -g trading -m 0700 /var/lib/trading/data /var/lib/trading/logs
ensure_link() {
    local target="$1"
    local link="$2"
    if [[ -L "$link" ]]; then
        if [[ "$(readlink "$link")" != "$target" ]]; then
            echo "Unexpected existing data link: $link" >&2
            exit 1
        fi
    elif [[ -e "$link" ]]; then
        echo "Existing data path is not a link: $link" >&2
        exit 1
    else
        ln -s "$target" "$link"
    fi
}
ensure_link /var/lib/trading/data /opt/trading/backend/data
ensure_link /var/lib/trading/audit.sqlite3 /opt/trading/backend/audit.sqlite3
ensure_link /var/lib/trading/audit-child.sqlite3 /opt/trading/backend/audit-child.sqlite3

python3.11 -m venv /opt/trading/venv
/opt/trading/venv/bin/python -m pip install --no-cache-dir --disable-pip-version-check \
    -r /opt/trading/backend/requirements.txt

umask 077
secret_file="$(mktemp /run/trading-telegram.XXXXXX)"
aws ssm get-parameter --name "$TELEGRAM_PARAMETER" --with-decryption \
    --query Parameter.Value --output text > "$secret_file"
python3.11 /opt/trading/aws/write_config.py "$secret_file" /opt/trading/backend/.env
rm -f -- "$secret_file"
umask 022
chown root:trading /opt/trading/backend/.env
chmod 0640 /opt/trading/backend/.env

install -m 0644 /opt/trading/aws/nginx.conf /etc/nginx/nginx.conf
nginx -t
install -m 0644 /opt/trading/aws/trading-dashboard.service /etc/systemd/system/trading-dashboard.service
sed -e "s|__APP_LOG_GROUP__|${APP_LOG_GROUP}|g" \
    -e "s|__SYSTEM_LOG_GROUP__|${SYSTEM_LOG_GROUP}|g" \
    /opt/trading/aws/cloudwatch-agent.json > /opt/aws/amazon-cloudwatch-agent/etc/trading.json
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -c file:/opt/aws/amazon-cloudwatch-agent/etc/trading.json -s
systemctl daemon-reload
systemctl enable amazon-cloudwatch-agent nginx trading-dashboard
systemctl restart nginx
systemctl start trading-dashboard

for _ in $(seq 1 60); do
    if curl -fsS --max-time 3 http://127.0.0.1:8080/api/health > /dev/null; then
        echo "Mock dashboard bootstrap complete"
        exit 0
    fi
    sleep 3
done
echo "Dashboard did not become ready after bootstrap" >&2
exit 1
