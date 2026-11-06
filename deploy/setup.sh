#!/usr/bin/env bash
# One-time setup on a fresh Vultr Ubuntu 24.04 VM. Run as root from the repo folder:
#   sudo bash deploy/setup.sh
# Then put your Serverless Inference key in /etc/safehands.env and: systemctl restart safehands
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"

echo "== Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

echo "== gVisor (runsc): the sandbox's system calls go to a user-space kernel, not the host's"
if ! command -v runsc >/dev/null; then
  curl -fsSL https://gvisor.dev/archive.key | gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
    > /etc/apt/sources.list.d/gvisor.list
  apt-get update && apt-get install -y runsc
  runsc install
  systemctl restart docker
fi
RUNTIME=runsc
if ! docker run --rm --runtime=runsc hello-world >/dev/null 2>&1; then
  echo "!! gVisor didn't start here; falling back to the default runtime (runc). Containment flags still apply."
  RUNTIME=""
fi

echo "== Sandbox image"
docker build -t safehands-sandbox:latest sandbox/

echo "== App"
apt-get install -y python3-venv
python3 -m venv .venv
.venv/bin/pip install -q fastapi "uvicorn[standard]"
mkdir -p runs

if [ ! -f /etc/safehands.env ]; then
  cat > /etc/safehands.env <<EOF
# Vultr Serverless Inference (https://my.vultr.com -> Serverless Inference -> API key)
SAFEHANDS_INFERENCE_KEY=
# Optional: pin a model id from https://api.vultrinference.com/v1/models
SAFEHANDS_MODEL=
SAFEHANDS_RUNTIME=$RUNTIME
SAFEHANDS_IMAGE=safehands-sandbox:latest
EOF
  chmod 600 /etc/safehands.env
fi

cat > /etc/systemd/system/safehands.service <<EOF
[Unit]
Description=SafeHands
After=docker.service
Requires=docker.service

[Service]
WorkingDirectory=$APP_DIR
EnvironmentFile=/etc/safehands.env
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80
Restart=always

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now safehands

if command -v ufw >/dev/null; then ufw allow 80/tcp || true; fi
echo
echo "SafeHands is running on http://$(curl -s -4 ifconfig.me || hostname -I | cut -d' ' -f1)/"
echo "Add your inference key to /etc/safehands.env, then: systemctl restart safehands"
