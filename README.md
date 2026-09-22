# knowledge-brain

基于 RAG、支持 Obsidian 的知识库底座。

自动把 Obsidian vault 同步到云端、建立混合检索能力，供 AI 客户端（Claude Code / Claude Desktop）通过 MCP 查询。
**只返回带出处的原文片段，由调用方的 LLM 自己组织答案。**

- 设计文档：`docs/superpowers/specs/2026-09-22-obsidian-rag-knowledge-base-design.md`
- 实施计划：`docs/plan/2026-09-22-implementation-plan.md`

---

## 架构

```
Obsidian (本地) ──obsidian-git push──> 私有 Git 仓库
                                          │ sync worker（轮询 + diff）
手动上传 ─────────────────────────────────┤
                                          ▼
                                     converter（按 content_sha 缓存）
                                          │
                                          ▼
                                      indexer（分块 → embedding）
                                          │
                                          ▼
                                   Postgres（pgvector + tsvector + RLS）
                                          ▲
                                          │ retrieval（RRF 混合检索，宽召回）
                                          │
                                    /mcp endpoint
                                          │
                                Claude Code / Claude Desktop
```

五个组件严格单一职责：`sync worker` / `upload API` / `converter` / `indexer` / `retrieval`。

---

## 快速开始

### 前置要求

- Python **3.12+**
- [uv](https://docs.astral.sh/uv/)
- Docker（用于带 `pgvector` + `zhparser` 的 Postgres）

### 1. 安装依赖

```bash
uv sync --extra dev
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env：填 EMBEDDING_API_KEY；数据库三项默认值可直接用于本地
```

### 3. 构建并起数据库

`pgvector` 与 `zhparser` 是数据库扩展，**无法事后安装**——必须在构建镜像时编译
（`zhparser` 没有 Debian 包，其分词库 SCWS 连源码包都没有）。源码 tarball 已 vendor 在
`docker/vendor/`，因此构建过程不需要访问 GitHub。

```bash
docker compose build postgres   # 首次约 2~5 分钟，在编译 zhparser
docker compose up -d postgres
```

镜像在首次初始化时会创建：两个扩展、`chinese` 全文检索配置、以及**无特权的 `kb_app` 角色**。

> ⚠️ **应用必须用 `kb_app` 连接。** RLS 对 superuser 恒不生效，对表 owner 默认也不生效；
> 若 `DATABASE_URL` 指向 owner 角色，租户隔离会被静默关掉——这是 spec §11.1 的第一条红线。
> 配置层会拦住这种情况：`DATABASE_URL` 与 `DATABASE_URL_ADMIN` 角色名相同即启动报错。

验证中文分词确实生效（不生效时 `to_tsvector` 会把整句当成一个 token）：

```bash
docker compose exec postgres psql -U kb -d kb -c "SELECT to_tsvector('chinese', '防止任务重复执行');"
```

### 4. 跑迁移

```bash
uv run alembic upgrade head
```

迁移包含全部表、HNSW / GIN 索引，以及租户表的 RLS policy。

### 5. 起服务

```bash
# API + MCP
uv run uvicorn kb.main:app --host 0.0.0.0 --port 8000

# Worker（另开一个终端）
uv run python -m kb.worker
```

---

## 开发

### 代码检查

```bash
uv run ruff check src tests
uv run ruff format src tests
```

### 测试

```bash
# 单元测试（无需数据库）：配置校验、结构性守卫等
uv run pytest tests/unit

# 集成测试（需真实 Postgres + pgvector + zhparser）
uv run pytest tests/integration
```

> `pgvector` 和 `zhparser` 是数据库扩展，**mock 不了**。集成测试必须连真实 PG。
> 集成测试用 testcontainers 起一个 `kb-postgres:local` 容器，所以先执行
> `docker compose build postgres`；Docker 不可用时它们会 **skip 并给出提示**，
> 而不是静默通过——跳过和通过必须能区分开。

### 租户隔离：两层闸门

spec §5 ② 要求应用层与数据库层各挡一道，任一层失效都不泄漏：

| 层 | 实现 | 测试 |
|---|---|---|
| 应用层 | `kb/retrieval/query_builder.py` 是租户范围 SQL 的唯一入口 | `test_query_builder_scopes_even_without_rls`（在 owner 连接上跑，RLS 关掉） |
| 数据库层 | 迁移中的 RLS policy，读 `current_setting('app.user_id')` | `test_rls_hides_other_tenants_*_from_raw_sql`（绕过 query builder 直接发原生 SQL） |

第三道是**结构性守卫**：`tests/unit/test_tenant_scoping_guard.py` 扫描 `src/kb`，
一旦在 `query_builder.py` 之外发现手写的 `select(Chunk)` 或触碰这三张表的 SQL 字面量就失败。
"记得加 WHERE" 是约定，约定会腐化；这个检查不会。

`app.user_id` 用 `set_config(..., is_local => true)` 注入，随事务结束自动失效——
**不需要**在连接归还连接池时手动 reset，也就不存在"漏了一次 reset 就串租户"的隐患。
`test_tenant_binding_does_not_survive_the_transaction` 在同一条物理连接上验证了这一点。

### 检索质量评估

改了任何检索逻辑（分块、RRF 参数、embedding 策略）之后**必须**跑评估集。

```bash
uv run python -m kb.eval.run
```

> **代码改动后必须跑 eval 验证，不能只看单元测试通过。**
> 单项优化（调 RRF 的 K、放宽最低分阈值）在实测中大量出现**负收益**，没有评估集无从察觉。

---

## 部署注意

- **必须 HTTPS**。bearer token 走明文 HTTP 等于把知识库公开。
- Git 凭证明文**绝不落库**，只存 `credential_ref` 引用。
- 备份只需 `users` / `api_tokens` / `repos` / `index_config` / `conversion_cache`；
  其余全是派生物，可从 Git 重建。

---

## 客户端接入

```bash
claude mcp add --transport http kb https://your-host/mcp \
  --header "Authorization: Bearer <token>"
```

Token 通过 CLI 生成，**只在创建时打印一次**：

```bash
uv run python -m kb.cli user create --email you@example.com
```
