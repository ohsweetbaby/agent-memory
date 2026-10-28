#!/usr/bin/env bash
# 备份 Postgres 事实源与资产卷；不打包 .env/密钥。
#
# 用法：bash infra/scripts/backup.sh [目标目录]
# 默认：<repo>/backups/<UTC 时间戳>
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT/infra/docker-compose.yml"
ENV_ARGS=()
if [[ -f "$ROOT/.env" ]]; then
  ENV_ARGS=(--env-file "$ROOT/.env")
fi
compose() {
  docker compose -f "$COMPOSE_FILE" "${ENV_ARGS[@]}" "$@"
}

TIMESTAMP="${1:-$ROOT/backups/$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$TIMESTAMP"

DB_USER="${POSTGRES_USER:-agent_memory}"
DB_NAME="${POSTGRES_DB:-agent_memory}"

echo "[backup] postgres -> $TIMESTAMP/pg.dump"
compose exec -T postgres pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$TIMESTAMP/pg.dump"

echo "[backup] assets   -> $TIMESTAMP/assets.tar.gz"
compose exec -T worker tar -C /data/assets -czf - . > "$TIMESTAMP/assets.tar.gz"

GIT_COMMIT="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
PG_SIZE="$(wc -c < "$TIMESTAMP/pg.dump" | tr -d ' ')"
ASSETS_SIZE="$(wc -c < "$TIMESTAMP/assets.tar.gz" | tr -d ' ')"
cat > "$TIMESTAMP/manifest.json" <<EOF
{
  "version": 1,
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "git_commit": "$GIT_COMMIT",
  "database": "$DB_NAME",
  "files": {
    "pg.dump": {"bytes": $PG_SIZE},
    "assets.tar.gz": {"bytes": $ASSETS_SIZE}
  }
}
EOF

rm -rf "$ROOT/backups/latest"
mkdir -p "$ROOT/backups"
cp -R "$TIMESTAMP" "$ROOT/backups/latest"

echo "[backup] done: $TIMESTAMP"
echo "[backup] latest: $ROOT/backups/latest"
