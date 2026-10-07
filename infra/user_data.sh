#!/bin/bash
# ---------------------------------------------------------------------------
# Bootstrap for the CashMatch application host.
#
# Runs once at first boot. Everything here is idempotent, because
# user_data_replace_on_change means a change to this file replaces the
# instance and runs it again from scratch.
#
# Logs land in /var/log/user-data.log, which is the first place to look when
# the site is not answering.
# ---------------------------------------------------------------------------
set -euxo pipefail
exec > >(tee /var/log/user-data.log | logger -t cashmatch -s 2>/dev/console) 2>&1

REGION="${region}"
ACCOUNT="${account_id}"
NAME="${name}"
API_IMAGE="${api_repository}:latest"
UI_IMAGE="${ui_repository}:latest"
LLM_MODE="${llm_mode}"

echo "=== packages ==="
dnf update -y
dnf install -y docker

# The compose plugin is not in the AL2023 repositories.
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL \
  "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

systemctl enable --now docker

echo "=== swap ==="
# t3.micro has 1 GB of RAM. The containers fit, but a `docker pull` of a
# large image plus Postgres client libraries can spike past it. 2 GB of swap
# costs nothing and turns an OOM kill into a slow second.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "=== secrets ==="
# Read through the instance role. Nothing is baked into the image or this
# script; rotating a value is a parameter update and a restart.
DATABASE_URL="$(aws ssm get-parameter --region "$REGION" \
  --name "/$NAME/database_url" --with-decryption --query Parameter.Value --output text)"
GEMINI_API_KEY="$(aws ssm get-parameter --region "$REGION" \
  --name "/$NAME/gemini_api_key" --with-decryption --query Parameter.Value --output text)"

install -d -m 0750 /opt/cashmatch
cat > /opt/cashmatch/.env <<EOF
DATABASE_URL=$DATABASE_URL
GEMINI_API_KEY=$GEMINI_API_KEY
LLM_MODE=$LLM_MODE
DATA_DIR=/data
LOG_LEVEL=INFO
EOF
chmod 0600 /opt/cashmatch/.env

echo "=== compose file ==="
cat > /opt/cashmatch/docker-compose.yml <<EOF
services:
  api:
    image: $API_IMAGE
    restart: unless-stopped
    env_file: /opt/cashmatch/.env
    volumes:
      - cashmatch_data:/data
    expose:
      - "8000"
    # No command override. The image's entrypoint runs migrations, seeds an
    # empty database and then serves -- overriding it here silently skipped
    # the seeding on the first deploy and left a live site with no data in
    # it, which is a confusing failure because nothing errored.
    healthcheck:
      test: ["CMD-SHELL", "python -c \"import urllib.request;urllib.request.urlopen('http://localhost:8000/health')\""]
      interval: 15s
      timeout: 5s
      retries: 10

  ui:
    image: $UI_IMAGE
    restart: unless-stopped
    ports:
      - "80:80"
    depends_on:
      api:
        condition: service_healthy

volumes:
  cashmatch_data:
EOF

echo "=== pull and start ==="
aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"

cd /opt/cashmatch
docker compose pull
docker compose up -d

echo "=== waiting for the application to report healthy ==="
# Migrations and seeding happen inside the container, in entrypoint.sh.
# Nothing to orchestrate from out here beyond confirming it came up.
for attempt in $(seq 1 40); do
  if curl -fsS http://localhost/ >/dev/null 2>&1; then
    echo "application is serving"
    break
  fi
  echo "waiting ($attempt/40)"
  sleep 15
done

echo "=== refresh script and unit ==="
# `systemctl start cashmatch-refresh` pulls the newest images and restarts.
# That is the whole deploy step once the infrastructure exists.
#
# The body lives in a script rather than inline in the unit: a systemd
# ExecStartPre that has to rediscover the region from instance metadata needs
# three levels of nested quoting to do it, and user_data already knows the
# region. Writing the script first also means this section is the last thing
# the bootstrap does, so a failure earlier on cannot leave a unit that
# references a script that was never written.
cat > /opt/cashmatch/refresh.sh <<EOF
#!/usr/bin/env bash
# Pull the newest images and restart. Called by cashmatch-refresh.service.
set -euo pipefail
cd /opt/cashmatch
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ACCOUNT.dkr.ecr.$REGION.amazonaws.com"
docker compose pull
docker compose up -d
EOF
chmod 0750 /opt/cashmatch/refresh.sh

cat > /etc/systemd/system/cashmatch-refresh.service <<'EOF'
[Unit]
Description=Pull the latest CashMatch images and restart
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/opt/cashmatch/refresh.sh
EOF
systemctl daemon-reload

echo "=== bootstrap complete ==="
