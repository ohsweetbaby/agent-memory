#!/usr/bin/env bash
# 从 backup.sh 生成的目录恢复 Postgres 与资产卷。
#
# 用法：bash infra/scripts/restore.sh <备份目录> [--yes]
# 非空库默认拒绝覆盖；恢复后必须重建索引：
#   uv run python -m app.worker_cli rebuild --all
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

if [[ $# -lt 1 ]]; then
  echo "usage: $0 <backup-dir> [--yes]" >&2
  exit 2
fi
SOURCE="$1"
ASSUME_YES="${2:-}"
if [[ ! -f "$SOURCE/manifest.json" ]]; then
  echo "[restore] 缺少 manifest.json：$SOURCE" >&2
  exit 2
fi
if [[ ! -f "$SOURCE/pg.dump" || ! -f "$SOURCE/assets.tar.gz" ]]; then
  echo "[restore] 备份不完整，需要 pg.dump 与 assets.tar.gz" >&2
  exit 2
fi

DB_USER="${POSTGRES_USER:-agent_memory}"
DB_NAME="${POSTGRES_DB:-agent_memory}"
COUNT="$(compose exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d '[:space:]' || echo 0)"
if [[ "${COUNT:-0}" != "0" && "$ASSUME_YES" != "--yes" ]]; then
  echo "[restore] 目标库非空（$COUNT 张表），拒绝覆盖。确认后加 --yes。" >&2
  exit 3
fi

echo "[restore] postgres <- $SOURCE/pg.dump"
compose exec -T postgres pg_restore --clean --if-exists --no-owner \
  -U "$DB_USER" -d "$DB_NAME" < "$SOURCE/pg.dump"

echo "[restore] assets   <- $SOURCE/assets.tar.gz"
compose exec -T worker tar -C /data/assets -xzf - < "$SOURCE/assets.tar.gz"

echo "[restore] done"
echo "[restore] 请重建索引：uv run python -m app.worker_cli rebuild --all"
