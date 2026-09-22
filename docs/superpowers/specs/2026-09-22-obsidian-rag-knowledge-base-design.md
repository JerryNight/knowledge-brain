# Obsidian RAG 知识库平台 — 设计文档

日期：2026-09-22
状态：待评审

## 1. 背景与目标

用户使用 Obsidian 记录并保存文档。需要一个后端服务，自动把这些文档同步到云端并建立 RAG 检索能力，供 AI 客户端（Claude Code / Claude Desktop）通过 MCP 查询。

明确不要前端。

核心目标：**让 Claude 能搜到用户 Obsidian 里的内容，并给出可溯源的原文片段。**

## 2. 范围

### 第一期做

- 多租户数据模型（所有表带 `user_id`），但**只服务一个用户**（用户自己）
- 从私有 Git 仓库增量同步 Obsidian vault
- 手动上传渠道（与 Git 渠道独立命名空间）
- 文件转换层：PDF / Word / Excel → markdown
- Markdown 感知的分块 + 混合检索（向量 + 关键词）
- MCP endpoint（streamable HTTP）+ REST 管理接口
- Token 认证

### 第一期不做

| 不做的事 | 原因 |
|---|---|
| 前端 / 注册流程 | 用户明确不要前端；第一期用 CLI 建用户发 token |
| 文档共享 / 权限隔离 | 已定为纯私有模型，无共享需求 |
| 后端生成答案 | 决定只返回检索片段，调用方的 LLM 自己组织答案 |
| OCR / 扫描件处理 | converter 预留接口位置，第一期标记 `no_text` 跳过 |
| Cross-encoder rerank | 检索管道末端留钩子，第一期不做 |
| Git webhook 触发 | 轮询已满足「自动同步」，webhook 留第二期 |
| 双链 / 反向链接图 | 第一期原样保留双链文本，不建图（无消费方） |
| Redis | 队列抽象成接口，第一期用 Postgres 实现 |

## 3. 需求约束

| 维度 | 结论 |
|---|---|
| 租户模型 | 多租户、纯私有隔离、不支持共享 |
| 拓扑 | 后端在云服务器，用户各自本地装 Obsidian → 只能推不能拉 |
| 同步 | Git 仓库中转（`obsidian-git` 插件 push → 后端 pull） |
| 查询端 | MCP server，只返回带出处的原文片段 |
| Embedding | provider 可插拔，第一期用云 API |
| 技术栈 | Python |
| 规模目标 | 最终 >100 用户 / 百万级 chunk，但第一期只跑通单用户链路 |

## 4. 整体架构

### 组件边界

每个单元只做一件事，互相不知道对方的内部实现。这是本设计的核心约束。

| 组件 | 职责 | 明确不知道的事 |
|---|---|---|
| **sync worker** | 拉取 Git 仓库，算 diff，产出变更事件 | 不知道 markdown 是什么，不解析内容 |
| **upload API** | 接收手动上传，产出变更事件 | 同上 |
| **converter** | 任意格式 → markdown | 不知道 Git，不知道向量 |
| **indexer** | markdown → chunks → embedding | 不知道内容从哪来（Git 还是上传） |
| **retrieval** | 混合检索，强制租户过滤 | 不知道 Git、不知道 markdown 怎么切的 |

converter 和 embedding provider 都做成**可插拔 registry**，换实现只改一处。

### 数据流

```
Obsidian (本地)
  │  obsidian-git push
  ▼
私有 Git 仓库 ─────────┐
                       │  sync worker（轮询）
                       │  git diff old_sha..new_sha
                       ▼
              [(path, A/M/D/R, content), ...]      ← source='git'
                       
手动上传 ──────────────────────────────────────┐
                                                │  upload API
                                                ▼
                                    [(path, A, content), ...]  ← source='upload'
                                                │
                ┌───────────────────────────────┘
                ▼
           converter（按 content_sha 缓存）
                │  markdown + 定位元数据
                ▼
            indexer
                │  分块 → embedding
                ▼
┌───────────────────────────────────────────┐
│  Postgres（唯一有状态组件）                  │
│  users / repos / documents / chunks        │
│  chunks.embedding  (pgvector, HNSW)        │
│  chunks.tsv        (tsvector, GIN)         │
│  + 行级安全 RLS                             │
└───────────────▲───────────────────────────┘
                │  强制 WHERE user_id = ?
        retrieval（RRF 混合检索）
                │
          /mcp endpoint
                │
     Claude Code / Claude Desktop
```

### 进程模型

一份代码，两个入口：`api` 和 `worker`。可以单独重启、单独扩容、单独看日志。

任务队列**不引入消息中间件**，用 Postgres 表 + `SELECT ... FOR UPDATE SKIP LOCKED`。

队列定成一个窄接口：

```python
enqueue(job) / dequeue() / ack(job, result) / fail(job, error)
```

第一期给 Postgres 实现。以后要换 Redis，只改这一个实现类，调用方零改动。

> **迁移注意事项（不现在解决）**：Postgres 队列有一个 Redis 给不了的好处 —— 入队和数据写入在同一个事务里，不会出现双写不一致。换到 Redis 后这个保证消失，届时需要自己兜。

## 5. 数据模型

### 全局性质：索引库是派生物

所有内容都能从 Git 仓库和上传记录重建。因此：

- 索引坏了、schema 改了、换了 embedding 模型 → **清库重跑**永远是可行路径
- 不需要为了保护索引而设计极端健壮的修复逻辑
- 备份只需要配置表和转换缓存（见 §10）

### Schema

```sql
users
  id            uuid pk
  email         text unique
  created_at    timestamptz

api_tokens
  id            uuid pk
  user_id       uuid fk → users
  token_hash    text                  -- sha256，明文不落库
  name          text
  last_used_at  timestamptz
  created_at    timestamptz
  revoked_at    timestamptz

repos                                  -- Git 来源，一人可配多个
  id             uuid pk
  user_id        uuid fk → users
  url            text
  branch         text default 'main'
  credential_ref text                  -- 指向密钥存储的引用，明文不落库
  last_synced_sha text                 -- diff 的起点
  sync_enabled   bool
  locked_at      timestamptz           -- 队列锁超时判定

documents                              -- 一个逻辑文档
  id                uuid pk
  user_id           uuid fk → users
  source            text               -- 'git' | 'upload'
  source_path       text               -- 仓库内相对路径 / 上传文件名
  content_sha       text               -- sha256(原始字节)
  converted_sha     text               -- sha256(转换后 markdown)，同时也是缓存键
  mime              text
  size_bytes        int
  title             text               -- frontmatter title 或首行
  tags              text[]
  conversion_status text               -- ok | failed | no_text | unsupported
  conversion_error  text
  indexed_at        timestamptz
  UNIQUE (user_id, source, source_path)

chunks
  id            bigserial pk
  user_id       uuid                   -- 冗余存储，见下
  document_id   uuid fk → documents ON DELETE CASCADE
  ordinal       int
  text          text                   -- 返回给用户的干净原文
  heading_path  text[]                 -- ['第三章', '3.2 认证']
  locator       jsonb                  -- markdown: null（用 heading_path 定位）
                                       -- pdf: {page: 12}
                                       -- excel: {sheet: 'Sheet1', row_start: 40}
  token_count   int
  embedding     vector(N)              -- N 由 provider 决定，建表时固定
  tsv           tsvector
  UNIQUE (document_id, ordinal)

conversion_cache                       -- 按内容哈希缓存转换结果
  content_sha   text pk                -- sha256(原始字节)
  converted     text
  status        text
  created_at    timestamptz

index_config                           -- 全局一行
  embedding_provider text
  embedding_model    text
  embedding_dim      int
  chunker_version    int

sync_jobs                              -- Postgres 队列
  id            bigserial pk
  user_id       uuid
  repo_id       uuid null              -- upload 任务为 null
  kind          text                   -- 'repo_sync' | 'doc_index' | 'full_rebuild'
  payload       jsonb
  status        text                   -- pending | running | done | failed
  attempts      int
  last_error    text
  locked_at     timestamptz
  run_after     timestamptz            -- 指数退避
  created_at    timestamptz
```

### 索引

- `chunks.embedding` → HNSW
- `chunks.tsv` → GIN
- `chunks(user_id)`、`documents(user_id, source, source_path)` → btree
- `sync_jobs(status, run_after)` → btree

### 四个有意为之的设计点

**① `chunks.user_id` 冗余存储，不靠 join `documents` 过滤。**

租户过滤是唯一的安全边界。让它出现在检索的那一条 SQL 里、能走索引、能被单测直接验证，比省这点存储重要得多。

**② 双层租户隔离：应用层 query builder + 数据库 RLS。**

- 应用层：所有检索收口到一个 query builder，**唯一入口**，杜绝手写 SQL 漏 `WHERE`
- 数据库层：RLS policy 强制 `user_id = current_setting('app.user_id')::uuid`

任何一道单独失效都不会泄漏。代价是半天工期，换掉一类灾难性 bug。

**③ `documents` 按 `(user_id, source, source_path)` 唯一。**

这是两条渠道不打架的机制：Git 里删文件不会碰到手动上传的文档，同名也不覆盖。

**④ 换 embedding 模型 = 全量重跑。**

一个 chunk 表只能有一个固定维度的向量列。`index_config` 记录当前 provider/model/维度，运行时不匹配就报错要求重建。因为索引是派生物，重建安全 —— 但如果用云 API，这是一笔真金白银的费用。

### 必须提前知道的坑

中文全文检索需要 `zhparser` 或 `pg_jieba` 扩展（在 Dockerfile 里编译一次）。不装的话 Postgres 会把整句中文当成单个 token，`tsvector` 那半边等于没做。

## 6. 同步管道

### 触发

| 方式 | 第一期 | 说明 |
|---|---|---|
| 定时轮询 | ✅ 默认 5 分钟 | 满足「自动同步」诉求 |
| 手动端点 `POST /api/repos/{id}/sync` | ✅ | 刚写完想立刻查、调试用 |
| Git webhook | ❌ 第二期 | 要处理 secret 校验和重放，轮询已够用 |

### 拉取与 diff

每个 repo 一个 job，领取时用 `SKIP LOCKED` 锁住，防同一仓库并发同步。

```
git fetch origin <branch>            # partial clone
new_sha = rev-parse origin/<branch>

new_sha == last_synced_sha  → 无变更，结束
last_synced_sha is null     → 首次全量，列出全部文件
否则                         → git diff --name-status -M <old> <new>
                               产出 A/M/D/R 变更事件
```

**用 partial clone（`git fetch --filter=blob:none`）**。浅克隆 `--depth=1` 拿不到历史就 diff 不了；完整克隆在带附件时会撑爆磁盘。partial clone 拿到完整提交历史但不下载 blob，需要哪个文件才按需取。

### 幂等三级短路 ← 核心设计

每个变更事件进来，先按 `content_sha` 逐级短路：

| 检查 | 命中则跳过 | 主要吃掉的场景 |
|---|---|---|
| `content_sha` 未变 | 整个文件跳过（含转换） | 改名、touch、纯空白改动 |
| `converted_sha` 命中 `conversion_cache` | 跳过转换，只重做 chunk + embedding | 附件内容重复出现 |
| `(document_id, ordinal)` 已存在且文本相同 | 跳过 embedding | 任务重跑、手动点两次同步 |

第二级主要吃**改名**：Git 的 `-M` 报 `R100 old.md new.md`，内容没变，只更新 `source_path`，chunks 一个都不重算。没有这一级，重命名一个目录会导致下面所有笔记的向量重算。

第三级是**可重入的基础**：任务重复执行、worker 崩溃后重跑，结果都一致。

### 首次全量

几万个文件的全量转换 + embedding 是小时级的。全量同步**分批入队**（每批 500 个文件），而非一次性全塞。好处：中途崩溃不用从头来，进度在 `documents` 表里天然可查。

### 忽略规则

默认忽略：`.obsidian/`、`.trash/`、`.git/`、`.DS_Store`。

另支持仓库根目录 `.kbignore`（gitignore 语法）。

> **必须处理的边界情况**：用户修改 `.kbignore` 本身也是一次文件变更。改完之后，新放开规则的文件要补索引、新收窄规则的文件要删索引。因此 `.kbignore` 变更必须作为**触发一次全量对比**的信号，而不是当普通文件索引。漏掉这条会出现「我明明取消忽略了，怎么还是搜不到」。

### 失败、重试、崩溃恢复

| 情况 | 处理 |
|---|---|
| 单文件转换失败 | 标 `conversion_status='failed'` + `conversion_error`，只影响它自己 |
| 单文件超过 50MB | 标 `unsupported` 跳过，防止巨型文件占住 worker |
| 整个 repo fetch 失败 | job 失败，指数退避重试 |
| 进程崩溃 | `locked_at` 超时（15 分钟）后判定孤儿，重新可领取 |
| 批处理中途崩溃 | `last_synced_sha` **不推进**，重跑补齐 |

> **`last_synced_sha` 只在整批成功后推进。** 推进早了，下次 diff 会漏掉这批里没处理完的文件 —— 这是同步系统最经典的静默丢数据 bug，需要专门的测试覆盖（见 §11）。

### 删除与改名

- `D` → 删 `documents` 行，`ON DELETE CASCADE` 带走 chunks
- `R` → 交给第二级短路处理，内容没变就只改路径

### Obsidian 特有语法

`[[双链]]`、`![[嵌入]]`、`> [!note]` callout、dataview 代码块 —— 第一期**原样保留文本**，不改写、不建链接图。

frontmatter 解析出来存进 `documents.title` 和 `documents.tags`，检索时可作为过滤条件。这是有用的，要做。

## 7. 转换层（converter）

### 边界

**输入二进制 blob + 格式，输出 markdown。** 不知道 Git，不知道向量。

跟 embedding provider 一样做成可插拔 registry，以后某个格式想换更强的解析器只改一个实现。

### 选型：MarkItDown

微软开源（MIT），Python，一个库覆盖 PDF / DOCX / XLSX / PPTX / HTML / CSV / 图片。安装零负担、无需模型文件。

PDF 版面分析较弱（双栏、复杂表格容易乱），这是选型时就接受的代价。

### 六条设计约束

1. **转换发生在索引期，不是检索期。** 检索时现转 PDF 会慢到不可用，而且转换结果无法进全文索引。
2. **按内容哈希缓存。** `sha256(blob) → markdown` 存 `conversion_cache` 表。PDF 转换是秒级到十几秒级，全量重建索引时不能重转。
3. **转换失败绝不阻塞管道。** `conversion_status` 记录 `ok / failed / no_text / unsupported`，单文件失败只标记它自己，可单独重试。
4. **出处指向原文件，不是转换后的文本。** 检索结果要说「来自 `报告.pdf` 第 12 页」，所以转换时必须把页码 / 工作表名带进 chunk 的 `locator`。
5. **质量退化要提前认。** 双栏 PDF、复杂表格、含公式文档转出来的结构会乱。这是所有 PDF 解析器的通病，不是选型能解决的。
6. **上传端只传原始文件，转换全在服务端。** 客户端先转再传会导致转换逻辑有两份，且服务端失去重新转换的能力（换解析器时得求用户重传）。

### 扫描件

第一期不做 OCR。解析不出文本的 PDF / 图片统一标 `no_text`，不入索引但在元数据里可见。

因为 `no_text` 是一个明确的状态，以后接 OCR 时只需要重跑这批文件 —— 这个设计天然支持补跑。

## 8. 分块与检索

### 分块

Markdown 的结构是免费信息。按标题层级切，不按固定字符数切。

| 规则 | 取值 / 行为 |
|---|---|
| 切分点 | H1 → H2 → H3 层级边界 |
| 短 section 合并 | 不足 120 tokens 的 section 向上合并，碎片会污染检索 |
| 超长 section | 超过 800 tokens 按段落滑窗二次切分，15% overlap |
| 代码块 | **绝不切开**，宁可让该 chunk 超长 |
| 表格 | 小表整体保留；宽表按行分组，**每组重复表头** |

宽表重复表头这条是必需的：Excel 转出来的宽表，后半截 chunk 全是裸数字，没有任何检索价值。

### embedding 输入 ≠ 存储文本

```
embed 的输入  = "# 认证\n## 3.2 认证流程\n\n{jwt 校验只接受 RS256，...}"
返回给用户的   = "jwt 校验只接受 RS256，..."
```

「配置超时时间」这类短 chunk 脱离标题就无从理解，向量化时必须带上下文；但用户看到的片段应该是干净原文。`heading_path` 同时存进元数据数组。

`chunker_version` 记录在 `index_config`，改了切分逻辑必须全量重跑 —— 没有版本号兜着，新旧 chunk 混在库里没法排查。

### Embedding provider 接口

```python
embed(texts: list[str]) -> list[vector]
```

实现里必须处理：批量（64~128）、并发上限、**云 API 限流退避重试**。

> 限流不是异常情况，是常态。不能拿 429 当致命错误。

### 检索：混合检索

纯向量检索对这个场景不够。个人知识库最高频的查询恰好是向量最不擅长的：专有名词、代码标识符、人名、精确短语。「我记的那个 `SKIP LOCKED` 的笔记在哪」 —— 纯向量基本命不中，纯关键词一枪毙命。反过来「怎么防止任务重复执行」这种语义查询，纯关键词也不行。

两路并行，用 **RRF（Reciprocal Rank Fusion）** 融合：

```
query
 ├─ 向量路：embed(query) → pgvector HNSW，top 40
 └─ 关键词路：websearch_to_tsquery → tsvector GIN，top 40
                      ↓
        RRF 融合：score = Σ 1/(60 + rank)
                      ↓
        每文档限量：同一文件最多 3 个 chunk，防霸榜
                      ↓
               top N（默认 8）+ 出处
```

**为什么是 RRF 而不是加权求和**：两路的分数尺度完全不同（余弦距离 vs `ts_rank`），归一化之后加权是拍脑袋。RRF 只看排名，不需要调权重，鲁棒得多。

关键词路依赖 `zhparser`（见 §5）。

### 明确不做

| 不做 | 理由 |
|---|---|
| Cross-encoder rerank | 要选中文模型、跑推理、加延迟，第一期不值当。管道末端留 `rerank` 钩子，接入时只改一处 |
| Query 改写 | 后端不接 LLM。改写是调用方（Claude）的事，它比后端更清楚用户意图。这让检索接口保持成纯函数 |

### 检索参数

`query`（必填）、`limit`（默认 8）、`tags`（可选）、`path_prefix`（可选，限定目录）。

### 结果格式

```json
{
  "path": "报告.pdf",
  "title": "Q3 复盘",
  "source": "upload",
  "heading_path": ["结论"],
  "locator": {"page": 12},
  "score": 0.032,
  "snippet": "...原文片段..."
}
```

出处指向**原文件**。markdown 文件用 `heading_path` 定位、`locator` 为 null；PDF 带页码；Excel 带工作表名和起始行。

## 9. MCP 接口与认证

### 暴露方式

后端直接暴露 **streamable HTTP** 的 MCP endpoint（`POST /mcp`），不需要本地 stdio 代理。

客户端配置（已对照 Claude Code 当前文档确认）：

```bash
claude mcp add --transport http kb https://your-host/mcp \
  --header "Authorization: Bearer <token>"
```

SSE transport 已废弃。`--transport http` 在服务端不支持 HTTP 时会自动回退 SSE，所以这个写法是安全的。

### MCP tools

三个，全部只读：

| tool | 签名 | 用途 |
|---|---|---|
| `search_notes` | `(query, limit=8, tags?, path_prefix?)` | 主入口，返回带出处的片段 |
| `read_note` | `(path)` | 取全文。检索只给片段，Claude 需要完整上下文时调它 |
| `list_notes` | `(prefix?, limit=100)` | 浏览目录，回答「我关于 X 都写了什么」 |

`read_note` 是必需的 —— 没有它 Claude 只有碎片，没法「读完再总结」。`list_notes` 价值次一等，**若要砍功能，砍它**。

`read_note` 遇到 PDF 时返回**转换后的 markdown，并在响应里明确标注这是转换产物**（附原文件路径和 `conversion_status`）。不能让 Claude 以为它读到的是原始排版。

### 错误处理

一律结构化返回，不抛裸异常：

- 空结果明确说「没有匹配的笔记」，**不返回空数组**（空数组会让 Claude 误判成工具故障）
- 文件不存在给明确原因
- token 无效返回 401

### 认证

- 生成：CLI `kb user create --email you@x.com`，**token 只在创建时打印一次**，库里只存 sha256
- 第一期不做注册流程，CLI 建用户 + 发 token
- **必须 HTTPS**。bearer token 走明文 HTTP 等于把知识库公开。写入部署 checklist
- 每个请求解析出 `user_id`，塞进 `app.user_id` 会话变量，让 RLS policy 生效

### REST 管理接口

| 端点 | 用途 |
|---|---|
| `POST /api/repos/{id}/sync` | 手动触发同步 |
| `POST /api/admin/rebuild` | 清空索引并全量重建。§5 那个「索引是派生物」性质的兑现出口，换 embedding 模型或改 chunker 版本时走这里 |
| `GET /api/admin/status` | 每个 repo 的 `last_synced_sha`、待处理 job 数、`failed` / `no_text` 文件清单 |
| `GET /api/health` | 存活探测 |

## 10. 部署与运维

### Docker Compose

| 服务 | 说明 |
|---|---|
| `postgres` | 自建镜像，编译 `pgvector` + `zhparser` |
| `api` | FastAPI，暴露 `/mcp` + `/api/*` |
| `worker` | 同一个代码库，跑队列消费 |

迁移用 Alembic，配置走环境变量 + pydantic-settings。

`credential_ref` 指向密钥存储（文件或环境变量），**Git 凭证明文绝不落库**。

### 备份

只需要备份配置表和转换缓存：

- `users`、`api_tokens`、`repos`、`index_config`
- `conversion_cache`（可选，丢了只是重转一遍）

其余全是派生物，能从 Git 重建。这是 §5 那个性质的第二次兑现。

### 可观测

第一期不做 Prometheus 那套。`GET /api/admin/status` + 结构化日志（带 `job_id` / `user_id` / `document_id` 关联字段）足够。

## 11. 测试策略

按重要性排序，前两条是红线。

| # | 测试 | 验证什么 |
|---|---|---|
| 1 | **跨租户泄漏** | 两用户各建文档，用 A 的 token 检索，断言结果里一个 B 的 chunk 都没有。同时验证 query builder 和 RLS 两层闸门 |
| 2 | **`last_synced_sha` 推进时机** | 模拟批处理中途失败，断言 sha **没有**推进，重跑能补齐漏掉的文件 |
| 3 | **幂等性** | 同一批变更跑两次，断言 chunks 数量与内容和第一次完全一致 |
| 4 | **改名不重算** | mock 计数 embedding 调用次数，断言纯改名触发的调用数为 0 |
| 5 | **分块** | 代码块不被切断、短 section 会合并、宽表重复表头、超长 section 滑窗 |
| 6 | **RRF 融合** | 纯函数，直接测排名结果 |
| 7 | **converter** | 三种格式各一个 fixture，外加一个坏文件（断言 `failed`）、一个扫描件（断言 `no_text`） |
| 8 | **Git 操作** | 本地临时 bare repo 做 fixture，不发网络请求 |

测试库必须跑真实 Postgres —— `pgvector` 和 `zhparser` 是扩展，mock 不了。用 testcontainers 起。

## 12. 已知代价汇总

这些是设计时明确接受的，不是遗漏：

| 代价 | 说明 |
|---|---|
| PDF 转换质量退化 | 双栏、复杂表格、公式文档结构会乱。所有 PDF 解析器的通病 |
| 扫描件不可检索 | 第一期无 OCR，标 `no_text`。设计上支持日后补跑 |
| 换 embedding 模型需全量重跑 | 云 API 场景下是真金白银的费用，不是免费操作 |
| 中文全文检索依赖扩展 | 必须编译 `zhparser`，否则关键词路失效 |
| Redis 迁移时失去事务性 | Postgres 队列的入队/写库同事务保证，换 Redis 后需自己兜 |
| `list_notes` 可砍 | 若要缩减第一期范围，这是首选项 |

## 13. 后续演进方向（不在本期）

- Git webhook 触发同步
- OCR 管道（重跑 `no_text` 文件）
- Cross-encoder rerank（管道末端已留钩子）
- 多用户注册与自助 token 管理
- 独立向量库（仅当 pgvector 成为瓶颈时）
- Redis 队列实现（接口已就绪）