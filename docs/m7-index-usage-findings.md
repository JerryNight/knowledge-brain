# M7.1 索引使用验证 —— 一条已修，一条是结构性限制

**日期**：2026-09-23
**环境**：WSL2 原生 dockerd 29.1.3 · PG17 + pgvector 0.8.6 + zhparser 2.3 · 50000 chunks × 1536 维
**结论**：向量路**已修复**（5935 ms → 0.8 ms，走 HNSW）；关键词路的 GIN 索引在 RLS 下**结构性不可用**，需要一次产品级取舍（见 §三）。

---

## 一、向量路：JOIN 杀掉 HNSW（已修复）

### 症状

`chunk_vector_search()` 原先是 `chunks JOIN documents ... ORDER BY embedding <=> :v LIMIT 40`，
50000 行实测 **5935 ms**，执行计划是 `Seq Scan + top-N heapsort`。

去掉 JOIN 后同一查询 **11.8 ms**，走 `Index Scan using ix_chunks_embedding_hnsw`。

### 根因

`LIMIT` **无法下推到 join 之下**。HNSW 索引扫描的启动代价极高（pgvector 固定估算
`cost=2541.12..207067.73`），只有把 `Limit` 直接挂在索引扫描上才划算。中间夹一个 join，
planner 只能"把两表 join 完（50000 行）再排序取前 40"，索引永远竞争不过。

`EXISTS` 半连接、强制 nested loop、`IN (子查询)` 都无效 —— 分别实测 5786 / 5759 / 6352 ms。

### 修复（已落地在 `kb/retrieval/query_builder.py`）

**形状 A —— `LIMIT` 在 CTE 内先落地，再 join 取元数据：**

```sql
WITH top AS (
  SELECT c.id, c.document_id, c.text, c.embedding <=> :v AS score
  FROM chunks c
  WHERE c.user_id = :tenant AND c.embedding IS NOT NULL
  ORDER BY c.embedding <=> :v
  LIMIT 40
)
SELECT t.*, d.source, d.source_path, d.title
FROM top t JOIN documents d ON d.id = t.document_id
WHERE d.user_id = :tenant          -- 租户谓词在两侧都写，不靠 join 继承
ORDER BY t.score;
```

**形状 B —— 元数据过滤（`tags` / `path_prefix`）先解析成允许列表，再作为数组传入。**
过滤条件在 `documents` 上，但打分阶段不能 join，所以由 `documents_matching_filters()`
先查一次 id，`documents.ids` 以 `IN (...)` 传给打分阶段。

### 权威验证（真实语句 + 真实绑定参数 + RLS 生效）

用驱动层事件截获适配器**实际发出**的语句和参数，再以 `kb_app` 身份 `EXPLAIN ANALYZE`：

| 场景 | 计划 | 执行时间 |
|---|---|---|
| vector / 无元数据过滤 | **Index Scan using ix_chunks_embedding_hnsw** | **0.81 ms** |
| vector / `path_prefix='notes/'`（真实 expanding `IN ($5::UUID)`） | **Index Scan using ix_chunks_embedding_hnsw** | **0.83 ms** |
| 旧形状（JOIN + 最外层 LIMIT） | Hash Join + Seq Scan + top-N heapsort | 185–215 ms |
| `IN (子查询)` 变体 | Hash Join + Seq Scan | 181–192 ms |

> ⚠️ 前面几轮用的是手写的 `= ANY(常量数组)`，而适配器实际发出的是 SQLAlchemy 的
> **expanding `IN (...)` 绑定参数列表** —— 两者并不等价。上表是重做后的结论，
> 绑定参数不改变计划。

**回归守卫**：`tests/unit/test_retrieval_sql.py` 文本级断言"`LIMIT` 必须出现在
`JOIN documents` 之前、打分阶段不得出现 `documents`"。这条回归在原测试套件里是完全
隐形的 —— 把它挪出去只会变慢，不会变红。

---

## 二、关键词路：RLS 让 GIN 索引彻底不可用（结构性）

`ix_chunks_tsv_gin` 存在、有效、也在被维护（5072 kB），**但只要 `chunks` 开着 RLS，
planner 就完全不考虑它**。同一个查询，只切 RLS：

| 条件 | 执行计划 | 耗时 | 估算代价 |
|---|---|---|---|
| `owner`（无 RLS） | **Bitmap Index Scan on ix_chunks_tsv_gin** | 1.0 ms | **38.96** |
| `kb_app`（RLS 生效） | Seq Scan，`Rows Removed by Filter: 49800` | 7.6 ms | **2265.80** |
| `kb_app` + `enable_seqscan=off` | Index Scan `ix_chunks_user_id` + Filter | 8.8 ms | 2691.01 |
| `kb_app` + 临时关 RLS | **Bitmap Index Scan on ix_chunks_tsv_gin** | 0.7 ms | **38.96** |

代价从 38.96 跳到 2265.80（**58 倍**），且 `enable_seqscan=off` 时退到 btree 而不是 GIN
—— 说明 GIN 压根没进候选集。

### 根因

RLS 策略谓词被当作**安全屏障**（security barrier）：屏障之上的谓词不能被下推利用索引，
除非它是 `leakproof` 的。

- `user_id = :tenant` —— **能**作为索引条件，因为该谓词**蕴含**策略谓词，下推它不泄露额外信息；
- `tsv @@ :tsquery` —— **不能**。

### 已排除的方向：「换个 leakproof 的写法」

原先猜测可以改写成 `text @@ tsquery`（leakproof 形式）配 `to_tsvector('chinese', text)`
函数索引绕过屏障。**实测否掉**：`pg_catalog` 里 `@@` 的全部重载都是 `leakproof=false`，
包括 `ts_match_tq` / `ts_match_tt` / `ts_match_vq` / `ts_match_qv`：

```
ts_match_qv      leakproof=False
ts_match_tq      leakproof=False
ts_match_tt      leakproof=False
ts_match_vq      leakproof=False
```

也就是说：**在保留 RLS 的前提下，任何文本检索谓词都无法下推到索引。** 这条路到此为止。

---

## 三、关键词路的三个方向（需要产品级取舍）

| 方向 | 做法 | 代价 |
|---|---|---|
| **1. 接受现状** | 什么都不做 | 关键词路代价与租户语料量成正比（50k → 8 ms，500k → ~80 ms）；GIN 索引白占 5 MB |
| **2. `SECURITY DEFINER` + `BYPASSRLS` 角色** | 把关键词检索包进一个 owner 为 `BYPASSRLS` 角色的函数，租户 id 作为参数传入，函数自己写 `WHERE user_id = p_user_id` | 索引可用；但隔离保证从"数据库策略"降级为"函数契约"，spec §11.1 红线被削弱 |
| **3. 按 `user_id` 分区 `chunks`** | hash 分区，查询带 `user_id` 时分区裁剪 | **仍然用不上 GIN**（屏障还在），只是扫描量降到 1/N |

### 修正：方向 3 的收益被高估了

分区裁剪的前提是**租户数量多**。本项目是**个人知识库**，实际租户数≈1 ——
`1/N` 就是 `1/1`，分区带不来任何收益，只增加一次结构性迁移的复杂度。**方向 3 不再推荐。**

### 现在的建议：方向 1（接受），并保留触发条件

50k chunks 下关键词路 8 ms、向量路 0.8 ms，都在交互式工具的预算内；而关键词路的绝对
代价只取决于**单个租户的语料量**，与租户数无关。红线（spec §11.1）值这个代价。

重新评估的触发条件：单租户 chunks 接近 1M，或关键词分支 p95 超过 50 ms。
届时优先考虑方向 2，并配套：把"函数契约"补进红线测试（跨租户调用必须返回空）。

---

## 四、本次验证通过的部分

```
扩展          vector 0.8.6 · zhparser 2.3 · plpgsql
分词配置      chinese —— '关于 PostgreSQL 向量检索与中文分词的调优记录'
              → 'postgresql':1 '向量':2 '检索':3 '中文':4 '分词':5 '调':6 '优':7 '记录':8
alembic       0001_initial_schema 干净通过，9 张表
RLS           chunks / documents / repos：relrowsecurity=t 且 relforcerowsecurity=t
              策略 = user_id = NULLIF(current_setting('app.user_id', true), '')::uuid
              其余 5 张表刻意无 RLS，迁移里逐条写明理由
索引          ix_chunks_embedding_hnsw  hnsw (embedding vector_cosine_ops) m=16 ef_construction=64
              ix_chunks_tsv_gin         gin (tsv)   ← 有效但被 RLS 挡住（见 §二）
kb_app        rolsuper=f（RLS 生效前提成立）
测试          ruff clean · 单元 369 passed · 集成 20 passed（含 12 个跨租户红线用例）
```

50000 行语料下各索引体积：HNSW 391 MB · GIN 5.0 MB · chunks_pkey 1.1 MB。

---

## 五、复现方法

**M7.1 的验收已固化成脚本**（它就是上面"权威验证"那一步，不要再用一遍手写 SQL）：

```bash
cd /home/yongtao/project/knowledge-brain
docker compose up -d postgres
uv run alembic upgrade head
# 先灌一份语料（合成向量即可）

uv run python scripts/explain_retrieval.py
```

脚本会：跑真实的 `PostgresSearchBackend` → 在驱动层截获**实际发出的语句与绑定参数**
→ 以 `kb_app` 身份 `EXPLAIN ANALYZE` → 断言向量路命中 HNSW 索引。
向量路丢索引时退出码为 **1**。

> 为什么必须用脚本而不是手写 SQL：语句里 `LIMIT` 的位置、元数据过滤的形状
> （expanding `IN (...)` 而不是 `= ANY(数组)`）都会改变计划。手抄一遍 SQL 等于在测
> 别的东西 —— 这正是本文件早期几轮结论需要重做的原因。

**手工排查时的两个坑**：

```bash
docker compose exec -T postgres psql -U kb_app -d kb   # 必须 kb_app，owner 身份没有 RLS
BEGIN;
SELECT set_config('app.user_id', '<tenant>', true);
EXPLAIN (ANALYZE, COSTS OFF) <query>;
ROLLBACK;
```
