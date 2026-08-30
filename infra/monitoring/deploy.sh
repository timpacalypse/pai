#!/usr/bin/env bash
# Deploy PAI monitoring stack to Synology NAS (192.168.0.5)
# Usage: ./deploy.sh [user@synology]
set -euo pipefail

SYNOLOGY_HOST="${1:-192.168.0.5}"
SYNOLOGY_USER="admin"
SYNOLOGY_TARGET="${SYNOLOGY_USER}@${SYNOLOGY_HOST}"
REMOTE_DIR="/volume1/docker/pai-monitoring"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_BIN="/volume1/@appstore/ContainerManager/usr/bin/docker"

echo "==> Deploying monitoring stack to ${SYNOLOGY_TARGET}:${REMOTE_DIR}"

# Create remote directory structure
ssh "${SYNOLOGY_TARGET}" "mkdir -p ${REMOTE_DIR}/{data/grafana,data/prometheus}"

# Sync project files via tar-over-ssh (works on all Synology — no rsync/sftp needed)
echo "==> Uploading files..."
tar -cf - -C "${SCRIPT_DIR}" \
  --exclude='data' \
  --exclude='.env' \
  --exclude='__pycache__' \
  . \
  | ssh "${SYNOLOGY_TARGET}" "tar -xf - -C ${REMOTE_DIR}/"

echo "==> Files synced"

# Check if .env exists on remote, prompt if not
if ! ssh "${SYNOLOGY_TARGET}" "test -f ${REMOTE_DIR}/.env"; then
  echo "WARNING: No .env file found on Synology."
  echo "  1. ssh ${SYNOLOGY_TARGET}"
  echo "  2. cp ${REMOTE_DIR}/.env.example ${REMOTE_DIR}/.env"
  echo "  3. vi ${REMOTE_DIR}/.env   # fill in secrets"
  echo "  4. Re-run: ./deploy.sh"
  exit 1
fi

# Build and start — docker socket requires root on Synology
echo "==> Starting containers (requires root SSH)..."
ssh "root@${SYNOLOGY_HOST}" "cd ${REMOTE_DIR} && ${DOCKER_BIN} compose up -d --build"

echo "==> Stack deployed!"
echo "    Grafana:    http://${SYNOLOGY_HOST}:3001"
echo "    Prometheus: http://${SYNOLOGY_HOST}:9090"
