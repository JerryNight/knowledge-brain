"""``kb`` — the operational CLI (spec §2 / §9 / §10).

Phase 1 has no registration flow and no frontend, so this is the only way to
create a user, mint a token, register a repository, or run the two processes.

Every subcommand that touches the database builds the same ``Services`` container
the API and the worker use. That is not just tidiness: it means ``kb repo add``
writes a repository through exactly the tenant-scoped builder the REST endpoint
uses, so the CLI cannot become a quieter path around the isolation rules.

**The token is printed once.** It is stored only as a sha256, so a token that is
not captured at creation time cannot be recovered — it can only be replaced. The
output says so, loudly, in the same breath.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from kb.wiring import build_services

# Set once, before any adapter is constructed, so the first query is not the
# first thing anyone hears about.
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def _configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format=DEFAULT_LOG_FORMAT)


def _parse_uuid(value: str, *, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise SystemExit(f"{what} is not a UUID: {value!r}") from None


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def _user_create(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = await services.tokens.create_user(args.email)
    issued = await services.tokens.issue_token(user_id, args.token_name)
    print(f"user_id: {user_id}")
    print(f"email:   {args.email.strip().lower()}")
    print()
    print(f"token:   {issued.plaintext}")
    print()
    print("上面这串 token 只显示这一次，库里只存 sha256，丢了只能重新签发。")
    print("写进客户端的 MCP 配置：")
    print()
    print(f'  claude mcp add --transport http kb {args.base_url}/mcp \\')
    print(f'    --header "Authorization: Bearer {issued.plaintext}"')
    print()
    print("必须走 HTTPS —— bearer token 走明文 HTTP 等于把知识库公开。")
    return 0


async def _token_issue(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    issued = await services.tokens.issue_token(user_id, args.name)
    print(f"token_id: {issued.token_id}")
    print(f"token:    {issued.plaintext}")
    print("只显示这一次。")
    return 0


async def _token_revoke(args: argparse.Namespace) -> int:
    services = build_services()
    revoked = await services.tokens.revoke_token(_parse_uuid(args.token_id, what="--token-id"))
    print("已吊销。" if revoked else "没有找到未吊销的该 token。")
    return 0 if revoked else 1


async def _repo_add(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    repo_id = await services.repos.create_repo(
        user_id,
        url=args.url,
        branch=args.branch,
        credential_ref=args.credential_ref,
        sync_now=not args.no_sync,
    )
    print(f"repo_id: {repo_id}")
    if args.no_sync:
        print("未入队同步；稍后用 `kb sync --repo-id` 或 POST /api/repos/{id}/sync 触发。")
    else:
        print("首次全量同步任务已入队，等 worker 消费。")
    return 0


async def _sync(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    if args.repo_id:
        repo_id = _parse_uuid(args.repo_id, what="--repo-id")
        if await services.repos.get_repo(user_id, repo_id) is None:
            raise SystemExit(f"找不到该 repo：{repo_id}")
        job_id = await services.repos.request_sync(user_id, repo_id)
    else:
        repos = await services.repos.list_repos(user_id)
        if not repos:
            raise SystemExit("该用户还没有配置任何仓库。")
        job_id = 0
        for repo in repos:
            job_id = await services.repos.request_sync(user_id, repo.id)
    print(f"已入队同步任务：job_id={job_id}")
    return 0


async def _status(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    repos = await services.repos.list_repos(user_id)
    jobs = await services.queue.counts_for_user(user_id)
    chunks = await services.index_maintenance.count_chunks(user_id)
    unsearchable = await services.documents.by_status(user_id, ("failed", "no_text"))

    print(f"vector_search_enabled: {services.vector_search_enabled}")
    print(f"chunks: {chunks}")
    print("repos:")
    for repo in repos:
        sha = (repo.last_synced_sha or "-")[:12]
        print(f"  {repo.id}  {repo.branch:<10} {sha:<12} {repo.url}")
    print(f"jobs: {jobs or '{}'}")
    print(f"failed/no_text: {len(unsearchable)}")
    for document in unsearchable[:20]:
        print(f"  [{document.conversion_status}] {document.source_path}")
    return 0


async def _rebuild(args: argparse.Namespace) -> int:
    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    job_id = await services.repos.request_full_rebuild(user_id)
    print(f"全量重建已入队：job_id={job_id}")
    return 0


async def _eval(args: argparse.Namespace) -> int:
    """Run the retrieval-quality set (spec §11.2).

    ``--dry-run`` validates the set without a database, which is what CI can run:
    a malformed or style-uncovered evaluation file is a real defect, and catching
    it needs no embeddings.
    """
    from kb.evaluate import EvalSetError, evaluate, load_cases, summarise_styles

    try:
        cases = load_cases(args.cases)
    except EvalSetError as exc:
        raise SystemExit(f"评估集有问题：{exc}") from exc

    ks = tuple(int(value) for value in args.k.split(",") if value.strip())
    styles = summarise_styles(cases)
    print(f"用例数：{len(cases)}；风格分布：{styles}")
    if len(cases) < 50:
        print("提醒：spec §11.2 要求 50~100 条真实查询，当前样本太少，基线不具代表性。")

    if args.dry_run:
        print("--dry-run：只校验格式，未执行检索。")
        return 0

    services = build_services()
    user_id = (
        _parse_uuid(args.user_id, what="--user-id")
        if args.user_id
        else await _user_id_by_email(services, args.email)
    )
    print(f"向量路可用：{services.vector_search_enabled}")
    report = await evaluate(services.retrieval, cases, user_id=user_id, ks=ks, limit=args.limit)

    print()
    print(report.as_markdown())
    if args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report.as_markdown(), encoding="utf-8")
        path.with_suffix(".json").write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"报告已写入 {path} 与 {path.with_suffix('.json')}")
    return 0


async def _worker(args: argparse.Namespace) -> int:
    from kb.worker import Worker

    services = build_services()
    worker = Worker(services, poll_interval_seconds=args.poll_interval)
    await worker.run_forever()
    return 0


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from kb.app import create_app
    from kb.config import get_settings

    settings = get_settings()
    uvicorn.run(
        create_app(),
        host=args.host or settings.api_host,
        port=args.port or settings.api_port,
        log_level=args.log_level.lower(),
    )
    return 0


async def _user_id_by_email(services, email: str | None) -> uuid.UUID:
    if not email:
        raise SystemExit("需要 --user-id 或 --email 之一。")
    user_id = await services.tokens.find_user_by_email(email)
    if user_id is None:
        raise SystemExit(f"找不到 email 为 {email!r} 的用户。先用 `kb user create` 建一个。")
    return user_id


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kb", description="knowledge-brain 运维 CLI")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    user = subparsers.add_parser("user", help="用户管理")
    user_sub = user.add_subparsers(dest="subcommand", required=True)
    create = user_sub.add_parser("create", help="建用户并签发一个 token")
    create.add_argument("--email", required=True)
    create.add_argument("--token-name", default="default")
    create.add_argument("--base-url", default="https://your-host")
    create.set_defaults(func=_user_create)

    token = subparsers.add_parser("token", help="token 管理")
    token_sub = token.add_subparsers(dest="subcommand", required=True)
    issue = token_sub.add_parser("issue", help="为已有用户再签发一个 token")
    issue.add_argument("--user-id")
    issue.add_argument("--email")
    issue.add_argument("--name", default="default")
    issue.set_defaults(func=_token_issue)
    revoke = token_sub.add_parser("revoke", help="吊销一个 token")
    revoke.add_argument("--token-id", required=True)
    revoke.set_defaults(func=_token_revoke)

    repo = subparsers.add_parser("repo", help="仓库管理")
    repo_sub = repo.add_subparsers(dest="subcommand", required=True)
    add = repo_sub.add_parser("add", help="注册一个 Git 仓库")
    add.add_argument("--url", required=True)
    add.add_argument("--branch", default="main")
    add.add_argument("--credential-ref", help="存放 token 的环境变量名；明文不落库")
    add.add_argument("--user-id")
    add.add_argument("--email")
    add.add_argument("--no-sync", action="store_true", help="只登记，不立即入队同步")
    add.set_defaults(func=_repo_add)

    sync = subparsers.add_parser("sync", help="手动触发同步")
    sync.add_argument("--repo-id")
    sync.add_argument("--user-id")
    sync.add_argument("--email")
    sync.set_defaults(func=_sync)

    status = subparsers.add_parser("status", help="查看同步位置、队列深度、不可检索文件")
    status.add_argument("--user-id")
    status.add_argument("--email")
    status.set_defaults(func=_status)

    rebuild = subparsers.add_parser("rebuild", help="清空索引并全量重建")
    rebuild.add_argument("--user-id")
    rebuild.add_argument("--email")
    rebuild.set_defaults(func=_rebuild)

    worker = subparsers.add_parser("worker", help="运行队列消费者")
    worker.add_argument("--poll-interval", type=float, default=None, help="队列为空时的轮询间隔（秒）")
    worker.set_defaults(func=_worker)

    evaluate_parser = subparsers.add_parser("eval", help="跑检索质量评估集（Recall@k / MRR）")
    evaluate_parser.add_argument("--cases", required=True, help="JSONL 或 JSON 评估集路径")
    evaluate_parser.add_argument("--k", default="5,10", help="逗号分隔的 k，例如 5,10")
    evaluate_parser.add_argument("--limit", type=int, default=None, help="覆盖每次检索的召回上限")
    evaluate_parser.add_argument("--out", default=None, help="把 Markdown/JSON 报告写到这个路径")
    evaluate_parser.add_argument("--dry-run", action="store_true", help="只校验评估集格式，不检索")
    evaluate_parser.add_argument("--user-id")
    evaluate_parser.add_argument("--email")
    evaluate_parser.set_defaults(func=_eval)

    serve = subparsers.add_parser("serve", help="运行 API + MCP 服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.set_defaults(func=_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.log_level)
    if asyncio.iscoroutinefunction(args.func):
        return asyncio.run(args.func(args))
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover - process entry
    sys.exit(main())
