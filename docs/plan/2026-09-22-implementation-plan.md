# Obsidian RAG 知识库平台 — 实施计划

日期：2026-09-22
状态：M0–M1 已合并；**M3–M9 代码已完成并通过单元测试**，集成测试待 Docker 环境恢复后补跑
依据：`docs/superpowers/specs/2026-09-22-obsidian-rag-knowledge-base-design.md`
参考项目：`/home/yongtao/project/kb`（同领域已上线项目，本计划的技术栈与工程约定对齐它）

---

## 0. 计划使用说明

- 本计划是 spec 的**落地拆解**，不重复 spec 的设计论证。设计决策看 spec，执行步骤看本计划。
- **里程碑串行，里程碑内任务可并行**。
- 每个里程碑末尾有**验收标准**，不通过不进下一个里程碑。
- 标 🔴 的是红线任务（spec §11.1 明确列为红线），必须优先完成且必须有测试覆盖。
- 标 ⚠️ 的是已识别的环境/技术风险，已在 §9 单独列出。

---

## 0.1 进度快照（2026-09-23 更新）

Docker 已换成 WSL 原生引擎（`docker.io` 29.1.3 + `docker-compose-v2` 2.40.3），
集成测试可以真正跑起来了。

| 里程碑 | 代码 | 单元测试 | 集成测试 |
|---|---|---|---|
| M0 工程地基 | ✅ 已提交（`037ca61`） | ✅ | — |
| M1 数据模型 + 租户隔离 🔴 | ✅ 已合并（PR #1） | ✅ 含结构性守卫 | ✅ **12 passed**（首次真正执行） |
| M2 容器与本地 PG | ✅ 冷构建 4 分 39 秒 | — | ✅ 扩展/分词/RLS/索引全部实测 |
| M3 队列 | ✅ | ✅ | ⏸️ 待补 |
| M4 转换层 | ✅ | ✅ | — |
| M5 分块与 embedding | ✅ | ✅ | — |
| M6 同步管道 🔴 | ✅ | ✅（含红线） | ⏸️ 待补 |
| M7 混合检索 | ✅ 向量路索引已修 | ✅ 含 RRF 红线 + SQL 形状守卫 | ✅ **M7.1 验收通过**（向量路走 HNSW） |
| M8 MCP + REST | ✅ | ✅ | 待真机连 Claude Code |
| M9 评估集 | ✅ 框架 + 模板集 | ✅ | ⏸️ 待真实笔记 |

**当前测试状态**：`ruff check` 零报错；`pytest tests/unit` **369 passed**；
`pytest tests/integration` **20 passed**（含 12 个跨租户红线用例 + 8 个检索读路径用例）。

**M7.1 的结论与修复**（完整记录见 `docs/m7-index-usage-findings.md`）：

1. **向量路走 Seq Scan（5935 ms），已修**：`LIMIT` 无法下推到 join 之下，而 HNSW 的启动
   代价极高，只有 `Limit` 直接挂在索引扫描上才划算。改为「CTE 内先 `LIMIT`，再 join 取
   元数据」，并把 `tags` / `path_prefix` 改成先解析成文档 id 允许列表（避免了
   `IN (子查询)` 被去关联成 Hash Join）。实测 **5935 ms → 0.8 ms**，真实绑定参数下确认走
   `ix_chunks_embedding_hnsw`。回归守卫：`tests/unit/test_retrieval_sql.py`。
2. **关键词路用不上 GIN，属于结构性限制**：RLS 把策略谓词当作安全屏障，非 `leakproof`
   的 `tsv @@ tsquery` 不能下推。已实测排除"改写成 leakproof 形式"这条路（`pg_catalog`
   里 `@@` 的全部重载都是 `leakproof=false`）。三个方向（接受 / `SECURITY DEFINER` /
   分区）的取舍见 findings 文档 §三 —— 当前建议**接受**，因为本项目租户数≈1，
   分区带不来收益，而另两个方向要削弱 spec §11.1 红线。

**搁置项**：
- M9 用真实笔记构造 50~100 条查询并跑基线
- MCP 真机验收（Claude Code 实连 `/mcp`）
- M3 / M6 的集成用例

**期间发现并修掉的 6 个既有缺陷**：
1. `kb/indexer/service.py` 用了 `ExistingChunk` 但没 import（ruff F821）；
2. `kb/retrieval/types.py` 的 `SearchResult.message` 里 `not self.branches` 恒为假
   —— `branches` 是每个分支的计数 dict，永远非空。后果是"两条检索路全挂"时
   会回"没有匹配的笔记"，让模型误判成知识库为空。spec §9 明确禁止这种误导；
3. `tests/integration/conftest.py` 从 `testcontainers.core.exceptions` 导入
   `DockerException` —— 该类在 testcontainers 4.x 已被移除，import 失败又被
   `except ImportError` 吞掉、误报成"testcontainers is not installed"，
   导致 **M1.9 那 12 个跨租户红线用例从未执行过**；
4. `test_query_builder_renders_a_tenant_predicate` 用 `str(uuid)` 断言编译后的 SQL，
   而 PG 渲染 UUID 字面量不带连字符 —— 断言永远失败；
5. 单元测试不隔离 `.env`（`Settings` 声明了 `env_file=".env"`），照 README 建 `.env`
   之后 2 个配置测试立即变红；
6. `docker-compose.yml` 的 `api`/`worker` 共享同一 `build` 与 tag，冷构建时 BuildKit
   并发导出同名镜像，报 `failed to solve: image "kb-app:local": already exists`。

---

## 1. 现状盘点

| 项 | 现状 |
|---|---|
| 仓库 | `git@github.com:JerryNight/knowledge-brain.git`，分支 `main` |
| 已有内容 | 仅 `docs/superpowers/specs/...-design.md`（595 行设计文档） |
| 代码 | **零**。从零开始 |
| 开发环境 | WSL2 Ubuntu-22.04，`/home/yongtao/project/knowledge-brain` |
| 运行环境 | Python 3.10.12（⚠️ 需 3.12）、uv 未装、Docker 未接入该发行版 |
| push 通道 | GitHub SSH 认证已验证通过（`Hi JerryNight!`） |

**结论：这是从 0 到 1 的绿地项目，第一阶段的重点是把工程地基打对。**

---

## 2. 技术栈与工程约定

对齐 `kb` 项目已验证的约定，减少团队认知切换成本。

| 维度 | 选型 | 说明 |
|---|---|---|
| 语言 | Python **3.12+** | 对齐 kb；需在 WSL 装 3.12（见 §9.1） |
| 包管理 | **uv** | 对齐 kb，`pyproject.toml` + `[tool.uv]` |
| Web 框架 | **FastAPI** + `uvicorn[standard]` | spec §9：MCP streamable HTTP + REST |
| MCP | `mcp>=1.27` | 对齐 kb |
| 数据库访问 | **SQLAlchemy 2.0** + **asyncpg** | 迁移用 Alembic |
| 迁移 | **Alembic** | RLS policy 也走迁移管理 |
| 配置 | `pydantic-settings` | 环境变量驱动 |
| 中文分词 | `jieba`（Python 侧）+ **zhparser/pg_jieba**（PG 侧） | ⚠️ 见 §9.2 |
| 代码规范 | `ruff`，line-length=120，target=py312 | 对齐 kb |
| 测试 | `pytest` + `pytest-asyncio`，`asyncio_mode="auto"` | 对齐 kb |
| 容器 | Docker Compose | 自建 PG 镜像 |

### 目录结构（初稿，对齐 kb 的 `src/` 布局）

```
knowledge-brain/
├── docs/
│   ├── plan/                     # 本计划
│   └── superpowers/specs/        # spec（已有）
├── src/kb/
│   ├── config/                   # pydantic-settings 配置
│   ├── db/                       # engine / session / RLS 会话变量
│   ├── models/                   # SQLAlchemy ORM（spec §5 全部表）
│   ├── queue/                    # 队列窄接口 + Postgres 实现（spec §4）
│   ├── sync/                     # sync worker：git 拉取 + diff（spec §6）
│   ├── converter/               # 格式 → markdown，可插拔 registry（spec §7）
│   ├── indexer/                  # 分块 + embedding（spec §8）
│   ├── retrieval/                # 混合检索 + RRF（spec §8）
│   ├── mcp/                      # MCP tools（spec §9）
│   ├── routers/                  # REST 管理接口（spec §9）
│   └── main.py                   # api 入口 / worker.py 入口
├── alembic/                      # 迁移
├── tests/
│   ├── unit/                     # 纯函数测试（分块/RRF/converter）
│   └── integration/              # 需真实 PG 的测试
├── docker/                       # Dockerfile + PG 镜像
├── docker-compose.yml
├── pyproject.toml
└── README.md
```

---

## 3. 里程碑总览

| # | 里程碑 | 关键产出 | 依赖 | 复杂度 |
|---|---|---|---|---|
| M0 | 工程地基 | 骨架 + 依赖 + 配置 + CI-ready | — | 低 |
| M1 | 数据模型 + 租户隔离 🔴 | 全部表 + RLS + query builder + 泄漏测试 | M0 | **高** |
| M2 | 容器与本地 PG | 自建 PG 镜像（pgvector+zhparser）+ compose | M0 | **高** |
| M3 | 队列 | 窄接口 + PG `SKIP LOCKED` 实现 | M1, M2 | 中 |
| M4 | 转换层 | converter registry + MarkItDown + 缓存 | M1 | 中 |
| M5 | 分块与 embedding | 分块规则 + provider 接口 + query 缓存 | M1 | **高** |
| M6 | 同步管道 🔴 | git partial clone + diff + 幂等三级短路 | M3 | **高** |
| M7 | 混合检索 | pgvector + tsvector + RRF | M5 | **高** |
| M8 | MCP + REST 接口 | 3 个 tool + 4 个 REST 端点 + token 认证 | M6, M7 | 中 |
| M9 | 评估集 | 50~100 查询 + Recall/MRR 基线 | M7 | 中 |

**建议执行顺序**：M0 → M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9

> M2 虽标"高"复杂度，但它是 M1 集成测试的前置（测试库要真 PG），所以排在 M1 之后立刻做。

---

## 4. 里程碑详细拆解

### M0 — 工程地基

| # | 任务 | 验收 |
|---|---|---|
| M0.1 | 装 Python 3.12 + uv（见 §9.1） | `uv --version` 可用，`uv run python -V` = 3.12 |
| M0.2 | `pyproject.toml`：依赖 + ruff + pytest 配置 | `uv sync` 成功 |
| M0.3 | 目录骨架 + `src/kb/config/`（pydantic-settings） | `uv run python -c "from kb.config import settings"` 通过 |
| M0.4 | `.gitignore`（排除 `.env`、`__pycache__`、`.venv`） | — |
| M0.5 | `.env.example`（所有配置项，**无真实密钥**） | — |
| M0.6 | `README.md`：本地起服务步骤 | 新人照做能跑通 |
| M0.7 | `ruff check` + `ruff format` 通过 | 零报错 |

**验收标准**：`uv sync && uv run ruff check src tests` 零错误；配置能从环境变量加载。

---

### M1 — 数据模型 + 租户隔离 🔴

> spec §5。这是**第一条红线**所在，也是所有组件的共同地基，必须最先做对。

| # | 任务 | 验收 |
|---|---|---|
| M1.1 | ORM 模型：`users` / `api_tokens` / `repos` / `documents` / `chunks` / `conversion_cache` / `index_config` / `sync_jobs` | 模型与 spec §5 字段逐一对应 |
| M1.2 | Alembic 初始化 + 首个迁移 | `alembic upgrade head` 成功 |
| M1.3 | 索引：HNSW(`embedding`) / GIN(`tsv`) / btree | 迁移中包含，`\d chunks` 可见 |
| M1.4 | `chunks.user_id` 冗余列（spec §5 ①） | 检索 SQL 无需 join documents |
| M1.5 | **RLS policy**：`user_id = current_setting('app.user_id')::uuid` | 直接 SQL 查询也受约束 |
| M1.6 | 会话变量注入：每请求/每事务设置 `app.user_id` | 连接池归还时**必须重置**，防串租户 |
| M1.7 | **唯一约束**：`documents(user_id, source, source_path)`、`chunks(document_id, ordinal)` | spec §5 ③ |
| M1.8 | **query builder 收口**：所有检索唯一入口（spec §5 ②） | 代码层面禁止手写检索 SQL |
| M1.9 | 🔴 **跨租户泄漏测试** | 见下 |

**M1.9 测试细节（spec §11.1 #1，红线）**：
- 建 2 个用户，各建文档 + chunks
- 用 A 的 `user_id` 上下文检索，断言结果中 **B 的 chunk 数量为 0**
- **两层分别验证**：① 绕过 query builder 直接执行原生 SQL —— RLS 应拦住；② 走 query builder —— 应用层应拦住
- 用错误/缺失的 `app.user_id` 也应返回空而非全表

**验收标准**：🔴 跨租户泄漏测试通过（两层闸门都验证）；`alembic upgrade head` 在干净库可重放。

---

### M2 — 容器与本地 PG

> spec §10 + §5 的"必须提前知道的坑"。`pgvector` 与 `zhparser` 是扩展，**mock 不了**。

| # | 任务 | 验收 |
|---|---|---|
| M2.1 | 自建 PG 镜像：基于 `pgvector/pgvector` 或官方 PG + 编译 `zhparser` | `CREATE EXTENSION zhparser;` 成功 |
| M2.2 | 配置中文分词：`zhparser` 的 `text_search_config` | `to_tsvector('chinese', '防止任务重复执行')` 分词正确 |
| M2.3 | `docker-compose.yml`：`postgres` / `api` / `worker` | `docker compose up` 三个服务起来 |
| M2.4 | ⚠️ WSL Docker 集成（见 §9.3） | `docker ps` 在 WSL 内可用 |
| M2.5 | 集成测试 fixture：testcontainers 或 compose 起测试库 | 集成测试能连上真 PG |

**关键风险**：`zhparser` 编译是本项目最容易卡住的一环（见 §9.2）。

**验收标准**：干净机器上 `docker compose up -d postgres && alembic upgrade head` 成功；中文分词验证 SQL 输出正确。

---

### M3 — 队列

> spec §4：队列抽象成窄接口，第一期 PG 实现。

| # | 任务 | 验收 |
|---|---|---|
| M3.1 | 窄接口：`enqueue(job)` / `dequeue()` / `ack(job, result)` / `fail(job, error)` | 接口定义与 spec §4 一致 |
| M3.2 | PG 实现：`SELECT ... FOR UPDATE SKIP LOCKED` | 并发 dequeue 不重复领取 |
| M3.3 | 锁超时：`locked_at` 15 分钟判孤儿（spec §6） | 模拟崩溃后可重新领取 |
| M3.4 | 指数退避：`run_after` + `attempts` | 失败 job 按退避重试 |
| M3.5 | 入队与业务写库**同事务**（spec §4 迁移注意事项） | 事务回滚时 job 不残留 |

**验收标准**：多 worker 并发消费不重复；崩溃恢复测试通过；入队与写库原子性测试通过。

---

### M4 — 转换层（converter）

> spec §7。可插拔 registry，选型 MarkItDown。

| # | 任务 | 验收 |
|---|---|---|
| M4.1 | converter registry（可插拔，spec §7 边界） | 换实现只改一处 |
| M4.2 | MarkItDown 接入：PDF / DOCX / XLSX / PPTX / HTML / CSV | 每格式一个 fixture 通过 |
| M4.3 | **定位元数据**：PDF 带页码、Excel 带 sheet+行号到 `locator` | spec §7 约束 4 |
| M4.4 | 内容哈希缓存：`sha256(blob) → conversion_cache` | 同内容二次转换命中缓存 |
| M4.5 | 状态机：`ok / failed / no_text / unsupported` | 坏文件→`failed`；扫描件→`no_text` |
| M4.6 | 50MB 上限 → `unsupported`（spec §6） | 大文件被跳过不阻塞 |
| M4.7 | 转换失败**不阻塞管道**（spec §7 约束 3） | 单文件失败其他文件照常 |

**验收标准**：spec §11.1 #7 全部通过（3 格式 + 坏文件 + 扫描件）。

---

### M5 — 分块与 embedding

> spec §8。分块规则全部是**纯函数**，可先在 Windows/无 DB 环境下单测。

| # | 任务 | 验收 |
|---|---|---|
| M5.1 | Markdown 感知分块：H1→H2→H3 层级边界 | spec §8 分块表 |
| M5.2 | 短 section（<120 token）向上合并 | 单测覆盖 |
| M5.3 | 超长 section（>800 token）滑窗 + 15% overlap | 单测覆盖 |
| M5.4 | **代码块绝不切开** | 单测覆盖 |
| M5.5 | 宽表按行分组 + **每组重复表头** | 单测覆盖 |
| M5.6 | **embedding 跳过窗口**：<10 或 >8000 token 只进 tsv | 省钱规则，单测覆盖 |
| M5.7 | embed 输入 = 带 heading 上下文；存储 = 干净原文（spec §8） | 单测断言两者不同 |
| M5.8 | `chunker_version` 记录进 `index_config` | 改逻辑必须全量重跑 |
| M5.9 | Embedding provider 接口 `embed(texts) -> list[vector]` | 可插拔 |
| M5.10 | 批量(64~128) + 并发上限 + **429 退避重试** | 限流是常态不是异常 |
| M5.11 | **Query embedding 缓存**：`md5(query)` + 进程内 LRU | 只缓存 query，不缓存 chunk |

**验收标准**：spec §11.1 #5 全部通过；429 重试测试通过；query 缓存命中率可观测。

---

### M6 — 同步管道 🔴

> spec §6。幂等三级短路是**核心设计**，`last_synced_sha` 推进时机是**第二条红线**。

| # | 任务 | 验收 |
|---|---|---|
| M6.1 | git **partial clone**：`fetch --filter=blob:none` | 不全量下载 blob |
| M6.2 | diff：`git diff --name-status -M old new` → A/M/D/R 事件 | 本地 bare repo fixture 测试 |
| M6.3 | 首次全量（`last_synced_sha is null`） | 列出全部文件 |
| M6.4 | **幂等三级短路**（spec §6） | 见下 |
| M6.5 | ⚠️ **`.kbignore` 变更 → 触发全量对比**（spec §6 边界） | 取消忽略后能搜到 |
| M6.6 | 忽略规则：`.obsidian/` `.trash/` `.git/` `.DS_Store` + `.kbignore` | — |
| M6.7 | 首次全量**分批入队**（每批 500） | 中途崩溃不用从头 |
| M6.8 | 🔴 **`last_synced_sha` 只在整批成功后推进** | 见下 |
| M6.9 | D→删 documents（CASCADE 带 chunks）；R→只改路径 | 单测覆盖 |
| M6.10 | frontmatter → `documents.title` / `tags` | 解析测试 |
| M6.11 | Obsidian 语法**原样保留**（`[[双链]]`/callout/dataview） | 不改写 |

**M6.4 三级短路验收（spec §11.1 #3 #4）**：
1. `content_sha` 未变 → 整个文件跳过（改名/touch/纯空白）
2. `converted_sha` 命中 `conversion_cache` → 跳过转换，只重做 chunk+embedding
3. `(document_id, ordinal)` 文本相同 → 跳过 embedding
- **幂等测试**：同一批变更跑两次，chunks 数量与内容完全一致
- **改名测试**：mock 计数 embedding 调用，纯改名触发的调用数 **必须为 0**

**M6.8 验收（spec §11.1 #2，红线）**：模拟批处理中途失败，断言 `last_synced_sha` **没有**推进，重跑能补齐漏掉的文件。

**验收标准**：🔴 `last_synced_sha` 时机测试通过；三级短路 + 幂等 + 改名零重算测试全部通过；Git 操作用本地 bare repo，**不发网络请求**。

---

### M7 — 混合检索

> spec §8。RRF 融合是纯函数，可无 DB 单测。

| # | 任务 | 验收 |
|---|---|---|
| M7.1 | 向量路：`embed(query)` → pgvector HNSW top 40 | ✅ 走索引（`scripts/explain_retrieval.py`） |
| M7.2 | 关键词路：`websearch_to_tsquery` → tsvector GIN top 40 | ⚠️ 依赖 zhparser；GIN 在 RLS 下不可用（见下） |
| M7.3 | **RRF 融合**：`score = Σ 1/(60 + rank)` | 纯函数，直接测排名 |
| M7.4 | 每文档限量：同文件最多 3 chunk（防霸榜） | 单测覆盖 |
| M7.5 | 宽召回：默认 25，上限 50 | 参数校验 |
| M7.6 | 参数：`limit` / `tags` / `path_prefix` | — |
| M7.7 | 结果格式（spec §8）：path/title/source/heading_path/locator/score/snippet | 出处指向**原文件** |
| M7.8 | 强制租户过滤（复用 M1.8 query builder） | 与 M1.9 同源 |
| M7.9 | 管道末端留 `rerank` 钩子（不做实现，spec §8） | 接口占位 |
| M7.10 | query embedding 缓存接入 | 复用 M5.11 |

**验收标准**：spec §11.1 #6（RRF 纯函数测试）通过；EXPLAIN 确认向量路走 HNSW 索引。

**M7.1 状态**：✅ 通过（2026-09-23）。50000 chunks 实测 5935 ms → 0.8 ms，
`Limit` 直接挂在 `Index Scan using ix_chunks_embedding_hnsw` 上。

**M7.2 的已知限制（结构性）**：RLS 把策略谓词当作安全屏障，非 `leakproof` 的
`tsv @@ tsquery` 无法下推到索引 —— 应用角色下 GIN 索引用不上，关键词路扫本租户语料
（50k chunks 实测 8 ms，代价与单个租户的语料量成正比）。三个方向的取舍见
`docs/m7-index-usage-findings.md` §三，当前建议**接受现状**。

---

### M8 — MCP 与 REST 接口

> spec §9。

| # | 任务 | 验收 |
|---|---|---|
| M8.1 | MCP streamable HTTP endpoint（`POST /mcp`） | Claude Code 能连上 |
| M8.2 | tool `search_notes(query, limit=25, tags?, path_prefix?)` | 主入口 |
| M8.3 | tool `read_note(path)` — PDF 返回转换产物**并显式标注** | spec §9 要求 |
| M8.4 | tool `list_notes(prefix?, limit=100)` | 可砍项，但先做 |
| M8.5 | **错误处理**：空结果返回明确文案，**不返回空数组** | 防 Claude 误判工具故障 |
| M8.6 | Token 认证：CLI `kb user create --email x`，token 只打印一次，库存 sha256 | 明文不落库 |
| M8.7 | 每请求解析 `user_id` → `app.user_id` 会话变量 → RLS 生效 | 与 M1.6 打通 |
| M8.8 | REST：`POST /api/repos/{id}/sync` | 手动触发同步 |
| M8.9 | REST：`POST /api/admin/rebuild` | 清空索引全量重建 |
| M8.10 | REST：`GET /api/admin/status` | sha / 待处理 job / failed+no_text 清单 |
| M8.11 | REST：`GET /api/health` | 存活探测 |
| M8.12 | 部署 checklist 写入 README：**必须 HTTPS** | bearer token 明文 HTTP = 公开知识库 |

**验收标准**：Claude Code 实际连上能搜到内容；token 无效返回 401；空结果文案正确。

---

### M9 — 检索质量评估集

> spec §11.2。**不要一开始定目标阈值**，先跑基线。

| # | 任务 | 验收 |
|---|---|---|
| M9.1 | 评估集：50~100 条真实查询（从笔记反向构造） | 三种风格都覆盖 |
| M9.2 | 指标：Recall@5 / Recall@10 / MRR | 可复现运行 |
| M9.3 | 基线跑通并记录 | 基线数字落盘 |
| M9.4 | 纳入流程：改检索逻辑后必须跑 eval | 写进 README |

**评估集三种风格**（spec §11.2）：关键词式（`SKIP LOCKED`）、语义式（「怎么防止任务重复执行」）、实体式（「张三那个方案」）。

**验收标准**：eval 一条命令跑完，输出 R@5/R@10/MRR 基线。

---

## 5. 测试策略

### 两层分离（spec §11 核心原则）

| 层 | 保证什么 | 手段 |
|---|---|---|
| 正确性（§11.1） | **不坏** | 单元 + 集成测试 |
| 检索质量（§11.2） | **搜得准** | 评估集 |

> **代码改动后必须跑 eval 验证，不能只看单元测试通过。**（spec §11.2 原文结论）

### 红线测试清单（必须全覆盖）

| # | 测试 | 归属 |
|---|---|---|
| 1 | 🔴 跨租户泄漏（两层闸门） | M1.9 |
| 2 | 🔴 `last_synced_sha` 推进时机 | M6.8 |
| 3 | 幂等性 | M6.4 |
| 4 | 改名不重算（embedding 调用数为 0） | M6.4 |
| 5 | 分块（代码块/合并/宽表/滑窗） | M5 |
| 6 | RRF 融合（纯函数） | M7.3 |
| 7 | converter（3 格式 + 坏文件 + 扫描件） | M4 |
| 8 | Git 操作（本地 bare repo，不发网络） | M6.2 |

### 测试环境

- **单元测试**：无需 DB，可在任意环境跑（分块、RRF、converter）
- **集成测试**：必须真实 Postgres（`pgvector` + `zhparser` 是扩展，**mock 不了**）→ 用 testcontainers

---

## 6. 分支与提交约定

| 项 | 约定 |
|---|---|
| 主分支 | `main` |
| 功能分支 | `feat/m<N>-<slug>`，如 `feat/m1-data-model-tenant-isolation` |
| 提交信息 | 对齐现有风格：`docs(spec): ...` → 用 `<type>(<scope>): <subject>` |
| PR | 每个里程碑一个 PR，标题含里程碑编号 |
| CI | 每条 PR 必须跑：`ruff check` + `pytest`（单元）；集成测试待 M2 就绪后加入 |

---

## 7. 已知代价（承接 spec §12，不在本计划解决）

| 代价 | 影响 |
|---|---|
| PDF 转换质量退化 | 双栏/复杂表格/公式会乱，选型时已接受 |
| 扫描件不可检索 | 第一期无 OCR，标 `no_text`，设计支持日后补跑 |
| 换 embedding 模型需全量重跑 | 云 API 场景下是真金白银的费用 |
| 中文全文检索依赖扩展 | 必须编译 `zhparser`，否则关键词路失效 |
| PG 中文全文检索有天花板 | 规模更大时需评估迁 ES |
| 宽召回增加上下文占用 | 默认 25 片段，用 token 换召回 |
| Redis 迁移失去事务性 | 换 Redis 后入队/写库一致性需自己兜 |

---

## 8. 第一期明确不做（spec §2）

前端/注册流程、文档共享、后端生成答案、OCR、cross-encoder rerank、Git webhook、双链反向链接图、Redis。

> 若要缩减范围，首砍 `list_notes`（spec §9、§12）。

---

## 9. 环境风险与前置动作

### 9.1 ⚠️ Python 版本不满足

- 现状：WSL 内 Python **3.10.12**，且 `pip`/`ensurepip` 缺失
- 需要：**3.12+**（对齐 kb，spec 用 Python）
- 方案：装 uv（自带 python 管理），`uv python install 3.12`，全程 `uv run` 隔离；或 `apt` 装 `python3.12` + `python3.12-venv`
- **动作**：M0.1 处理

### 9.2 ⚠️ zhparser 编译（本项目最大技术风险）

- spec §5 原文：中文全文检索需要 `zhparser` 或 `pg_jieba`（在 Dockerfile 里编译一次），不装则 `tsvector` 那半边等于没做
- kb 项目里已有 `ik_custom_dict.dic`（分词词典先例），可参考其做法
- **动作**：M2.1 处理，且必须写**验证 SQL**（`to_tsvector('chinese', ...)` 分词正确）

### 9.3 ⚠️ Docker 未接入 WSL 发行版

- 现状：`docker` 只映射到 Windows exe，WSL 内报 "could not be found in this WSL 2 distro"
- **动作（需要用户操作）**：Docker Desktop → Settings → Resources → **WSL Integration** → 为 `Ubuntu-22.04` 打开

### 9.4 ✅ push 通道已验证

- WSL 内 GitHub SSH 认证成功（`Hi JerryNight!`），`github.com` 可直连（1.6s）
- **无需额外配置即可 push / 提 PR**（注意：只有 WSL 内通畅，Windows 侧不通）

---

## 10. 下一步（立即执行）

1. **用户确认本计划**（尤其 §2 技术栈、§3 里程碑顺序）
2. 处理 §9.3（Docker WSL 集成）—— 这是 M2 的前置，也是 M1 集成测试的前置
3. 开分支 `feat/m0-scaffold`，开始 M0
4. M0 完成后立即进 M1（数据模型 + 租户隔离，第一条红线）
