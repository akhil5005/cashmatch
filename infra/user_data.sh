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
    command: >
      sh -c "alembic upgrade head &&
             uvicorn cashmatch.api.app:app --host 0.0.0.0 --port 8000"
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

echo "=== seed the demo dataset ==="
# Only on an empty database. `generate` refuses to run against existing data
# without --reset, so a restart never silently wipes a populated instance.
for attempt in $(seq 1 30); do
  if docker compose exec -T api python -m cashmatch.cli health >/dev/null 2>&1; then
    break
  fi
  echo "waiting for the database ($attempt/30)"
  sleep 10
done

if docker compose exec -T api python -m cashmatch.cli generate >/dev/null 2>&1; then
  docker compose exec -T api python -m cashmatch.cli extract
  docker compose exec -T api python -m cashmatch.cli apply
  docker compose exec -T api python -m cashmatch.cli evaluate --no-markdown || true
  echo "demo dataset seeded"
else
  echo "database already populated, leaving it alone"
fi

echo "=== refresh unit ==="
# `systemctl start cashmatch-refresh` pulls the newest images and restarts.
# That is the whole deploy step once the infrastructure exists.
cat > /etc/systemd/system/cashmatch-refresh.service <<'EOF'
[Unit]
Description=Pull the latest CashMatch images and restart
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/cashmatch
ExecStartPre=/bin/bash -c 'aws ecr get-login-password --region $(curl -s http://169.254.169.254/latest/meta-data/placement/region -H "X-aws-ec2-metadata-token: $(curl -s -X PUT http://169.254.169.254/latest/api/token -H \"X-aws-ec2-metadata-token-ttl-seconds: 60\")") | docker login --username AWS --password-stdin $(docker compose config --images | head -1 | cut -d/ -f1)'
ExecStart=/usr/bin/docker compose pull
ExecStartPost=/usr/bin/docker compose up -d
EOF
systemctl daemon-reload

echo "=== bootstrap complete ==="
