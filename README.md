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
# 编辑 .env：填 DATABASE_URL 与 EMBEDDING_API_KEY
```

### 3. 起数据库

```bash
docker compose up -d postgres
```

### 4. 跑迁移

```bash
uv run alembic upgrade head
```

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
# 单元测试（无需数据库）
uv run pytest tests/unit

# 集成测试（需真实 Postgres + pgvector + zhparser）
uv run pytest tests/integration
```

> `pgvector` 和 `zhparser` 是数据库扩展，**mock 不了**。集成测试必须连真实 PG。

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
