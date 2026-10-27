# 通用智能体记忆系统 M1 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 交付一个可本地自托管的通用记忆服务：五类记忆模型、衰减/遗忘/巩固、四路混合检索、10 个 MCP 工具、以及可治理的 Web 管理台。

**架构：** 7 容器分离式部署（api / worker / web / postgres / qdrant / neo4j / redis）。Postgres 是唯一事实源，Qdrant 与 Neo4j 通过 outbox 异步投影且可重建；纯领域逻辑放在 `packages/memory-core`，与 IO 解耦。

**技术栈：** Python 3.12、FastAPI、SQLAlchemy 2、Alembic、Pydantic v2、Celery 5、PostgreSQL 16、Qdrant、Neo4j 5、Redis 7、React 18、Vite、TypeScript、shadcn/ui、TanStack Query、Playwright、pytest、testcontainers。

**规格：** `docs/superpowers/specs/2026-10-27-agent-memory-system-design.md`（执行前必须同时阅读本计划与规格）

## 全局约束

- 运行时版本：Python 3.12、Node 20、PostgreSQL 16、Redis 7、Qdrant 1.x、Neo4j 5 Community。
- 时间一律存 UTC `timestamptz`；主键一律 UUIDv7（`uuid6.uuid7()`）；ID 以字符串对外。
- 所有数据库查询必须带 `workspace_id`；跨工作区访问返回 404，不返回 403。
- Postgres 是唯一事实源；Qdrant/Neo4j 只能经 outbox 或 `rebuild-index` 写入，业务代码不得直接双写。
- 单条记忆内容上限 8192 字符；空内容或超限返回 422，绝不返回 500。
- 检索默认 `balanced`；上下文预算 1500 tokens；RRF `k=60`。
- 遗忘参数：`S0=7.0` 天、`archive_threshold=0.15`、`importance_alpha=2.0`、`access_beta=0.8`、`rehearsal_factor=1.5`、`max_rehearsal_factor=10.0`。
- 自动抽取入库阈值：`confidence >= 0.75` 且 `importance >= 0.5`；否则进待审队列。
- REST 错误体固定 `{"error": {"code": str, "message": str, "details": object|null}}`；MCP 错误码 `-32001` 至 `-32006`。
- Web 界面文案中文；时间按浏览器本地时区展示，存储与 API 传输一律 UTC。
- 日志为 JSON 结构化；禁止输出 API Key、Provider 密钥、完整记忆内容（只输出 id、类型与长度）。
- 测试命令：`uv run pytest`、`pnpm --dir apps/web test`、`pnpm --dir apps/web test:e2e`；集成测试需要 Docker。

## 审查重点（Review Focus）

1. **空内容 / 超过 8 KB 的内容**：必须返回 422 与字段路径，不能 500、不能静默截断——由任务 9 的 `test_rejects_blank_content`、`test_rejects_content_over_8kb` 钉住。
2. **嵌入服务或 Qdrant 不可用**：写入必须成功落 Postgres，检索必须降级为其余通道并标注 `degraded`，不能整体报错——由任务 10 的 `test_projection_retries_when_qdrant_down`、任务 11 的 `test_search_degrades_when_vector_channel_unavailable` 钉住。
3. **同一 `idempotency_key` 并发写入**：只能产生一条记忆，两个请求返回同一 id——由任务 9 的 `test_concurrent_same_idempotency_key_creates_one_memory` 钉住。
4. **用其他 workspace 的 ID 直接读取**：必须 404，不能泄漏存在性——由任务 12 的 `test_get_memory_from_other_workspace_returns_404` 钉住。
5. **记忆内容含 HTML / 控制字符**：API 原样返回，Web 必须以纯文本渲染、不得执行脚本——由任务 12 的 `test_control_characters_roundtrip` 与任务 18 的 `test_memory_content_is_rendered_as_text` 钉住。

## 文件结构

```
agent-memory/
├── pyproject.toml                     # uv workspace 根
├── Makefile                           # up/down/test/e2e/seed/backup
├── .env.example  .gitignore  README.md
├── infra/
│   ├── docker-compose.yml             # 7 容器
│   ├── docker-compose.lite.yml        # 无 Neo4j 的降级组合
│   ├── nginx.conf
│   └── scripts/                       # backup.sh restore.sh
├── apps/api/
│   ├── Dockerfile  pyproject.toml  alembic.ini
│   ├── alembic/versions/
│   └── app/
│       ├── main.py  config.py  db.py  errors.py
│       ├── models/                    # SQLAlchemy：memory.py workspace.py ops.py auth.py
│       ├── schemas/                   # Pydantic：memory.py search.py auth.py ops.py
│       ├── api/                       # auth.py memories.py search.py review.py jobs.py providers.py keys.py stats.py export.py
│       ├── auth/                      # password.py session.py api_key.py
│       ├── services/                  # memory_write.py search.py review.py consolidation.py lifecycle.py export.py stats.py
│       ├── adapters/                  # qdrant.py neo4j.py fts.py embeddings.py llm.py assets.py
│       ├── ports/                     # protocols.py
│       ├── jobs/                      # celery_app.py tasks.py schedules.py
│       └── mcp/                       # server.py tools.py resources.py
│   └── tests/{unit,integration,contract}/
├── packages/memory-core/
│   ├── pyproject.toml
│   ├── memory_core/                   # types.py scope.py importance.py decay.py dedupe.py fusion.py context.py
│   └── tests/
├── packages/mcp-stdio/
│   ├── pyproject.toml
│   └── memory_mcp/__main__.py
├── apps/web/
│   ├── package.json  vite.config.ts  playwright.config.ts
│   ├── src/{app,pages,components,lib,hooks}/
│   └── e2e/
└── apps/api/tests/fixtures/golden/    # 黄金集与评测脚本
```

## 测试约定

- 单元测试不依赖 Docker；集成测试用 testcontainers 起 PG/Qdrant/Neo4j/Redis，并标记 `@pytest.mark.integration`。
- 共享 fixture 统一放 `apps/api/tests/conftest.py` 与 `apps/api/tests/integration/conftest.py`：`client, db_session, session_factory, admin, workspace, memory, seeded_memories, qdrant_index, neo4j_index, fake_embedding, fake_llm, run_worker_once, run_concurrently`；任务内首次出现的领域 fixture（如 `superseded_pair`、`mergeable_memories`）在所属任务的测试文件里就地定义或提升到 conftest。
- 测试辅助函数：`valid_write_payload, make_memory, snapshot_counts, run_alembic, has_table, read_export, run_rebuild, raise_connection_error`，放在对应测试模块或 conftest。
- 每个任务先跑该任务的测试，再跑全量；集成测试失败时先确认 Docker 可用。

---

### 任务 1：仓库骨架与运行环境

**文件：**
- 创建：`pyproject.toml`、`.gitignore`、`.env.example`、`Makefile`、`infra/docker-compose.yml`、`infra/docker-compose.lite.yml`、`infra/nginx.conf`、`apps/api/Dockerfile`、`apps/api/pyproject.toml`、`apps/api/app/main.py`、`apps/api/app/config.py`、`apps/api/app/logging.py`、`apps/api/tests/conftest.py`、`apps/api/tests/unit/test_config.py`、`apps/api/tests/unit/test_health.py`、`apps/api/tests/unit/test_logging.py`

**接口：**
- `app.config.Settings`（pydantic-settings）：`database_url, redis_url, qdrant_url, neo4j_uri, neo4j_user, neo4j_password, secret_key, assets_dir, embedding_provider, embedding_model, embedding_dim, llm_base_url, llm_api_key`
- `app.main.create_app() -> FastAPI`，`GET /health` 返回 `{"status": "ok|degraded", "components": {"postgres": bool, "redis": bool, "qdrant": bool, "neo4j": bool}}`
- `GET /metrics`（Prometheus 文本格式，暴露 `http_requests_total{route,status}`）
- `app.logging.configure_logging() -> None`（JSON 行日志）

- [ ] **步骤 1：编写失败的测试**

```python
# apps/api/tests/unit/test_config.py
def test_settings_parse_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/mem")
    monkeypatch.setenv("SECRET_KEY", "x" * 32)
    settings = Settings()
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.embedding_dim == 1536

# apps/api/tests/unit/test_health.py
def test_health_reports_degraded_when_neo4j_down(client, monkeypatch):
    monkeypatch.setattr("app.main.check_neo4j", lambda: False)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["components"]["neo4j"] is False

def test_metrics_endpoint_exposes_request_counter(client):
    client.get("/health")
    body = client.get("/metrics").text
    assert "http_requests_total" in body

# apps/api/tests/unit/test_logging.py
def test_logs_are_json_lines(capsys):
    configure_logging()
    logging.getLogger("app").info("hello", extra={"memory_id": "m1"})
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO" and payload["memory_id"] == "m1"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/unit/test_config.py apps/api/tests/unit/test_health.py apps/api/tests/unit/test_logging.py -v`
预期：FAIL，`ModuleNotFoundError: No module named 'app'`。

- [ ] **步骤 3：创建 uv workspace、配置、compose 与最小 API**

`infra/docker-compose.yml` 包含 postgres/qdrant/neo4j/redis/api/worker/web 七个服务，全部带 healthcheck 与命名卷 `pg_data, qdrant_data, neo4j_data, assets_data`；`web` 依赖 `api`。`Makefile` 提供 `up down logs test e2e seed backup restore`。`Settings` 用 `pydantic-settings` 从环境变量读取，`create_app()` 注册 `/health`、`/metrics` 与 CORS（仅开发环境放开）；`tests/conftest.py` 提供 `client` fixture（`TestClient(create_app())`）与依赖覆盖入口。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/unit -v && docker compose -f infra/docker-compose.yml config -q`
预期：全部 PASS；compose 配置无输出、退出码 0。

- [ ] **步骤 5：Commit**

```bash
git add pyproject.toml .gitignore .env.example Makefile infra apps/api
git commit -m "chore: scaffold monorepo, compose stack and health endpoint"
```

---

### 任务 2：记忆类型与作用域（memory-core）

**文件：**
- 创建：`packages/memory-core/pyproject.toml`、`packages/memory-core/memory_core/__init__.py`、`packages/memory-core/memory_core/types.py`、`packages/memory-core/memory_core/scope.py`、`packages/memory-core/tests/test_scope.py`

**接口：**
- `MemoryType(str, Enum)`：`WORKING/EPISODIC/SEMANTIC/PROCEDURAL/PERCEPTUAL`
- `Scope(str, Enum)`：`GLOBAL/AGENT/USER/SESSION`
- `MemoryStatus(str, Enum)`：`ACTIVE/ARCHIVED/SUPERSEDED/DELETED/PURGED`
- `@dataclass(frozen=True) ScopeContext(workspace_id, agent_id=None, end_user_id=None, session_id=None)`
- `visible_scopes(ctx: ScopeContext, requested: list[Scope] | None = None) -> list[tuple[Scope, str | None]]`

- [ ] **步骤 1：编写失败的测试**

```python
def test_visible_scopes_merges_global_with_context():
    ctx = ScopeContext("ws1", agent_id="a1", end_user_id="u1", session_id="s1")
    assert visible_scopes(ctx) == [
        (Scope.GLOBAL, None), (Scope.AGENT, "a1"),
        (Scope.USER, "u1"), (Scope.SESSION, "s1"),
    ]

def test_missing_identifiers_yield_only_global():
    assert visible_scopes(ScopeContext("ws1")) == [(Scope.GLOBAL, None)]

def test_requested_scopes_cannot_escape_context():
    ctx = ScopeContext("ws1", agent_id="a1")
    assert visible_scopes(ctx, [Scope.USER, Scope.SESSION]) == [(Scope.GLOBAL, None)]
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest packages/memory-core/tests/test_scope.py -v`
预期：FAIL，`ImportError: cannot import name 'visible_scopes'`。

- [ ] **步骤 3：实现类型与作用域解析**

`visible_scopes` 只返回当前上下文确实存在的标识符；`requested` 是过滤而非授权，不能引入上下文里没有的作用域。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest packages/memory-core/tests/test_scope.py -v`
预期：3 passed。

- [ ] **步骤 5：Commit**

```bash
git add packages/memory-core
git commit -m "feat(memory-core): memory types and scope visibility"
```

---

### 任务 3：重要性评分

**文件：**
- 创建：`packages/memory-core/memory_core/importance.py`、`packages/memory-core/tests/test_importance.py`

**接口：**
- `@dataclass(frozen=True) ImportanceSignals(explicit_remember=False, pinned=False, helpful_feedback=0)`
- `score_importance(content: str, memory_type: MemoryType, signals: ImportanceSignals, llm_score: float | None) -> float`
- `effective_llm_score(llm_score: float | None, signals: ImportanceSignals) -> float`

- [ ] **步骤 1：编写失败的测试**

```python
def test_working_memory_is_capped_at_half():
    assert score_importance("临时信息", MemoryType.WORKING, ImportanceSignals(), 0.9) == 0.5

def test_explicit_remember_has_floor_090():
    assert score_importance("记住我不吃香菜", MemoryType.SEMANTIC, ImportanceSignals(explicit_remember=True), 0.2) == 0.9

def test_helpful_feedback_increases_but_clamps():
    assert score_importance("x", MemoryType.SEMANTIC, ImportanceSignals(helpful_feedback=20), 0.99) == 1.0

def test_missing_llm_score_falls_back_to_neutral():
    assert score_importance("普通内容", MemoryType.SEMANTIC, ImportanceSignals(), None) == 0.5
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest packages/memory-core/tests/test_importance.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现评分**

`score_importance` 以 `llm_score` 为基线（缺失时 0.5），加规则修正（显式记住取 `max(score, 0.9)`、置顶为 1.0、每次 helpful 反馈 +0.05），最后按类型上限裁剪并 clamp 到 `[0, 1]`。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest packages/memory-core/tests/test_importance.py -v`
预期：4 passed。

- [ ] **步骤 5：Commit**

```bash
git add packages/memory-core
git commit -m "feat(memory-core): importance scoring with type caps and signals"
```

---

### 任务 4：遗忘与复习强化

**文件：**
- 创建：`packages/memory-core/memory_core/decay.py`、`packages/memory-core/tests/test_decay.py`

**接口：**
- `@dataclass(frozen=True) DecayParams(base_stability_days=7.0, archive_threshold=0.15, importance_alpha=2.0, access_beta=0.8, rehearsal_factor=1.5, max_rehearsal_factor=10.0)`
- `stability_days(importance: float, access_count: int, rehearsal_factor: float, params: DecayParams) -> float`
- `retention(last_reinforced_at: datetime, now: datetime, stability_days: float) -> float`
- `should_archive(last_reinforced_at, now, importance, access_count, rehearsal_factor, params) -> bool`
- `apply_rehearsal(current: float, params: DecayParams) -> float`

- [ ] **步骤 1：编写失败的测试**

```python
NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)

def test_retention_is_one_at_zero_elapsed():
    assert retention(NOW, NOW, 10.0) == 1.0

def test_higher_importance_decays_slower():
    assert stability_days(0.9, 0, 1.0, DecayParams()) > stability_days(0.2, 0, 1.0, DecayParams())

def test_30_days_low_importance_is_archived_high_is_not():
    low = NOW - timedelta(days=30)
    assert should_archive(low, NOW, 0.2, 0, 1.0, DecayParams()) is True
    assert should_archive(low, NOW, 0.9, 3, 1.5, DecayParams()) is False

def test_rehearsal_doubles_stability_capped():
    assert apply_rehearsal(1.0, DecayParams()) == 1.5
    assert apply_rehearsal(9.0, DecayParams()) == 10.0

def test_archive_threshold_boundary_is_strict():
    # S=7 天、Δ=7 天时 R=exp(-1)，R 等于阈值不归档
    params = DecayParams(archive_threshold=math.exp(-1))
    assert should_archive(NOW - timedelta(days=7), NOW, 0.0, 0, 0.0, params) is False
    assert should_archive(NOW - timedelta(days=7.01), NOW, 0.0, 0, 0.0, params) is True
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest packages/memory-core/tests/test_decay.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：按规格公式实现**

严格按规格 5.4 的公式：`S = S0 × (1 + α·importance) × (1 + β·ln(1+access_count)) × rehearsal_factor`，`R = exp(-Δt/S)`；`rehearsal_factor <= 0` 视为 1；`R < threshold` 才归档（相等不归档）；未来时间戳按 `Δt=0` 处理。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest packages/memory-core/tests/test_decay.py -v`
预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add packages/memory-core
git commit -m "feat(memory-core): ebbinghaus retention, rehearsal and archive policy"
```

---

### 任务 5：去重与冲突决策

**文件：**
- 创建：`packages/memory-core/memory_core/dedupe.py`、`packages/memory-core/tests/test_dedupe.py`

**接口：**
- `@dataclass(frozen=True) ExistingCandidate(id, content, similarity, subject_predicate: tuple[str, str] | None = None, valid_to: datetime | None = None)`
- `ConflictKind(str, Enum)`：`CREATE/MERGE/SUPERSEDE/COEXIST`
- `@dataclass(frozen=True) ConflictDecision(kind: ConflictKind, target_id: str | None, reason: str)`
- `decide_conflict(new_content: str, candidates: list[ExistingCandidate], *, contradiction: bool | None, temporal_change: bool = False, merge_threshold=0.92, conflict_threshold=0.75) -> ConflictDecision`

- [ ] **步骤 1：编写失败的测试**

```python
def test_near_duplicate_merges():
    c = ExistingCandidate("m1", "用户住在北京", 0.95, ("用户", "居住"))
    assert decide_conflict("用户住在北京市", [c], contradiction=None).kind is ConflictKind.MERGE

def test_explicit_contradiction_supersedes_old():
    c = ExistingCandidate("m1", "用户住在北京", 0.85, ("用户", "居住"))
    decision = decide_conflict("用户搬到上海", [c], contradiction=True)
    assert decision.kind is ConflictKind.SUPERSEDE and decision.target_id == "m1"

def test_temporal_change_coexists():
    c = ExistingCandidate("m1", "用户住在北京", 0.85, ("用户", "居住"))
    assert decide_conflict("用户搬到上海", [c], contradiction=True, temporal_change=True).kind is ConflictKind.COEXIST

def test_unrelated_content_creates():
    c = ExistingCandidate("m1", "用户住在北京", 0.4, ("用户", "居住"))
    assert decide_conflict("用户喜欢摇滚", [c], contradiction=None).kind is ConflictKind.CREATE

def test_high_similarity_without_contradiction_still_merges():
    c = ExistingCandidate("m1", "用户偏好 Python", 0.99, ("用户", "偏好"))
    assert decide_conflict("用户偏爱 Python", [c], contradiction=False).kind is ConflictKind.MERGE
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest packages/memory-core/tests/test_dedupe.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现决策规则**

按规格 5.3 的优先级：相似度 ≥ merge 阈值 → MERGE；`temporal_change` 为真 → COEXIST；`contradiction=True` → SUPERSEDE（取相似度最高的候选）；其余 → CREATE。候选为空直接 CREATE。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest packages/memory-core/tests/test_dedupe.py -v`
预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add packages/memory-core
git commit -m "feat(memory-core): deterministic dedupe and conflict decisions"
```

---

### 任务 6：RRF 融合与上下文组装

**文件：**
- 创建：`packages/memory-core/memory_core/fusion.py`、`packages/memory-core/memory_core/context.py`、`packages/memory-core/tests/test_fusion.py`、`packages/memory-core/tests/test_context.py`

**接口：**
- `rrf(rankings: dict[str, list[str]], k: int = 60) -> dict[str, float]`
- `final_score(rrf_score: float, importance: float, scope: Scope) -> float`
- `@dataclass(frozen=True) ContextItem(id, type, content, importance, confidence, retention, timestamp, scope, source_quote=None, why=None)`
- `estimate_tokens(text: str) -> int`（`max(1, ceil(len(text)/2))`）
- `assemble_context(items: list[ContextItem], budget_tokens: int = 1500) -> tuple[str, list[ContextItem]]`

- [ ] **步骤 1：编写失败的测试**

```python
def test_rrf_rewards_agreement():
    scores = rrf({"vector": ["a", "b"], "bm25": ["b", "a"]})
    assert scores["a"] == scores["b"]
    assert scores["a"] > 0

def test_scope_weight_orders_session_before_global():
    assert final_score(0.1, 0.5, Scope.SESSION) > final_score(0.1, 0.5, Scope.GLOBAL)

def test_context_truncates_to_budget():
    items = [ContextItem(str(i), "semantic", "字" * 400, 0.5, 0.9, 0.8, NOW, Scope.USER) for i in range(10)]
    text, kept = assemble_context(items, budget_tokens=100)
    assert len(kept) < 10
    assert estimate_tokens(text) <= 100

def test_context_format_contains_type_time_confidence():
    item = ContextItem("m1", "semantic", "用户对花生过敏", 0.9, 0.95, 0.8, NOW, Scope.USER, "我吃花生会过敏")
    text, _ = assemble_context([item])
    assert text.startswith("[语义|2026-10-27|0.95|")
    assert "我吃花生会过敏" in text

def test_near_duplicate_items_are_collapsed():
    a = ContextItem("a", "semantic", "用户对花生过敏", 0.9, 0.9, 0.8, NOW, Scope.USER)
    b = ContextItem("b", "semantic", "用户对花生严重过敏", 0.7, 0.8, 0.8, NOW, Scope.USER)
    _, kept = assemble_context([a, b])
    assert len(kept) == 1
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest packages/memory-core/tests/test_fusion.py packages/memory-core/tests/test_context.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现融合与组装**

`final_score` 按 `rrf × (0.8 + 0.2×importance) × scope_weight`；`scope_weight` 为 session 1.0、agent 0.98、user 0.96、global 0.94。组装按分数降序，分组顺序为语义/程序性/情景，超预算截断，文本近似重复（内容包含关系或编辑距离比 ≤ 0.2）只保留分数高者，来源原话截到 200 字。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest packages/memory-core/tests/test_fusion.py packages/memory-core/tests/test_context.py -v`
预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add packages/memory-core
git commit -m "feat(memory-core): rrf fusion and budgeted context assembly"
```

---

### 任务 7：数据库模型、迁移与仓储

**文件：**
- 创建：`apps/api/app/db.py`、`apps/api/app/models/__init__.py`、`apps/api/app/models/base.py`、`apps/api/app/models/workspace.py`、`apps/api/app/models/memory.py`、`apps/api/app/models/ops.py`、`apps/api/app/models/auth.py`、`apps/api/app/repositories/__init__.py`、`apps/api/app/repositories/memory.py`、`apps/api/app/repositories/outbox.py`、`apps/api/alembic.ini`、`apps/api/alembic/env.py`、`apps/api/alembic/versions/0001_initial.py`、`apps/api/tests/integration/conftest.py`、`apps/api/tests/integration/test_migrations.py`、`apps/api/tests/integration/test_memory_repository.py`

**接口：**
- `Base`（SQLAlchemy `DeclarativeBase`，`id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid7)`)
- 表：`workspace, agent, end_user, session, memory, memory_embedding, memory_link, entity, memory_entity, episode, review_item, job_run, audit_log, outbox_event, projection_state, api_key, provider_config`
- `MemoryRepository(session)`：`add(memory) -> Memory`、`get(workspace_id, memory_id) -> Memory | None`、`list(workspace_id, filters: MemoryFilters) -> list[Memory]`、`update(memory, **fields)`、`find_similar(workspace_id, scope_pairs, content, limit=3) -> list[tuple[Memory, float]]`
- `OutboxRepository.add(event_type, aggregate_id, payload)`、`claim_batch(limit=50)`

- [ ] **步骤 1：编写失败的测试**

```python
def test_initial_migration_up_and_down(db_engine):
    run_alembic("upgrade", "head")
    assert has_table(db_engine, "memory")
    run_alembic("downgrade", "base")
    assert not has_table(db_engine, "memory")

def test_memory_roundtrip_is_workspace_scoped(db_session):
    repo = MemoryRepository(db_session)
    memory = repo.add(make_memory(workspace_id="ws1", content="用户对花生过敏"))
    assert repo.get("ws1", memory.id).content == "用户对花生过敏"
    assert repo.get("ws2", memory.id) is None

def test_similarity_search_is_scoped_by_scope(db_session):
    repo = MemoryRepository(db_session)
    repo.add(make_memory(workspace_id="ws1", scope="user", scope_id="u1", content="用户住在北京"))
    repo.add(make_memory(workspace_id="ws1", scope="user", scope_id="u2", content="用户住在北京"))
    hits = repo.find_similar("ws1", [("user", "u1")], "用户住在北京市")
    assert all(h.scope_id == "u1" for h, _ in hits)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_migrations.py apps/api/tests/integration/test_memory_repository.py -v`
预期：FAIL，`ModuleNotFoundError`。

- [ ] **步骤 3：实现模型与迁移**

在 `0001_initial` 中启用 `pg_trgm` 与 `tsvector` 触发器；`memory` 表含规格 10.1 的全部字段与索引，其中 `version` 默认 1，任何更新自增；`api_key(id, workspace_id, name, prefix, key_hash, scopes, rate_limit, ...)`；`provider_config` 的 `encrypted_secret` 为 `LargeBinary`。**`metadata` 是 SQLAlchemy 保留名，Python 属性统一写 `metadata_`，数据库列名仍是 `metadata`。** `find_similar` 用 `similarity(content, :q)` 排序并返回分数。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_migrations.py apps/api/tests/integration/test_memory_repository.py -v`
预期：3 passed；Docker 未启动时提示 testcontainers 错误。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/alembic apps/api/alembic.ini apps/api/app/db.py apps/api/app/models apps/api/app/repositories apps/api/tests/integration
git commit -m "feat(api): schema, migrations and workspace-scoped repositories"
```

---

### 任务 8：管理员认证与 API Key

**文件：**
- 创建：`apps/api/app/errors.py`、`apps/api/app/auth/password.py`、`apps/api/app/auth/session.py`、`apps/api/app/auth/api_key.py`、`apps/api/app/api/auth.py`、`apps/api/app/api/keys.py`、`apps/api/app/api/workspaces.py`、`apps/api/app/api/agents.py`、`apps/api/tests/integration/test_auth.py`、`apps/api/tests/integration/test_api_keys.py`、`apps/api/tests/integration/test_workspaces.py`

**接口：**
- `hash_password(raw: str) -> str`（Argon2id）、`verify_password(raw, hashed) -> bool`
- `encrypt_secret(raw: str) -> bytes`、`decrypt_secret(blob: bytes) -> str`（Fernet，密钥派生自 `SECRET_KEY`）
- `create_session(response, admin_id)`、`require_admin(request) -> AdminUser`
- `create_api_key(session, workspace_id, name, scopes: list[str]) -> tuple[ApiKey, str]`（返回一次性明文）
- `verify_api_key(session, raw: str) -> ApiKey | None`
- 路由：`POST /api/v1/auth/setup`、`POST /auth/login`、`POST /auth/logout`、`GET /auth/me`、`GET/POST /keys`、`DELETE /keys/{id}`、`GET/POST /workspaces`、`GET/PATCH /workspaces/{id}`、`GET/POST /agents`、`GET/PATCH/DELETE /agents/{id}`、`GET/POST /end-users`

- [ ] **步骤 1：编写失败的测试**

```python
def test_setup_allowed_only_once(client):
    assert client.post("/api/v1/auth/setup", json={"password": "p" * 12}).status_code == 201
    assert client.post("/api/v1/auth/setup", json={"password": "p" * 12}).status_code == 409

def test_login_sets_httponly_cookie(client, admin):
    r = client.post("/api/v1/auth/login", json={"password": "p" * 12})
    assert r.status_code == 200
    assert "httponly" in r.headers["set-cookie"].lower()

def test_api_key_hash_is_not_plaintext(db_session, admin):
    key, raw = create_api_key(db_session, "ws1", "cursor", ["read"])
    assert key.key_hash != raw and key.key_hash.startswith("$argon2")

def test_revoked_key_is_rejected(client, revoked_key):
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {revoked_key}"}).status_code == 401

def test_login_is_rate_limited_after_five_failures(client, admin):
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"password": "wrong"})
    r = client.post("/api/v1/auth/login", json={"password": "p" * 12})
    assert r.status_code == 429

def test_provider_secret_is_encrypted_and_roundtrips():
    blob = encrypt_secret("sk-secret")
    assert b"sk-secret" not in blob
    assert decrypt_secret(blob) == "sk-secret"

def test_workspace_and_agent_crud_is_isolated(client, admin):
    ws = client.post("/api/v1/workspaces", json={"name": "个人"}).json()
    agent = client.post("/api/v1/agents", json={"workspace_id": ws["id"], "name": "助手"}).json()
    assert client.get(f"/api/v1/agents/{agent['id']}").json()["name"] == "助手"
    assert client.get("/api/v1/agents?workspace_id=ws_other").status_code == 404
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_auth.py apps/api/tests/integration/test_api_keys.py -v`
预期：FAIL，路由 404。

- [ ] **步骤 3：实现认证**

Cookie 用 `itsdangerous` 签名，`HttpOnly`、`SameSite=Lax`；CSRF 双提交 token 在登录响应体返回、写操作要求 `X-CSRF-Token`；API Key 明文格式 `mem_<prefix>_<secret>`，库存 Argon2id；scope 校验装饰器 `require_scope("read"|"write"|"admin")`；登录失败用 Redis 计数限流（5 次/分钟，锁 15 分钟）。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_auth.py apps/api/tests/integration/test_api_keys.py apps/api/tests/integration/test_workspaces.py -v`
预期：7 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/auth apps/api/app/api/auth.py apps/api/app/api/keys.py apps/api/app/errors.py apps/api/tests/integration
git commit -m "feat(api): admin session auth, scoped api keys and rate limiting"
```

---

### 任务 9：记忆写入管线与 outbox

**文件：**
- 创建：`apps/api/app/schemas/memory.py`、`apps/api/app/services/encoding.py`、`apps/api/app/services/memory_write.py`、`apps/api/app/api/memories.py`、`apps/api/tests/integration/test_memory_write.py`、`apps/api/tests/integration/test_memory_api.py`

**接口：**
- `MemoryWriteRequest`：`content, type, scope, scope_id, workspace_id, importance=None, entities=None, source=None, ttl_seconds=None, dedupe=True, idempotency_key=None`
- `normalize_content(raw: str, max_chars: int = 8192) -> str`（失败抛 `ValidationError`）
- `encode_memory(req, llm=None) -> EncodedMemory`（规范化、PII 标记、重要性、实体）
- `write_memory(db, req, actor) -> WriteResult`，其中 `WriteResult(id, action, superseded_ids, importance)`
- `POST /api/v1/memories`、`GET /memories`、`GET/PATCH/DELETE /memories/{id}`、`POST /memories/{id}/pin`、`POST /memories/merge`、`POST /memories/{id}/feedback`

- [ ] **步骤 1：编写失败的测试**

```python
def test_write_creates_memory_and_outbox_row(client, admin, db_session):
    r = client.post("/api/v1/memories", json=valid_write_payload(content="用户对花生过敏"))
    assert r.status_code == 201
    assert r.json()["action"] == "created"
    assert db_session.query(OutboxEvent).filter_by(aggregate_id=r.json()["id"]).count() == 1

def test_blank_content_returns_422(client, admin):
    r = client.post("/api/v1/memories", json=valid_write_payload(content="   \n  "))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["field"] == "content"

def test_rejects_content_over_8kb(client, admin):
    r = client.post("/api/v1/memories", json=valid_write_payload(content="字" * 8193))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "content_too_long"

def test_concurrent_same_idempotency_key_creates_one_memory(session_factory, admin):
    req = MemoryWriteRequest(**valid_write_payload(idempotency_key="idem-1"))
    ids = run_concurrently(lambda: write_memory(session_factory(), req, admin).id, workers=2)
    assert ids[0] == ids[1]
    with session_factory() as s:
        assert s.query(Memory).count() == 1

def test_contradiction_supersedes_and_keeps_history(client, admin):
    first = client.post("/api/v1/memories", json=valid_write_payload(content="用户住在北京")).json()
    second = client.post("/api/v1/memories", json=valid_write_payload(content="用户已搬到上海")).json()
    assert second["action"] == "superseded"
    old = client.get(f"/api/v1/memories/{first['id']}").json()
    assert old["status"] == "superseded" and old["superseded_by"] == second["id"]

def test_feedback_increases_importance_and_stability(client, admin, memory):
    before = client.get(f"/api/v1/memories/{memory.id}").json()
    client.post(f"/api/v1/memories/{memory.id}/feedback", json={"helpful": True})
    after = client.get(f"/api/v1/memories/{memory.id}").json()
    assert after["importance"] > before["importance"]
    assert after["stability"] > before["stability"]

def test_write_persists_entities_and_links(client, admin, db_session):
    payload = valid_write_payload(content="用户使用 Python", entities=[{"name": "Python", "type": "technology"}])
    client.post("/api/v1/memories", json=payload)
    assert db_session.query(Entity).filter_by(name="Python").count() == 1
    assert db_session.query(MemoryEntity).count() == 1

def test_global_scope_requires_admin_key(client, write_key):
    r = client.post("/api/v1/memories", headers={"Authorization": f"Bearer {write_key}"},
                    json=valid_write_payload(scope="global", scope_id=None))
    assert r.status_code == 403

def test_read_key_cannot_write(client, read_key):
    r = client.post("/api/v1/memories", headers={"Authorization": f"Bearer {read_key}"}, json=valid_write_payload())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_scope"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_memory_write.py apps/api/tests/integration/test_memory_api.py -v`
预期：FAIL，路由 404。

- [ ] **步骤 3：实现写入管线**

同一事务内写 `memory + audit_log + outbox_event`；`idempotency_key` 在 `(workspace_id, idempotency_key)` 上建唯一索引，冲突时返回已存在记录；去重候选用 `MemoryRepository.find_similar`，LLM 判定失败或未配置时退化为规则；`source_quote` 从 `source.quote` 截 200 字写入 `metadata`；PII 只做标记（`metadata.pii=true`），不改写内容。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_memory_write.py apps/api/tests/integration/test_memory_api.py -v`
预期：9 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/schemas apps/api/app/services apps/api/app/api/memories.py apps/api/tests/integration
git commit -m "feat(api): idempotent memory write pipeline with conflict handling"
```

---

### 任务 10：嵌入 Provider、投影 worker 与重建索引

**文件：**
- 创建：`apps/api/app/ports/protocols.py`、`apps/api/app/adapters/embeddings.py`、`apps/api/app/adapters/llm.py`、`apps/api/app/adapters/qdrant.py`、`apps/api/app/adapters/neo4j.py`、`apps/api/app/adapters/assets.py`、`apps/api/app/repositories/embedding.py`、`apps/api/app/jobs/celery_app.py`、`apps/api/app/jobs/tasks.py`、`apps/api/app/worker_cli.py`、`apps/api/tests/integration/test_projection.py`、`apps/api/tests/integration/test_embeddings.py`

**接口：**
- `EmbeddingProvider` 协议：`async embed(texts: list[str]) -> list[list[float]]`、`dim: int`、`name: str`
- `OpenAICompatibleEmbedding(base_url, api_key, model, dim)`、`LocalSentenceTransformerEmbedding(model, dim)`、`FakeEmbedding(dim=8)`
- `LLMProvider` 协议：`async complete(messages, json_schema=None) -> str`
- `Reranker` 协议：`async rerank(query, documents) -> list[float]`；M1 只提供 `NoopReranker`（返回原顺序），真实重排留到后续
- `QdrantIndex.upsert(memory_id, vector, payload)`、`search(vector, filters, limit)`、`get(memory_id)`、`count(workspace_id)`、`delete(memory_id)`
- `Neo4jIndex.upsert_memory(memory, entities)`、`get(memory_id)`、`delete(memory_id)`、`expand(entity_ids, hops=2, limit=30)`
- `EmbeddingRepository.upsert(memory_id, model, dim, vector)`、`get(memory_id, model) -> list[float] | None`
- Celery 任务：`dispatch_outbox(limit=50)`、`embed_memory(memory_id)`、`project_qdrant(memory_id, version)`、`project_neo4j(memory_id, version)`
- CLI：`python -m app.worker_cli rebuild --target qdrant|neo4j|all`

- [ ] **步骤 1：编写失败的测试**

```python
def test_projection_writes_qdrant_point(fake_embedding, qdrant_index, db_session, memory):
    project_qdrant.run(memory.id, memory.version)
    hit = qdrant_index.search([1.0] * 8, {"workspace_id": "ws1"}, limit=1)[0]
    assert hit.id == memory.id

def test_projection_retries_when_qdrant_down(db_session, memory, monkeypatch):
    monkeypatch.setattr(QdrantIndex, "upsert", raise_connection_error)
    with pytest.raises(Retry):
        project_qdrant.run(memory.id, memory.version)
    assert MemoryRepository(db_session).get("ws1", memory.id).status == "active"

def test_rebuild_recreates_missing_points(qdrant_index, db_session, memory):
    qdrant_index.delete(memory.id)
    run_rebuild("qdrant")
    assert qdrant_index.count("ws1") == 1

def test_fake_embedding_is_deterministic():
    p = FakeEmbedding(dim=8)
    assert p.embed_sync(["相同文本"]) == p.embed_sync(["相同文本"])

def test_outbox_dispatch_projects_memory(db_session, memory_with_outbox, qdrant_index, fake_embedding):
    dispatch_outbox()
    run_worker_once()
    assert qdrant_index.get(memory_with_outbox.id) is not None
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_projection.py apps/api/tests/integration/test_embeddings.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现 Provider 与投影**

Provider 通过工厂 `build_embedding_provider(settings)` 选择；`build_reranker(settings)` 在未配置真实模型时返回 `NoopReranker`；Qdrant collection 名 `mem_{provider}_{model}_{dim}`，payload 建索引；`dispatch_outbox` 每 5 秒领取 pending 事件并投递 Celery；`embed_memory` 把向量写入 `memory_embedding`，`project_qdrant` 缺嵌入时先补齐；投影成功后写 `projection_state(memory_id, target, version)`，版本不大于已投影版本则跳过；Celery 任务 `autoretry_for=(Exception,)`、`max_retries=8`、指数退避；`rebuild` 分页读取 Postgres 重放。Neo4j 用 `MERGE` 保证幂等。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_projection.py apps/api/tests/integration/test_embeddings.py -v`
预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/ports apps/api/app/adapters apps/api/app/repositories/embedding.py apps/api/app/jobs apps/api/app/worker_cli.py apps/api/tests/integration
git commit -m "feat(api): pluggable embeddings, outbox projection and rebuild CLI"
```

---

### 任务 11：四路召回检索服务

**文件：**
- 创建：`apps/api/app/schemas/search.py`、`apps/api/app/adapters/fts.py`、`apps/api/app/services/search.py`、`apps/api/tests/integration/test_search.py`

**接口：**
- `SearchRequest`：`query, types=None, scopes=None, workspace_id, agent_id=None, end_user_id=None, session_id=None, time_range=None, limit=8, mode="balanced", include_history=False, include_archived=False`
- `SearchResult(items: list[SearchItem], context_text: str, stats: SearchStats, degraded: list[str])`
- `search_memories(db, req, embedding_provider, qdrant, neo4j, llm=None, reranker=None) -> SearchResult`
- `procedural_bonus(metadata: dict) -> float`（`(success+1)/(success+failure+2)`，缺省 0.5）
- `POST /api/v1/search`

- [ ] **步骤 1：编写失败的测试**

```python
def test_search_fuses_vector_and_keyword(client, admin, seeded_memories, fake_embedding):
    r = client.post("/api/v1/search", json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u1"})
    assert r.status_code == 200
    assert r.json()["items"][0]["content"] == "用户对花生过敏"
    assert r.json()["items"][0]["why"]["vector_rank"] is not None

def test_search_degrades_when_vector_channel_unavailable(client, admin, seeded_memories, monkeypatch):
    monkeypatch.setattr(QdrantIndex, "search", raise_connection_error)
    r = client.post("/api/v1/search", json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u1"})
    assert r.status_code == 200
    assert "vector" in r.json()["degraded"]
    assert r.json()["items"]

def test_search_never_leaks_other_scope(client, admin, seeded_memories):
    r = client.post("/api/v1/search", json={"query": "花生过敏", "workspace_id": "ws1", "end_user_id": "u2"})
    assert all(i["scope_id"] != "u1" for i in r.json()["items"])

def test_expired_fact_is_hidden_by_default_and_shown_with_history(client, admin, seeded_memories):
    body = {"query": "用户住在哪里", "workspace_id": "ws1", "end_user_id": "u1"}
    assert "北京" not in [i["content"] for i in client.post("/api/v1/search", json=body).json()["items"]]
    body["include_history"] = True
    assert "北京" in [i["content"] for i in client.post("/api/v1/search", json=body).json()["items"]]

def test_search_rejects_empty_query(client, admin):
    assert client.post("/api/v1/search", json={"query": "  ", "workspace_id": "ws1"}).status_code == 422

def test_procedural_memory_ranks_by_success_rate(client, admin, seeded_procedural):
    items = client.post("/api/v1/search", json={"query": "Excel 转图表", "workspace_id": "ws1", "types": ["procedural"]}).json()["items"]
    assert items[0]["content"].startswith("高成功率")
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_search.py -v`
预期：FAIL，路由 404。

- [ ] **步骤 3：实现四路检索与融合**

四路并行（`asyncio.gather`，同步适配器包 `run_in_threadpool`），每路 top 50；`degraded` 记录失败通道；默认过滤 `status=active` 且 `valid_to is null or valid_to > now()`；`deep` 走 2 跳与重排，重排失败静默跳过；RRF 后用 `final_score` 排序，程序性记忆再乘 `procedural_bonus`；中文全文检索优先 `zhparser`，迁移中不可用时用 `simple` + 应用层空格分词；M1 查询改写默认关闭，配置开启后才调用 LLM。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_search.py -v`
预期：6 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/schemas/search.py apps/api/app/adapters/fts.py apps/api/app/services/search.py apps/api/app/api/search.py apps/api/tests/integration
git commit -m "feat(api): hybrid retrieval with rrf fusion and graceful degradation"
```

---

### 任务 12：记忆 REST 资源与权限边界

**文件：**
- 修改：`apps/api/app/api/memories.py`、`apps/api/app/api/search.py`、`apps/api/app/main.py`
- 创建：`apps/api/app/services/stats.py`、`apps/api/app/api/stats.py`、`apps/api/tests/integration/test_memory_permissions.py`

**接口：**
- `GET /api/v1/memories?cursor=&limit=&type=&scope=&status=&min_importance=&q=`
- `GET /api/v1/memories/{id}` 返回正文、关联与 `history`（被取代链）
- `PATCH /api/v1/memories/{id}` 支持 `content/importance/tags/valid_to`
- `DELETE /api/v1/memories/{id}?hard=false&confirm=false`
- `POST /api/v1/memories/{id}/feedback`
- `POST /api/v1/memories/merge`（body：`source_ids[], target_id`）
- `GET /api/v1/timeline?session_id=|entity=|from=&to=`
- `GET /api/v1/stats/overview`、`GET /api/v1/stats/timeseries?days=30`
- `apps/api/app/services/stats.py`：`overview(db, workspace_id) -> Overview`、`timeseries(db, workspace_id, days) -> list[Point]`

- [ ] **步骤 1：编写失败的测试**

```python
def test_get_memory_from_other_workspace_returns_404(client, admin, other_workspace_memory):
    r = client.get(f"/api/v1/memories/{other_workspace_memory.id}")
    assert r.status_code == 404

def test_patch_other_workspace_memory_returns_404(client, admin, other_workspace_memory):
    assert client.patch(f"/api/v1/memories/{other_workspace_memory.id}", json={"importance": 1}).status_code == 404

def test_hard_delete_requires_confirm(client, admin, memory):
    assert client.delete(f"/api/v1/memories/{memory.id}?hard=true").status_code == 400
    assert client.delete(f"/api/v1/memories/{memory.id}?hard=true&confirm=true").status_code == 200

def test_control_characters_roundtrip(client, admin):
    content = "用户说\x00\x07\x1b[31m红色\x1b[0m"
    r = client.post("/api/v1/memories", json=valid_write_payload(content=content))
    assert r.status_code == 201
    assert client.get(f"/api/v1/memories/{r.json()['id']}").json()["content"] == content

def test_pagination_is_stable(client, admin, seeded_memories):
    page1 = client.get("/api/v1/memories?limit=5").json()
    page2 = client.get(f"/api/v1/memories?limit=5&cursor={page1['next_cursor']}").json()
    assert not ({m["id"] for m in page1["items"]} & {m["id"] for m in page2["items"]})

def test_detail_returns_superseded_history(client, admin, superseded_pair):
    old_id, new_id = superseded_pair
    history = client.get(f"/api/v1/memories/{new_id}").json()["history"]
    assert history[0]["id"] == old_id and history[0]["status"] == "superseded"

def test_merge_moves_links_and_archives_sources(client, admin, mergeable_pair):
    a, b = mergeable_pair
    r = client.post("/api/v1/memories/merge", json={"source_ids": [a.id], "target_id": b.id})
    assert r.status_code == 200
    assert client.get(f"/api/v1/memories/{a.id}").json()["status"] == "archived"

def test_forget_by_query_soft_deletes_matches(client, admin, seeded_memories):
    r = client.request("DELETE", "/api/v1/memories", json={"query": "花生过敏", "scope": "user", "hard": False})
    assert r.status_code == 200 and r.json()["affected"] >= 1

def test_stats_overview_returns_counts(client, admin, seeded_memories):
    body = client.get("/api/v1/stats/overview").json()
    assert body["total_memories"] >= 1
    assert "by_type" in body and "pending_review" in body

def test_timeline_is_time_ordered(client, admin, seeded_memories):
    items = client.get("/api/v1/timeline?session_id=s1").json()["items"]
    assert [i["timestamp"] for i in items] == sorted(i["timestamp"] for i in items)
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_memory_permissions.py -v`
预期：FAIL，404 断言失败或缺少 cursor 参数。

- [ ] **步骤 3：实现权限边界与分页**

所有读写先按 `workspace_id` 取，再判断 scope；不存在与无权统一 404；分页用 `(created_at, id)` 复合游标，`next_cursor` 为 base64；硬删除要求 `confirm=true` 且 admin scope，否则 400 `confirmation_required`；`PATCH` 写审计；静态路由（`/memories/merge`、`/timeline`）必须声明在 `/{id}` 之前。合并时把 source 的 `memory_link` 迁到 target、source 置 `archived` 并记录 `metadata.merged_into`；`history` 沿 `superseded_by` 反向收集一条链；timeline 合并 `episode` 与带 session 的记忆按时间升序。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_memory_permissions.py -v`
预期：10 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/api apps/api/tests/integration/test_memory_permissions.py
git commit -m "feat(api): workspace-safe memory resources and stable pagination"
```

---

### 任务 13：MCP 服务与 stdio 客户端

**文件：**
- 创建：`apps/api/app/mcp/server.py`、`apps/api/app/mcp/tools.py`、`apps/api/app/mcp/resources.py`、`packages/mcp-stdio/pyproject.toml`、`packages/mcp-stdio/memory_mcp/__main__.py`、`apps/api/tests/contract/test_mcp_tools.py`

**前置：** 本任务依赖任务 14（抽取）与任务 15（巩固），在二者完成后执行，避免工具指向未实现的服务。

**接口：**
- `build_mcp_server(services) -> FastMCP`，挂载到 `/mcp`
- 10 个工具：`memory_search, memory_write, memory_get, memory_update, memory_forget, memory_feedback, memory_consolidate, memory_stats, memory_timeline, conversation_ingest`
- Resources：`memory://{id}`、`memory://recent`、`memory://entities/{name}`
- Prompt：`memory_aware_system`
- stdio：`memory-mcp --api-url ... --api-key ...`

- [ ] **步骤 1：编写失败的测试**

```python
@pytest.mark.parametrize("tool", TOOL_NAMES)
async def test_tool_is_listed(mcp_client, tool):
    tools = {t.name for t in await mcp_client.list_tools()}
    assert tool in tools

async def test_write_then_search_via_mcp(mcp_client):
    await mcp_client.call_tool("memory_write", {"content": "用户对花生过敏", "type": "semantic", "scope": "user", "scope_id": "u1", "workspace_id": "ws1"})
    result = await mcp_client.call_tool("memory_search", {"query": "花生", "workspace_id": "ws1", "end_user_id": "u1"})
    assert "花生" in result.structuredContent["items"][0]["content"]

async def test_invalid_key_returns_32001(mcp_client_bad_key):
    with pytest.raises(McpError) as exc:
        await mcp_client_bad_key.call_tool("memory_stats", {})
    assert exc.value.code == -32001

async def test_forget_requires_confirm(mcp_client):
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool("memory_forget", {"id": "m1", "hard": True, "confirm": False})
    assert exc.value.code == -32005

async def test_memory_prompt_is_listed(mcp_client):
    names = {p.name for p in await mcp_client.list_prompts()}
    assert "memory_aware_system" in names

async def test_write_rate_limit_returns_32004(mcp_client):
    for i in range(31):
        await mcp_client.call_tool("memory_write", {"content": f"内容{i}", "type": "semantic", "scope": "user", "scope_id": "u1", "workspace_id": "ws1"})
    with pytest.raises(McpError) as exc:
        await mcp_client.call_tool("memory_write", {"content": "超限", "type": "semantic", "scope": "user", "scope_id": "u1", "workspace_id": "ws1"})
    assert exc.value.code == -32004
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/contract/test_mcp_tools.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现 MCP 适配层**

工具只做参数校验、权限映射与服务调用，不含业务逻辑；错误统一映射到 `-32001`~`-32006`；stdio 客户端用 `httpx` 转发 HTTP，透传 Key 与 trace id；`memory_search` 返回 `items/context_text/stats/degraded`；`memory_timeline` 复用任务 12 的 timeline 服务，`memory_consolidate` 复用任务 15 的巩固服务；测试环境把写限流设为 30 次/分钟。同时实现 3 个 Resources（Markdown 视图，单条截断 4 KB）与 `memory_aware_system` Prompt。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/contract/test_mcp_tools.py -v`
预期：全部 PASS（含参数化 10 个工具与限流用例）。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/mcp packages/mcp-stdio apps/api/tests/contract
git commit -m "feat(mcp): ten memory tools, resources, prompt and stdio bridge"
```

---

### 任务 14：对话抽取与待审队列

**文件：**
- 创建：`apps/api/app/services/extraction.py`、`apps/api/app/services/review.py`、`apps/api/app/api/review.py`、`apps/api/tests/integration/test_extraction.py`、`apps/api/tests/integration/test_review.py`

**接口：**
- `ingest_conversation(db, req, actor) -> IngestResult(task_id, candidate_count)`
- `extract_candidates(messages, llm) -> list[Candidate]`，`Candidate(content, type, importance, confidence, evidence, entities)`
- 测试用 `FakeLLM`：`set_json(obj)`、`set_text(str)`，并记录调用次数
- `accept_candidate(db, candidate, actor) -> WriteResult`
- `GET /api/v1/review`、`POST /review/{id}/approve`、`POST /review/{id}/reject`、`POST /review/bulk`

- [ ] **步骤 1：编写失败的测试**

```python
def test_high_confidence_candidate_is_auto_accepted(db_session, fake_llm):
    fake_llm.set_json([{"content": "用户对花生过敏", "type": "semantic", "importance": 0.9, "confidence": 0.9, "evidence": "我吃花生会过敏"}])
    ingest_conversation(db_session, ingest_payload("我吃花生会过敏"), admin)
    run_worker_once()
    assert db_session.query(Memory).filter_by(content="用户对花生过敏").count() == 1
    assert db_session.query(ReviewItem).count() == 0

def test_low_confidence_goes_to_review(db_session, fake_llm):
    fake_llm.set_json([{"content": "用户可能喜欢猫", "type": "semantic", "importance": 0.4, "confidence": 0.5, "evidence": "也许吧"}])
    ingest_conversation(db_session, ingest_payload("也许吧"), admin)
    run_worker_once()
    assert db_session.query(ReviewItem).count() == 1

def test_approve_creates_memory_and_marks_reviewed(client, admin, pending_review):
    r = client.post(f"/api/v1/review/{pending_review.id}/approve", json={"content": "用户对花生过敏"})
    assert r.status_code == 200
    assert r.json()["memory_id"]
    assert client.get("/api/v1/review").json()["items"] == []

def test_ingest_is_idempotent_for_same_messages(db_session, fake_llm, admin):
    payload = ingest_payload("我吃花生会过敏")
    a = ingest_conversation(db_session, payload, admin)
    b = ingest_conversation(db_session, payload, admin)
    assert a.task_id == b.task_id
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_extraction.py apps/api/tests/integration/test_review.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现抽取与待审**

幂等键为 `sha256(session_id + 归一化消息 JSON)`；LLM 提示要求输出 JSON 数组并按 schema 校验，解析失败进死信并审计；候选先去重（与既有记忆和同批候选），再按阈值分流；批准时允许编辑内容与重要性，拒绝写审计；待审项保留 90 天后清理。测试用 `run_worker_once()` 直接调用 Celery task 函数（不经 broker），定义在 `tests/integration/conftest.py`。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_extraction.py apps/api/tests/integration/test_review.py -v`
预期：4 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/services/extraction.py apps/api/app/services/review.py apps/api/app/api/review.py apps/api/tests/integration
git commit -m "feat(api): conversation extraction and human review queue"
```

---

### 任务 15：巩固任务、dry-run 与回滚

**文件：**
- 创建：`apps/api/app/services/consolidation.py`、`apps/api/app/api/jobs.py`、`apps/api/tests/integration/test_consolidation.py`

**接口：**
- `consolidate_session(db, session_id, dry_run=False) -> ConsolidationDiff`
- `consolidate_nightly(db, window_start, window_end, scope=None, dry_run=False) -> ConsolidationDiff`
- `rollback_run(db, job_run_id) -> int`
- `GET /api/v1/jobs`、`GET /jobs/{id}`、`POST /jobs/run`（body：`{type, params, dry_run}`）、`POST /jobs/{id}/rollback`、`GET /jobs/{id}/events`（SSE）

- [ ] **步骤 1：编写失败的测试**

```python
def test_session_consolidation_creates_episode_and_facts(db_session, session_with_working_memories, fake_llm):
    fake_llm.set_json({"summary": "用户讨论了过敏问题", "facts": ["用户对花生过敏"]})
    diff = consolidate_session(db_session, "s1")
    assert diff.created_episodes == 1
    assert diff.created_memories >= 1
    assert db_session.query(Episode).count() == 1

def test_dry_run_changes_nothing(db_session, session_with_working_memories, fake_llm):
    before = snapshot_counts(db_session)
    diff = consolidate_session(db_session, "s1", dry_run=True)
    assert diff.created_memories >= 1
    assert snapshot_counts(db_session) == before

def test_consolidation_is_idempotent(db_session, session_with_working_memories, fake_llm):
    first = consolidate_session(db_session, "s1")
    second = consolidate_session(db_session, "s1")
    assert second.created_memories == 0 and first.job_run_id == second.job_run_id

def test_rollback_restores_previous_state(db_session, session_with_working_memories, fake_llm):
    diff = consolidate_session(db_session, "s1")
    assert rollback_run(db_session, diff.job_run_id) == diff.created_memories
    assert db_session.query(Memory).filter(Memory.metadata_["consolidation_run_id"].astext == diff.job_run_id).count() == 0

def test_rollback_restores_merged_memory_from_audit(db_session, mergeable_memories, fake_llm):
    diff = consolidate_session(db_session, "s1")
    rollback_run(db_session, diff.job_run_id)
    assert db_session.query(Memory).filter_by(id="m-merged").one().content == "原始内容"
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_consolidation.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现巩固**

聚类用“同实体或嵌入相似度 ≥ 0.82”，簇大小 ≥ 3 才在夜间任务处理；每条新建/修改记忆写 `metadata.consolidation_run_id`；`job_run` 记录 `params/result`；回滚按 run id 反向操作，合并用 `audit_log.before` 恢复；重复调用按幂等键返回同一 `job_run`；SSE 用 `sse-starlette` 从 Redis pub/sub 转发任务进度，连接断开不影响任务。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_consolidation.py -v`
预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/services/consolidation.py apps/api/app/api/jobs.py apps/api/tests/integration/test_consolidation.py
git commit -m "feat(api): consolidation with dry-run, idempotency and rollback"
```

---

### 任务 16：遗忘扫描、清理、对账与导出

**文件：**
- 修改：`apps/api/app/api/jobs.py`、`apps/api/app/main.py`
- 创建：`apps/api/app/services/lifecycle.py`、`apps/api/app/services/export.py`、`apps/api/app/api/export.py`、`apps/api/app/jobs/schedules.py`、`apps/api/tests/integration/test_lifecycle.py`、`apps/api/tests/integration/test_export.py`

**接口：**
- `decay_sweep(db, now, params) -> int`
- `purge_deleted(db, now, retention_days=30) -> int`
- `prune_ops_history(db, now, audit_days=180, review_days=90) -> int`
- `reconcile_indexes(db, qdrant, neo4j, sample=100) -> ReconcileReport`
- `export_workspace(db, workspace_id, fmt: Literal["json", "md"]) -> Path`
- REST：`POST /api/v1/jobs/run`（`type="export"`）、`GET /jobs/{id}`

- [ ] **步骤 1：编写失败的测试**

```python
def test_decay_sweep_archives_only_low_retention(db_session, old_memories):
    assert decay_sweep(db_session, NOW, DecayParams()) == 1
    assert db_session.get(Memory, "low").status == "archived"
    assert db_session.get(Memory, "high").status == "active"

def test_purge_removes_only_soft_deleted_older_than_30_days(db_session, deleted_memories):
    assert purge_deleted(db_session, NOW) == 1
    assert db_session.get(Memory, "old").status == "purged"

def test_hard_delete_cascades_to_indexes_and_attached_files(client, admin, memory_with_attached_file, qdrant_index, neo4j_index):
    paths = [Path(p) for p in memory_with_attached_file.metadata_["asset_paths"]]
    client.delete(f"/api/v1/memories/{memory_with_attached_file.id}?hard=true&confirm=true")
    assert qdrant_index.get(memory_with_attached_file.id) is None
    assert neo4j_index.get(memory_with_attached_file.id) is None
    assert all(not p.exists() for p in paths)

def test_reconcile_reports_and_repairs_missing_points(db_session, memory, qdrant_index):
    qdrant_index.delete(memory.id)
    report = reconcile_indexes(db_session, qdrant_index, None, sample=10)
    assert report.repaired == 1 and report.missing_after == 0

def test_export_contains_memories_and_audit(client, admin, seeded_memories):
    job_id = client.post("/api/v1/jobs/run", json={"type": "export", "params": {"format": "json"}}).json()["job_id"]
    run_worker_once()
    content = read_export(job_id)
    assert "memory" in content and "audit" in content
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/integration/test_lifecycle.py apps/api/tests/integration/test_export.py -v`
预期：FAIL，`ImportError`。

- [ ] **步骤 3：实现生命周期任务**

`schedules.py` 注册每日 03:00 巩固、04:00 遗忘、05:00 清理（软删记忆、审计 180 天、待审 90 天）、每小时对账；归档只改状态并写 `metadata.archived_reason`；硬删除先置 `purged` 并写 outbox，worker 删索引与 `metadata.asset_paths` 指向的文件（M3 上线后由资产表接管），对账兜底；导出流式写临时文件，JSON 含 `workspace/memories/episodes/audit`，Markdown 按类型分节。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/integration/test_lifecycle.py apps/api/tests/integration/test_export.py -v`
预期：5 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/services/lifecycle.py apps/api/app/services/export.py apps/api/app/api/export.py apps/api/app/jobs/schedules.py apps/api/tests/integration
git commit -m "feat(api): decay sweep, purge cascade, reconcile and export"
```

---

### 任务 17：Web 骨架、首次向导与概览

**文件：**
- 创建：`apps/web/package.json`、`vite.config.ts`、`tsconfig.json`、`tailwind.config.ts`、`playwright.config.ts`、`src/main.tsx`、`src/app/router.tsx`、`src/app/query.ts`、`src/lib/api.ts`、`src/lib/auth.tsx`、`src/components/layout/AppShell.tsx`、`src/pages/SetupPage.tsx`、`src/pages/LoginPage.tsx`、`src/pages/DashboardPage.tsx`、`e2e/helpers.ts`、`e2e/setup-login.spec.ts`、`e2e/dashboard.spec.ts`

**接口：**
- `api<T>(path, init) -> Promise<T>`：自动带 Cookie、CSRF、统一错误 `ApiError{code,message,details}`
- 路由：`/setup`、`/login`、`/`（概览）、`/memories`、`/review`、`/jobs`、`/settings`、`/playground`
- `useSession()`：`{admin, loading, refresh}`

- [ ] **步骤 1：编写失败的测试**

```typescript
// e2e/setup-login.spec.ts
test('首次启动完成向导并可登录', async ({ page }) => {
  await page.goto('/setup');
  await page.getByLabel('管理员密码').fill('p@ssw0rd-123');
  await page.getByLabel('确认密码').fill('p@ssw0rd-123');
  await page.getByRole('button', { name: '创建管理员' }).click();
  await expect(page).toHaveURL('/');
  await expect(page.getByRole('heading', { name: '概览' })).toBeVisible();
});

// e2e/dashboard.spec.ts
test('概览展示记忆计数', async ({ page }) => {
  await loginAsAdmin(page);
  await expect(page.getByTestId('stat-total-memories')).toHaveText(/\d+/);
  await expect(page.getByTestId('stat-pending-review')).toBeVisible();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pnpm --dir apps/web exec playwright test e2e/setup-login.spec.ts`
预期：FAIL，`Cannot find module '@playwright/test'` 或页面 404。

- [ ] **步骤 3：搭建前端**

Vite + React + TS + Tailwind + shadcn/ui；`playwright.config.ts` 的 `webServer` 只启动 Vite，E2E 前置为 `make up`，测试数据由 `e2e/helpers.ts` 通过 API 现场创建（不依赖任务 20 的种子脚本）；E2E 使用独立测试库，`globalSetup` 执行迁移并清空管理员，首次向导用例串行执行；`AppShell` 左侧导航；`SetupPage` 调 `POST /auth/setup`；登录后 `useSession` 拉取 `/auth/me`；Dashboard 调 `/stats/overview` 显示总量/类型分布/待审数/任务健康（元素带 `data-testid`）；`e2e/helpers.ts` 提供 `loginAsAdmin`、`createMemoryViaApi`、`seedPendingReview`、`seedMemories`；Nginx 配置 `try_files $uri /index.html`，反代 `/api` 与 `/mcp`。

- [ ] **步骤 4：验证通过**

运行：`pnpm --dir apps/web test && pnpm --dir apps/web exec playwright test e2e/setup-login.spec.ts e2e/dashboard.spec.ts`
预期：单元测试与 2 个 E2E 全 PASS。

- [ ] **步骤 5：Commit**

```bash
git add apps/web
git commit -m "feat(web): app shell, first-run wizard, login and dashboard"
```

---

### 任务 18：记忆浏览器、详情与待审队列

**文件：**
- 创建：`apps/web/src/pages/MemoriesPage.tsx`、`src/pages/MemoryDetailPage.tsx`、`src/pages/ReviewPage.tsx`、`src/components/memory/MemoryTable.tsx`、`src/components/memory/MemoryFilters.tsx`、`src/components/memory/MemoryDetailDrawer.tsx`、`src/components/review/ReviewList.tsx`、`e2e/memories.spec.ts`、`e2e/review.spec.ts`

**接口：**
- 列表：`GET /memories` 参数 `q,type,scope,status,min_importance,cursor,limit`
- 详情：`GET /memories/{id}`，展示版本链、审计、来源原话
- 操作：`PATCH /memories/{id}`、`POST /memories/{id}/pin`、`DELETE /memories/{id}`
- 待审：`GET /review`、`POST /review/{id}/approve|reject`

- [ ] **步骤 1：编写失败的测试**

```typescript
test('可以搜索并编辑记忆', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/memories');
  await page.getByPlaceholder('搜索记忆').fill('花生');
  await expect(page.getByText('用户对花生过敏')).toBeVisible();
  await page.getByText('用户对花生过敏').click();
  await page.getByLabel('重要性').fill('0.9');
  await page.getByRole('button', { name: '保存' }).click();
  await expect(page.getByText('已保存')).toBeVisible();
});

test('test_memory_content_is_rendered_as_text', async ({ page }) => {
  await createMemoryViaApi('<img src=x onerror="window.__xss=1">');
  await page.goto('/memories');
  await expect(page.getByText('<img src=x')).toBeVisible();
  expect(await page.evaluate(() => (window as any).__xss)).toBeUndefined();
});

test('待审候选可通过并生成记忆', async ({ page }) => {
  await seedPendingReview();
  await page.goto('/review');
  await page.getByRole('button', { name: '通过' }).click();
  await expect(page.getByText('待审队列为空')).toBeVisible();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pnpm --dir apps/web exec playwright test e2e/memories.spec.ts e2e/review.spec.ts`
预期：FAIL，页面 404。

- [ ] **步骤 3：实现页面**

表格支持服务端分页与筛选；详情抽屉展示正文、类型、作用域、重要性、有效期、来源原话、关联、审计；编辑用受控表单并做 8 KB 校验；删除二次确认；待审页支持编辑后通过、拒绝与批量；**所有记忆文本用 React 文本节点渲染，禁止 `dangerouslySetInnerHTML`**。

- [ ] **步骤 4：验证通过**

运行：`pnpm --dir apps/web exec playwright test e2e/memories.spec.ts e2e/review.spec.ts`
预期：3 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/web
git commit -m "feat(web): memory explorer, detail drawer and review queue"
```

---

### 任务 19：任务中心、设置与检索调试台

**文件：**
- 创建：`apps/web/src/pages/JobsPage.tsx`、`src/pages/SettingsPage.tsx`、`src/pages/PlaygroundPage.tsx`、`src/components/jobs/JobTable.tsx`、`src/components/jobs/DryRunDiff.tsx`、`src/components/settings/ProviderForm.tsx`、`src/components/settings/KeyManager.tsx`、`src/components/playground/SearchInspector.tsx`、`e2e/jobs.spec.ts`、`e2e/settings.spec.ts`、`e2e/playground.spec.ts`

**接口：**
- `GET /jobs`、`POST /jobs/run`（`type`、`params`、`dry_run`）、`POST /jobs/{id}/rollback`、`GET /jobs/{id}/events`（SSE）
- `GET/POST/PATCH/DELETE /providers`、`POST /providers/{id}/test`
- `GET/POST /keys`、`DELETE /keys/{id}`
- `POST /search`（返回 `why` 与 `degraded`）
- 后端新增：`apps/api/app/api/providers.py`、`apps/api/app/schemas/ops.py`

- [ ] **步骤 1：编写失败的测试**

```typescript
test('巩固任务可 dry-run 并回滚', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/jobs');
  await page.getByRole('button', { name: '巩固 dry-run' }).click();
  await expect(page.getByTestId('dry-run-diff')).toContainText('将新增');
});

test('Provider 连通性测试给出结果', async ({ page }) => {
  await loginAsAdmin(page);
  await page.goto('/settings');
  await page.getByLabel('Base URL').fill('http://127.0.0.1:9');
  await page.getByRole('button', { name: '测试连接' }).click();
  await expect(page.getByText('连接失败')).toBeVisible();
});

test('调试台展示分路召回得分', async ({ page }) => {
  await loginAsAdmin(page);
  await seedMemories();
  await page.goto('/playground');
  await page.getByPlaceholder('输入检索语句').fill('花生过敏');
  await page.getByRole('button', { name: '检索' }).click();
  await expect(page.getByTestId('channel-vector')).toBeVisible();
  await expect(page.getByTestId('channel-bm25')).toBeVisible();
});
```

- [ ] **步骤 2：运行测试验证失败**

运行：`pnpm --dir apps/web exec playwright test e2e/jobs.spec.ts e2e/settings.spec.ts e2e/playground.spec.ts`
预期：FAIL，页面 404。

- [ ] **步骤 3：实现页面与后端接口**

任务中心用 SSE 订阅进度，支持取消；设置页 Provider 密钥不回显，只允许覆盖；API Key 明文仅创建时展示一次并提示复制；调试台并排展示四路排名、RRF 分数、最终上下文与耗时，命中 `degraded` 时显示黄色告警。

- [ ] **步骤 4：验证通过**

运行：`pnpm --dir apps/web exec playwright test e2e/jobs.spec.ts e2e/settings.spec.ts e2e/playground.spec.ts`
预期：3 passed。

- [ ] **步骤 5：Commit**

```bash
git add apps/web apps/api/app/api/providers.py apps/api/app/schemas/ops.py
git commit -m "feat: job center, provider settings and retrieval playground"
```

---

### 任务 20：种子数据、黄金集评测、性能与运行手册

**文件：**
- 创建：`apps/api/app/seed.py`、`apps/api/tests/fixtures/golden/memories.json`、`apps/api/tests/fixtures/golden/queries.json`、`apps/api/tests/eval/run_eval.py`、`apps/api/tests/perf/test_search_latency.py`、`infra/scripts/backup.sh`、`infra/scripts/restore.sh`、`README.md`、`docs/runbook.md`

**接口：**
- `python -m app.seed --workspace ws_demo`：创建 200 条记忆、5 个 Agent、3 个用户、20 条待审
- `python apps/api/tests/eval/run_eval.py --provider fake|real`：输出 `recall@5, mrr, stale_fact_rate, contradiction_accuracy, avg_tokens, p95_ms`
- `pytest apps/api/tests/perf -m perf`：1 万条记忆下 p95 < 300ms
- `bash infra/scripts/backup.sh`：生成 `backups/<timestamp>/{pg.dump,assets.tar.gz,manifest.json}`，并更新 `backups/latest` 指向最新一次

- [ ] **步骤 1：编写失败的测试**

```python
def test_golden_set_has_required_coverage():
    queries = load_queries()
    categories = {q["category"] for q in queries}
    assert {"single_hop", "multi_hop", "temporal", "contradiction", "negation", "scope", "procedural"} <= categories
    assert len(load_memories()) >= 200 and len(queries) >= 100

def test_eval_meets_m1_baseline():
    report = run_eval(provider="fake")
    assert report.recall_at_5 >= 0.80
    assert report.stale_fact_rate <= 0.05

@pytest.mark.perf
def test_search_p95_under_300ms(seeded_10k):
    report = measure_search(iterations=100)
    assert report.p95_ms < 300
```

- [ ] **步骤 2：运行测试验证失败**

运行：`uv run pytest apps/api/tests/eval apps/api/tests/perf -v`
预期：FAIL，文件不存在。

- [ ] **步骤 3：构造数据与评测**

黄金集覆盖 7 类查询，其中矛盾类必须包含“北京→上海”与否定类“不吃辣”；`fake` provider 用确定性哈希嵌入保证 CI 稳定，`real` 模式读取环境变量调用真实模型；`seeded_10k` 与 `measure_search` fixture 定义在 `apps/api/tests/perf/conftest.py`；评测报告写 `artifacts/eval-{timestamp}.json`；备份脚本用 `pg_dump -Fc` + `tar`，恢复脚本先校验 manifest 再执行，并在 `runbook.md` 写明“恢复后必须 `rebuild-index`”；`runbook.md` 另写明启动、重建索引、常见故障（Qdrant/Neo4j 不可用、迁移失败、磁盘满）；README 给出 5 分钟接入步骤。

- [ ] **步骤 4：验证通过**

运行：`uv run pytest apps/api/tests/eval -v && uv run pytest apps/api/tests/perf -m perf -v && bash infra/scripts/backup.sh && test -f backups/latest/manifest.json`
预期：黄金集、性能测试与备份均成功（性能测试允许在 CI 外手动运行）。

- [ ] **步骤 5：Commit**

```bash
git add apps/api/app/seed.py apps/api/tests infra/scripts README.md docs/runbook.md
git commit -m "test: golden-set evaluation, perf baseline and runbook"
```

---

## 执行顺序与检查点

- 执行顺序：1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 11 → 12 → 14 → 15 → 16 → 13 → 17 → 18 → 19 → 20。任务 13（MCP）放在 14/15/16 之后，确保 10 个工具都有真实服务可调用。
- 任务 7 起依赖数据库与容器；没有 Docker 时只跑 `apps/api/tests/unit` 与 `packages/memory-core/tests`。
- 每个任务完成后运行全量 `uv run pytest`，不允许把失败留给下一个任务。
- 每完成 5 个任务做一次人工检查点：跑 `docker compose up` + 冒烟脚本，确认端到端仍可用。
- M1 完成定义：任务 1–20 全部勾选，且规格第 15 节 M1 的 8 条验收全部有对应自动化测试或可复现脚本。
