"""Copy the latest Dhan credentials from Parameter Store into the backend .env before start.

Run by systemd as root (ExecStartPre=-+...). It only replaces the Dhan credential keys, keeps
every other line, and never prints secret values. A failure leaves the existing .env untouched.
"""

import json
import os
import subprocess
import sys
import tempfile

ENV_PATH = "/opt/trading/backend/.env"
KEYS = (
    "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_MASTER_TRADING_TOKEN",
    "DHAN_CHILD_CLIENT_ID", "DHAN_CHILD_ACCESS_TOKEN", "APP_ORDER_PIN",
)
UNSAFE = set("\r\n\"'\\$`")


def merge(text: str, creds: dict) -> str:
    managed = {key: str(value) for key, value in creds.items() if key in KEYS and value}
    for key, value in managed.items():
        if UNSAFE & set(value):
            raise ValueError(f"{key} contains unsafe characters")
    lines = [line for line in text.splitlines() if line.split("=", 1)[0] not in managed]
    return "\n".join(lines + [f"{key}={value}" for key, value in managed.items()]) + "\n"


def main(parameter: str, region: str, path: str = ENV_PATH) -> int:
    raw = subprocess.check_output(
        ["aws", "ssm", "get-parameter", "--region", region, "--name", parameter, "--with-decryption",
         "--query", "Parameter.Value", "--output", "text"], timeout=30)
    creds = json.loads(raw)
    with open(path, encoding="utf-8") as file:
        current = file.read()
    merged = merge(current, creds)
    if merged == current:
        print("Dhan credentials already current")
        return 0
    st = os.stat(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(merged)
    os.chown(tmp, st.st_uid, st.st_gid)
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)
    print("Synced Dhan credential keys:", sorted(k for k in creds if k in KEYS and creds[k]))
    return 0


if __name__ == "__main__":
    name, aws_region = os.environ.get("DHAN_PARAMETER"), os.environ.get("AWS_REGION")
    if not name or not aws_region:
        print("DHAN_PARAMETER/AWS_REGION not set; skipping Dhan credential sync")
        sys.exit(0)
    sys.exit(main(name, aws_region))
