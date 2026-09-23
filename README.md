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

### 代码地图

```
src/kb/
├── config/       pydantic-settings，启动时校验（含"两个 DSN 不能同角色"）
├── db/           双引擎（app / admin）+ RLS 会话变量 + adapters/（端口们的 PG 实现）
├── models/       spec §5 全部表
├── queue/        窄接口 + PG `FOR UPDATE SKIP LOCKED` 实现
├── sync/         git partial clone、diff、幂等三级短路
├── converter/    格式 → markdown，可插拔 registry
├── indexer/      分块 + embedding provider + query 缓存
├── retrieval/    query_builder（租户 SQL 唯一入口）+ RRF + 检索服务
├── mcp/          3 个只读 tool
├── routers/      REST 管理接口
├── auth.py       纯函数：token 生成/哈希/bearer 解析
├── context.py    请求级 principal（ContextVar）
├── middleware.py 纯 ASGI 认证中间件
├── wiring.py     唯一的对象图组装点
├── app.py        FastAPI app 工厂（MCP 路由 + REST + 中间件）
├── main.py       ASGI 入口（`uvicorn kb.main:app`）
├── worker.py     队列消费者（`python -m kb.worker`）
├── cli.py        `kb` 命令
└── evaluate.py   Recall@k / MRR 评估
```

**端口与适配器分离**：`sync/ports.py`、`indexer/ports.py`、`retrieval/ports.py`
定义 Protocol，`db/adapters/` 给 Postgres 实现，测试用 `tests/unit/fakes.py`。
所以"批处理中途失败"、"改名零重算"、"一路检索挂掉"这些行为都能在没有数据库的
情况下精确构造出来 —— 这些恰好是最容易写错、也最难在集成测试里复现的部分。

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

### 5. 建用户、发 token

第一期没有注册流程，用 CLI 建用户（spec §9）。**token 只在创建时打印一次**，
库里只存 sha256 —— 丢了只能重新签发。

```bash
uv run kb user create --email you@example.com
```

输出会直接给你一条可粘贴的 `claude mcp add` 命令。想给已有用户再发一个 token：

```bash
uv run kb token issue --email you@example.com --name laptop
uv run kb token revoke --token-id <token_id>
```

### 6. 注册仓库

```bash
# 私有仓库的 token 放进环境变量，--credential-ref 只传变量名（明文不落库）
export KB_VAULT_TOKEN=ghp_xxx
uv run kb repo add --email you@example.com \
  --url git@github.com:you/vault.git --branch main \
  --credential-ref KB_VAULT_TOKEN
```

首次同步会自动入队。手动触发 / 看进度：

```bash
uv run kb sync --email you@example.com                 # 所有仓库
uv run kb sync --email you@example.com --repo-id <id>  # 单个
uv run kb status --email you@example.com               # sha / 队列 / 不可检索文件
uv run kb rebuild --email you@example.com              # 清空索引全量重建
```

### 7. 起服务

```bash
# API + MCP
uv run kb serve
# 等价于 uv run uvicorn kb.main:app --host 0.0.0.0 --port 8000

# Worker（另开一个终端）
uv run kb worker
# 等价于 uv run python -m kb.worker
```

或者整栈用 compose（postgres + api + worker）：

```bash
cp .env.example .env    # 填 EMBEDDING_API_KEY
docker compose build
docker compose up -d
docker compose exec api alembic upgrade head
```

### 8. 上传文档（与 Git 渠道互相独立的命名空间）

```bash
curl -X POST "http://localhost:8000/api/documents?path=报告.pdf" \
  -H "Authorization: Bearer $KB_TOKEN" \
  -H "Content-Type: application/pdf" \
  --data-binary @报告.pdf
```

请求体就是原始字节（不需要 multipart）。原件存到 `BLOB_STORE_PATH`，
`documents` 行与 `doc_index` job 在**同一个事务**里提交（spec §4）。

---

## REST 管理接口

全部需要 `Authorization: Bearer <token>`，只有 `/api/health` 例外。

| 端点 | 用途 |
|---|---|
| `GET /api/health` | 存活探测（不查库；顺便报告向量路是否可用） |
| `GET /api/repos` | 列出本租户的仓库与 `last_synced_sha` |
| `POST /api/repos` | 注册仓库 |
| `POST /api/repos/{id}/sync` | 手动触发同步（入队，由 worker 消费） |
| `POST /api/documents?path=…` | 上传原始文件 |
| `POST /api/admin/rebuild` | 清空索引并全量重建 |
| `GET /api/admin/status` | sha / 队列深度 / `failed`+`no_text` 清单 / index_config 是否与配置一致 |

> 同步与重建都是**入队**而不是同步执行：轮询与手动触发因此走完全相同的代码路径，
> `last_synced_sha` 那条红线只有一个实现（spec §11.1 #2）。

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
> 集成测试用 testcontainers 起一个 `kb-postgres:local` 容器，所以先执行
> `docker compose build postgres`；Docker 不可用时它们会 **skip 并给出提示**，
> 而不是静默通过——跳过和通过必须能区分开。

单元测试覆盖的核心行为，都是"排错了很难发现"的那一类：

| 文件 | 覆盖 |
|---|---|
| `test_chunker.py` | spec §8 分块规则：H1–H3 边界、短节合并、滑窗重叠、代码块不切开、宽表分组重复表头 |
| `test_indexer_service.py` | 三级短路里的第三级（同 ordinal 同文本 → 不重算向量）、embedding 窗口、转换失败隔离 |
| `test_sync_pipeline.py` | 🔴 `last_synced_sha` 只在整批成功后推进；改名零 embedding；`.kbignore` 双向全量对比 |
| `test_retrieval.py` | 🔴 RRF 纯函数排名；一路失败不算整次查询失败；每文档限量；空结果的文案 |
| `test_worker.py` | 三种 job 的分发与 ack/fail；重建按批入队 |
| `test_api.py` | 认证中间件、401、principal 不串请求、REST 语义（404 而非 403） |
| `test_mcp_tools.py` | spec §9 的结果格式：空结果给文案、转换产物显式标注 |
| `test_evaluate.py` | Recall@k / MRR 的手算校验；评估集格式校验 |
| `test_tenant_scoping_guard.py` | 结构性守卫（见下） |

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

改了任何检索逻辑（分块、RRF 参数、embedding 策略）之后**必须**跑评估集（spec §11.2）。

```bash
# 先校验评估集格式（不需要数据库，CI 可以跑）
uv run kb eval --cases eval/queries.example.jsonl --dry-run

# 真正跑，输出 Recall@5/@10 + MRR，按查询风格分开
uv run kb eval --cases eval/my-queries.jsonl --email you@example.com --out reports/baseline.md
```

**评估集必须从你自己的笔记反向构造**，50~100 条，三种风格各覆盖一些
（关键词式 `SKIP LOCKED`、语义式「怎么防止任务重复执行」、实体式「张三那个方案」）。
`eval/queries.example.jsonl` 只是格式模板，照抄它等于自己给自己出题 —— 测不出东西。

> **不要一开始定「R@5 必须 > 0.8」这种阈值。** 先跑基线，再决定往哪优化。
> **代码改动后必须跑 eval 验证，不能只看单元测试通过。**
> 单项优化（调 RRF 的 K、放宽最低分阈值）在实测中大量出现**负收益**，没有评估集无从察觉。

### 进程模型

一份代码，两个入口，共用 `kb/wiring.py` 里同一个对象图 —— 这是 spec §4
「api 与 worker 可以单独重启/扩容/看日志」的实现方式。

| 入口 | 命令 | 职责 |
|---|---|---|
| api | `kb serve`（`kb.main:app`） | `/mcp` + `/api/*`；认证、检索、入队 |
| worker | `kb worker`（`python -m kb.worker`） | 消费队列：`repo_sync` / `doc_index` / `full_rebuild` |
| CLI | `kb <subcommand>` | 建用户、发 token、注册仓库、同步、重建、评估 |

`kb.wiring.build_services()` 是唯一构造适配器的地方。两个入口都从它拿服务，
所以一个配置项不可能在 api 和 worker 里解释成两回事。

### 认证与租户绑定

`kb/middleware.py` 是一个**纯 ASGI** 中间件（不是 `BaseHTTPMiddleware`）：
同一个 task 里解析 token、往 `kb.context` 写入 principal，所以 MCP 工具和 REST
handler 拿到的身份没有二义性，也不会串到下一个请求。

token → `user_id` → 每个事务里 `set_config('app.user_id', …, is_local => true)`
→ RLS policy 生效。**每个端点都不需要自己认证**：漏了就取不到 principal，直接失败。

---

## 部署注意

- **必须 HTTPS**。bearer token 走明文 HTTP 等于把知识库公开。
- `DATABASE_URL` 必须是**非特权** `kb_app` 角色。配置层会在启动时拦下同角色的情形
  （RLS 对 superuser 恒不生效，对表 owner 默认也不生效）。
- Git 凭证明文**绝不落库**，只存 `credential_ref`；git 子进程通过环境变量拿 token，
  不走命令行参数（argv 对同机所有进程可见）。
- 备份只需 `users` / `api_tokens` / `repos` / `index_config` / `conversion_cache`；
  其余全是派生物，可从 Git 重建。
- `GIT_WORKDIR_ROOT` 与 `BLOB_STORE_PATH` 要挂持久卷 —— 后者是**上传原件的唯一副本**。

---

## 客户端接入

```bash
claude mcp add --transport http kb https://your-host/mcp \
  --header "Authorization: Bearer <token>"
```

三个只读工具：

| tool | 签名 | 用途 |
|---|---|---|
| `search_notes` | `(query, limit=25, tags?, path_prefix?)` | 主入口，宽召回带出处的片段 |
| `read_note` | `(path)` | 取全文；PDF/Word/Excel 会明确标注是转换产物 |
| `list_notes` | `(prefix?, limit=100)` | 浏览目录 |

空结果返回的是**一句话**而不是空数组 —— 空数组会让模型误判成工具故障（spec §9）。

