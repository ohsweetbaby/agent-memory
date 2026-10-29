# Agent Memory

一个独立于 Agent 框架的通用记忆服务：任意支持 MCP 的客户端都能读写记忆，人可以在 Web 管理台里检索、纠错、治理和审计。

- **类脑模型**：working / episodic / semantic / procedural / perceptual 五类记忆，带重要性、复习强化、遗忘归档、定期巩固与冲突取代。
- **混合检索**：Qdrant 向量 + Postgres 全文 + Neo4j 关联扩展 + 重要性/时近性先验，RRF 融合并返回“为什么召回”。
- **标准接入**：MCP Streamable HTTP 与 stdio 薄桥，10 个工具 + 3 个 Resources + 1 个 Prompt。
- **可治理**：待审队列、记忆详情/版本链/审计、任务 dry-run/回滚、检索调试台。
- **可运维**：Postgres 是唯一事实源，Qdrant/Neo4j 均可重建；备份/恢复/对账脚本齐备。

## 架构

```
MCP clients ──► api (FastAPI + MCP /mcp) ──► postgres（事实源）
Browser ─────► web (Nginx + React)      ├──► redis（队列/限流）
                                        └──► worker（Celery）
                                               ├──► qdrant（向量，派生）
                                               └──► neo4j（关联图，派生）
```

## 5 分钟启动

前置：Docker Desktop（或 Docker Engine + Compose），建议 4 核 8 GB。

```bash
cp .env.example .env
```

至少修改 `.env` 中的三项：

```dotenv
SECRET_KEY=<用 python -c "import secrets; print(secrets.token_urlsafe(48))" 生成>
POSTGRES_PASSWORD=<自定义>
NEO4J_PASSWORD=<自定义>
```

模型有两种起步方式：

- 先不接模型：`EMBEDDING_PROVIDER=fake` 可跑通写入、检索、管理台与 MCP（向量是确定性词面哈希，仅用于本地体验）。
- 接真实模型：设置 `EMBEDDING_PROVIDER=openai`（或兼容 OpenAI 的本地服务），填写 `EMBEDDING_BASE_URL` / `LLM_BASE_URL` / `LLM_API_KEY`。

启动：

```bash
docker compose -f infra/docker-compose.yml up -d --build
docker compose -f infra/docker-compose.yml ps
```

`migrate` 一次性执行迁移后会以 `Exited (0)` 结束；`beat` 负责 outbox 投影与定时巩固/遗忘，必须保持运行。

1. 打开 <http://localhost:8080/setup>，创建管理员。
2. 登录后进入「设置」→ 新建 Provider，点击「测试连接」。
   M1 的运行时模型仍以 `.env` 的 `EMBEDDING_*` / `LLM_*` 为准；Provider 页面用于保存与校验端点，切换模型时同步修改 `.env` 并重启 api/worker。
3. 新建一个 API Key（明文只展示一次，请立即复制）。
4. 进入「记忆」写入几条内容，或执行 `make seed` 生成演示数据。

## 接入 MCP

### Streamable HTTP

```json
{
  "mcpServers": {
    "agent-memory": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer mem_xxx_yyy" }
    }
  }
}
```

### stdio（Claude Desktop / Cursor 等）

先安装桥接命令（开发环境）：

```bash
uv sync
uv run memory-mcp --api-url http://localhost:8000 --api-key mem_xxx_yyy
```

客户端配置：

```json
{
  "mcpServers": {
    "agent-memory": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/agent-memory", "memory-mcp"],
      "env": {
        "MEMORY_API_URL": "http://localhost:8000",
        "MEMORY_API_KEY": "mem_xxx_yyy"
      }
    }
  }
}
```

工具：`memory_search`、`memory_write`、`memory_get`、`memory_update`、`memory_forget`、`memory_feedback`、`memory_consolidate`、`memory_stats`、`memory_timeline`、`conversation_ingest`。

## 本地开发

```bash
uv sync --all-packages
uv run alembic -c apps/api/alembic.ini upgrade head
uv run uvicorn --factory app.main:create_app --reload --port 8000
```

前端：

```bash
pnpm --dir apps/web install
pnpm --dir apps/web dev
```

注意：`infra/docker-compose.yml` 只把 api/web 暴露到宿主机。本机直接跑 API 时，需要把 `DATABASE_URL`、`REDIS_URL`、`QDRANT_URL`、`NEO4J_URI` 指向你自行启动的依赖（或临时加端口映射）。容器内运行时保持服务名即可。

## 常用命令

```bash
make up                 # 构建并启动全部服务
make down               # 停止
make logs               # 跟踪日志
make seed               # 写入 200 条演示记忆 + 20 条待审
make test               # 后端全量（unit + integration + eval）
make e2e                # Playwright（真实本地 API）
make backup             # 备份 Postgres + 资产卷
make restore BACKUP=backups/latest ARGS=--yes
```

评测与性能：

```bash
uv run python apps/api/tests/eval/run_eval.py --provider fake
uv run python apps/api/tests/eval/run_eval.py --provider real
uv run pytest apps/api/tests/perf -m perf -v -s
```

## 配置要点

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | Postgres 连接串，默认容器内服务名 |
| `EMBEDDING_PROVIDER` | `openai` / `local` / `fake` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | 决定 Qdrant collection 身份 |
| `LLM_BASE_URL` / `LLM_API_KEY` | 抽取、巩固、冲突判定的模型服务 |
| `AUTO_ACCEPT_CONFIDENCE` / `AUTO_ACCEPT_IMPORTANCE` | 自动入库双阈值 |
| `DECAY_*` / `PURGE_*` | 遗忘与清理 |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | 直接暴露 MCP 时的 DNS rebinding 白名单 |

完整列表见 `.env.example`。

## 文档

- 设计规格：`docs/superpowers/specs/2026-10-27-agent-memory-system-design.md`
- 实现计划：`docs/superpowers/plans/2026-10-27-agent-memory-m1.md`
- 运行手册：`docs/runbook.md`

## 安全说明

- 管理台使用 HttpOnly Cookie + CSRF；MCP 使用 `mem_` 前缀 API Key（read / write / admin 三档）。
- Provider 密钥以 Fernet 加密存储，列表不回显明文。
- 硬删除需要 `confirm=true` 且 admin 权限，并级联清理 Qdrant/Neo4j/资产。
- 记忆内容视为不可信数据；MCP 返回的 `context_text` 不应被当作系统指令。
