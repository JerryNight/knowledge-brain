"""MCP tools — the read-only surface Claude Code talks to (spec §9).

Three tools, no writes:

* ``search_notes`` — the main entry point. Wide recall with provenance, so the
  caller (a model with human-level judgement) does the selecting.
* ``read_note`` — full text, because search only returns fragments and a model
  cannot summarise what it has not read.
* ``list_notes`` — browse the tree. Explicitly the cuttable one (spec §12).

**Errors are values, not exceptions.** Spec §9 is specific about why: an empty
JSON array reads as "the tool is broken" to a model, whereas a sentence saying
"没有匹配的笔记" reads as "your search was too narrow". Every branch below
therefore returns a human-readable string, and the only exception that escapes is
"no authenticated principal", which is a server defect rather than a user outcome.

Authentication happens in middleware, not here (``kb.middleware``). The tools call
``require_principal()`` to read the tenant that middleware already established, so
there is exactly one place in the codebase that decides who the caller is.
"""

from __future__ import annotations

import json
import logging

from kb.context import require_principal
from kb.retrieval.types import SearchQuery
from kb.wiring import Services

LOGGER = logging.getLogger(__name__)

SERVER_NAME = "knowledge-brain"

INSTRUCTIONS = (
    "检索用户的 Obsidian 知识库。"
    "先用 search_notes 宽召回带出处的片段，判断哪个文件可能相关；"
    "需要完整上下文时用 read_note 读全文；不知道有哪些笔记时用 list_notes 浏览目录。"
    "结果里的 path 指向原文件，locator 给出页码或工作表位置。"
)

LIST_NOTES_DEFAULT_LIMIT = 100
LIST_NOTES_MAX_LIMIT = 500

NOT_FOUND_TEMPLATE = "找不到路径为 `{path}` 的文档。可以先用 list_notes 看一下实际路径。"
NO_NOTES_TEMPLATE = "知识库里还没有{scope}文档。先用 CLI 或管理接口注册一个 Git 仓库，或上传一个文件。"


def _dumps(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_mcp_server(services: Services):
    """Construct the MCP server with its three tools bound to ``services``.

    The tools are closures over ``services`` rather than globals, so a test can
    build a server against fakes without touching module state.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "在用户的知识库里做混合检索（向量 + 关键词），返回带出处的原文片段。"
            "默认宽召回 25 条：宁可多给候选，也不要漏掉答案。"
            "limit 上限 50；tags 过滤 frontmatter 标签；path_prefix 限定目录。"
        )
    )
    async def search_notes(
        query: str,
        limit: int = 25,
        tags: list[str] | None = None,
        path_prefix: str | None = None,
    ) -> str:
        principal = require_principal()
        result = await services.retrieval.search(
            SearchQuery(
                query=query,
                limit=limit,
                tags=tuple(tags or ()),
                path_prefix=path_prefix,
            ),
            user_id=principal.user_id,
        )
        return _dumps(result.as_dict())

    @server.tool(
        description=(
            "读取一个文档的全文。path 用 search_notes / list_notes 返回的路径。"
            "若该文档是 PDF/Word/Excel 等，返回的是服务端转换后的文本，响应里会明确标注。"
        )
    )
    async def read_note(path: str) -> str:
        principal = require_principal()
        content = await services.documents.read(user_id=principal.user_id, path=path)
        if content is None:
            return _dumps({"path": path, "found": False, "message": NOT_FOUND_TEMPLATE.format(path=path)})
        return _dumps({"found": True, **content.as_dict()})

    @server.tool(
        description=(
            "列出知识库里的文档（可按目录前缀过滤）。"
            "用于回答「我关于 X 都写了什么」，或查看目录结构。"
        )
    )
    async def list_notes(prefix: str | None = None, limit: int = LIST_NOTES_DEFAULT_LIMIT) -> str:
        principal = require_principal()
        effective = max(1, min(limit, LIST_NOTES_MAX_LIMIT))
        documents = await services.documents.browse(
            user_id=principal.user_id, prefix=prefix, limit=effective
        )
        if not documents:
            scope = f"以 `{prefix}` 开头的" if prefix else ""
            return _dumps(
                {
                    "path_prefix": prefix,
                    "count": 0,
                    "notes": [],
                    "message": NO_NOTES_TEMPLATE.format(scope=scope),
                }
            )
        return _dumps(
            {
                "path_prefix": prefix,
                "count": len(documents),
                "notes": [document.as_dict() for document in documents],
            }
        )

    return server


def mcp_routes(services: Services) -> list:
    """The MCP endpoint's routes, ready to be added to the FastAPI app.

    Returned rather than mounted: ``app.mount`` would put the sub-application's
    lifespan out of reach, and the streamable session manager needs to be started
    by the process that owns the loop (see ``kb.main``). Adding the routes
    directly keeps one lifespan, one event loop, one shutdown path.
    """
    server = build_mcp_server(services)
    app = server.streamable_http_app()
    return list(app.routes)


__all__ = ["INSTRUCTIONS", "SERVER_NAME", "build_mcp_server", "mcp_routes"]
