# 运行手册

面向部署与值班；开发流程见 `README.md`。

## 1. 启动与停止

```bash
cp .env.example .env
# 必改：SECRET_KEY、POSTGRES_PASSWORD、NEO4J_PASSWORD
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml ps
```

`ps` 中 `migrate` 显示 `Exited (0)` 是预期结果；`beat` 必须处于运行状态，否则 outbox 投影和所有定时任务都不会执行。

首次启动后访问 `http://localhost:8080/setup` 创建管理员。健康检查：

```bash
curl -s http://localhost:8000/health | jq
curl -s http://localhost:8000/metrics | head
```

`/health` 返回 `degraded` 表示某个依赖不可达，`components` 会指出具体组件；API 不会因此退出。

## 2. 数据库迁移

Compose 的 `migrate` 一次性服务会在 api/worker/beat 启动前执行迁移；正常情况下无需手工操作。检查：

```bash
docker compose -f infra/docker-compose.yml ps migrate
docker compose -f infra/docker-compose.yml run --rm migrate alembic -c apps/api/alembic.ini current
```

手工升级：

```bash
docker compose -f infra/docker-compose.yml run --rm migrate \
  alembic -c apps/api/alembic.ini upgrade head
```

- 生产升级前先备份（见第 4 节）。
- 迁移失败时 Alembic 会停在失败版本，先查看 `alembic_version` 与容器日志，再决定回滚或修复，不要直接删表。
- 容器内的 api 镜像包含 alembic 配置与迁移脚本；改迁移后需要重建镜像。

## 3. 索引重建与对账

Postgres 是唯一事实源。Qdrant/Neo4j 异常时优先重建，不要手工改索引。

```bash
# 单个工作区（推荐）
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.worker_cli rebuild --target all --workspace ws_demo

# 全量（会清理所有 workspace 的派生索引，打印跨租户警告）
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.worker_cli rebuild --target all --all
```

- `--workspace` 与 `--all` 必须二选一；空/空白 workspace 会直接退出 2。
- 选择 `--all` 前确认没有其他部署共用同一 Qdrant/Neo4j。
- 每小时对账会自动修复缺失/过期投影；`purged` 行的残留节点与文件也会被补删。
- 对账不会用假向量兜底：嵌入服务不可用时会计入 `failed/missing_after`，恢复后再跑。

## 4. 备份与恢复

```bash
bash infra/scripts/backup.sh
ls backups/latest
bash infra/scripts/restore.sh backups/latest --yes

# 恢复后必须重建索引
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.worker_cli rebuild --target all --all
```

- 备份内容：`pg.dump`（Postgres 自定义格式）、`assets.tar.gz`（资产卷）、`manifest.json`。
- 不包含 `.env`、Provider 密钥明文或 API Key 明文。
- 目标库非空时 `restore.sh` 默认拒绝覆盖，必须显式 `--yes`。
- 建议每日备份并异地保存；恢复演练至少每季度一次。

## 5. 评测与性能

```bash
# 黄金集（fake 嵌入，确定性；需要 Docker）
uv run python apps/api/tests/eval/run_eval.py --provider fake

# 真实模型（读取 .env 中的 DATABASE_URL/QDRANT_URL/NEO4J_*）
uv run python apps/api/tests/eval/run_eval.py --provider real

# 1 万条记忆检索 P95
uv run pytest apps/api/tests/perf -m perf -v -s
```

报告写入 `artifacts/eval-<timestamp>.json`。每次调整检索权重、遗忘参数或冲突阈值后都应跑一次，并记录基线。

## 6. 演示数据

```bash
make seed
# 或
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.seed --workspace ws_demo --memories 200 --review 20

# seed 直接写事实源，不经过 outbox；随后重建该 workspace 的派生索引，
# 否则 Playground 的向量/图通道在 beat 首次对账前是空的。
docker compose -f infra/docker-compose.yml exec -T api \
  python -m app.worker_cli rebuild --target all --workspace ws_demo
```

重复执行会清空并重建同一 workspace 的演示数据，不会影响其他 workspace。

## 7. 常见故障

| 现象 | 排查 |
|---|---|
| `make up` 卡在构建 | Docker Hub 不可达时给 Docker 配置镜像源；确认基础镜像可拉取 |
| `/health` 中 `postgres=false` | `docker compose logs postgres`；检查 `.env` 密码与卷权限 |
| `/health` 中 `qdrant=false` | 检查 `qdrant` 容器与 6333；检索会降级为其余通道并返回 `degraded` |
| `/health` 中 `neo4j=false` | Neo4j 首次启动较慢；`docker compose logs neo4j`；图通道降级不影响主检索 |
| `/mcp` 返回 503 | 通常是 MCP SDK 初始化失败或 `MCP_ENABLED=false`；看启动日志与 `/health.components.mcp` |
| MCP 工具报 `-32001` | API Key 无效/权限不足；确认 scope（read/write/admin） |
| MCP 工具报 `-32004` | 触发读 120/min、写 30/min 限流；等待或换 Key |
| 管理台写操作 403 `csrf_failed` | 会话令牌失效；刷新页面会从 `/auth/me` 重新水合，或重新登录 |
| 写入 422 `content_too_long` | 单条记忆超过 8192 个字符，拆分后再写 |
| 写入 422 `invalid_character` | 内容含 NUL 或孤立 UTF-16 代理对；清理后重试 |
| 检索结果为空 | 确认 `workspace_id` / scope 与写入一致；用调试台看四路候选与 `degraded` |
| 索引与事实源不一致 | 跑对账；仍不一致时按第 3 节重建索引 |
| 磁盘增长 | 检查 `pg_data`、`assets_data`；软删保留 30 天，审计 180 天，待审 90 天 |
| 回滚返回 409 `rollback_conflict` | 巩固后有人工修改；先手工确认冲突记忆，再决定是否强制处理 |

## 8. 安全与升级

- 生产必须替换默认 `SECRET_KEY` / 数据库密码 / Neo4j 密码；建议由密钥管理注入。
- 只在反向代理后暴露 api/web；`/mcp` 直连时配置 `MCP_ALLOWED_HOSTS` 与 `MCP_ALLOWED_ORIGINS`。
- API Key 只存 Argon2id 哈希；Provider 密钥 Fernet 加密；日志不输出密钥与完整记忆。
- 升级流程：备份 → 拉取新镜像 → 迁移 → 重建索引 → 跑 `/health` 与黄金集。
- 回滚镜像不会自动回滚数据库；迁移的 down 只在确认无新数据时使用。
