"""CLI 单入口。

    python -m app.wiki ingest knowledge/我的简历.md
    python -m app.wiki query "讲一下你的项目"
    python -m app.wiki lint

所有命令都要用项目自己的解释器：
    .venv\\Scripts\\python.exe -m app.wiki …
PATH 上的裸 python 指向另一个环境（hermes 的 venv），依赖完全不对。
"""
import argparse
import asyncio
import sys


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.wiki", description="LLM Wiki 命令行")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="摄入一份原始文档，编译成 Wiki 页面")
    ing.add_argument("source", help="knowledge/ 下的文件名，如 knowledge/我的简历.md")
    ing.add_argument("--force", action="store_true",
                     help="忽略增量判定，全部页面整页重编（不只是绕过文档级跳过）")
    ing.add_argument("--dry", action="store_true", help="只估算与判定，不调用模型")
    ing.add_argument("--no-merge", dest="no_merge", action="store_true",
                     help="只写摘要页，不动实体/概念/总览（假矛盾误报时的保底开关）")
    ing.add_argument("--allow-dangling", dest="allow_dangling", action="store_true",
                     help="保留悬空 [[ ]] 引用，不降级为纯文本")
    ing.add_argument("--no-reasoning", dest="no_reasoning", action="store_true",
                     help="不把编译推理写进 log.md")

    q = sub.add_parser("query", help="基于 Wiki 回答一个问题")
    q.add_argument("question", help="要问的问题")
    q.add_argument("--pages", help="手工指定要读的页面，逗号分隔（默认全量）")
    q.add_argument("--show-pages", dest="show_pages", action="store_true",
                   help="只列出会喂给模型的页面，不生成回答")

    sub.add_parser("lint", help="静态检查 wiki/")
    return p


def _report_ingest(res) -> None:
    from app.config import settings

    if res.skipped:
        print(f"跳过：{res.source} —— {res.reason}（零 token，{res.elapsed:.2f}s）")
        return
    if res.reason == "dry":
        return

    fmt = lambda xs: "、".join(xs) if xs else "（无）"      # noqa: E731
    print()
    print(f"入库 {res.source}")
    print(f"  新建      {fmt(res.created)}")
    print(f"  更新      {fmt(res.updated)}")
    print(f"  跳过      {fmt(res.untouched)}")
    if res.dead:
        print(f"  死链降级  {fmt(res.dead)}")
    u = res.usage or {}
    if u:
        print(f"  用量      {u.get('input_tokens', '?')} 进 / {u.get('output_tokens', '?')} 出 tokens")
    print(f"  用时      {res.elapsed:.1f}s   模型 {settings.ingest_model}")
    if res.gaps:
        print("  知识空白  " + "；".join(res.gaps))
    for w in res.warnings:
        print(f"  [告警] {w}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.cmd == "lint":
        from app.wiki import lint
        return lint.report(lint.run())

    if args.cmd == "query":
        from app.wiki import query
        return query.run(args.question, pages=args.pages, show_pages=args.show_pages)

    # 三类失败都要接住，甩 traceback 对使用者没有任何价值：
    # CompileError 是编译流程本身的问题（截断、没有 pages）；
    # ProtocolError 是模型输出的路径不合法（这条是故意硬失败的，见 protocol.py）；
    # CommitError 是落盘前校验没过。三者都是「一个字都没写」。
    from app.wiki import compiler, protocol, store

    try:
        res = asyncio.run(compiler.ingest(
            args.source,
            force=args.force,
            dry=args.dry,
            no_merge=args.no_merge,
            allow_dangling=args.allow_dangling,
            show_reasoning=not args.no_reasoning,
        ))
    except compiler.CompileError as exc:
        print(f"编译失败：{exc}", file=sys.stderr)
        return 1
    except protocol.ProtocolError as exc:
        print(f"编译失败：模型输出的文件路径不合法（{exc}）。本次没有写盘。",
              file=sys.stderr)
        return 1
    except store.CommitError as exc:
        print(f"编译失败：{exc}", file=sys.stderr)
        print("本次没有写盘。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断。本次没有写盘。", file=sys.stderr)
        return 130

    _report_ingest(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
