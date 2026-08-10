"""uts command line entry point."""

from __future__ import annotations

import argparse
import sys

from .conn import DEFAULT_EXEC_TIMEOUT, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, Limits
from .inventory import InventoryError, load_inventory, select
from .commands.pull import DEFAULT_MAX_SIZE as PULL_DEFAULT_MAX_SIZE
from .output import DEFAULT_MAX_COLS, EXIT_ALL_FAILED

EPILOG = """\
selectors:
  all              every host (default)
  test             by host name or IP
  a,b              several hosts
  @prod            by tag
  192.168.3.*      glob over name or IP

typical flow -- look around, narrow down, then fetch:
  uts ping all                                  are the machines alive
  uts ls test '~/data/'                         how many, how big, what types
  uts peek test '~/data/*.csv'                  right shape? several runs mixed?
  uts find test '~/data/' --name '*.log' --since 24h
  uts pull test '~/data/*.csv' --dry-run        see what would be fetched
  uts pull test '~/data/*.csv'                  fetch into .uts/ and record it

Quote path specs in single quotes: `~` and `*` are expanded by the remote shell.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uts",
        description="Inspect and analyse logs and data on other machines over SSH",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--hosts", metavar="PATH", help="host inventory, default ./hosts.json")
    p.add_argument("--jobs", "-j", type=int, default=8, metavar="N", help="concurrency, default 8")
    p.add_argument(
        "--timeout", type=float, default=DEFAULT_EXEC_TIMEOUT, metavar="S",
        help=f"per-command timeout in seconds, default {DEFAULT_EXEC_TIMEOUT:g}",
    )
    p.add_argument(
        "--max-lines", type=int, default=DEFAULT_MAX_LINES, metavar="N",
        help=f"max lines shown per host, default {DEFAULT_MAX_LINES}",
    )
    p.add_argument(
        "--max-bytes", type=int, default=DEFAULT_MAX_BYTES, metavar="N",
        help=f"max bytes shown per host, default {DEFAULT_MAX_BYTES}",
    )
    p.add_argument(
        "--max-cols", type=int, default=DEFAULT_MAX_COLS, metavar="N",
        help=f"max chars per line, 0 disables, default {DEFAULT_MAX_COLS}",
    )
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--workspace", metavar="DIR", help="local workspace, default ./.uts")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("hosts", help="list the inventory (no network)")

    sp_ping = sub.add_parser("ping", help="reachability, machine profile, clock skew")
    sp_ping.add_argument("selector", nargs="?", default="all")

    sp_exec = sub.add_parser(
        "exec",
        help="run one command on the selected hosts",
        description="Run one command concurrently. Destructive commands are blocked unless --write.",
    )
    sp_exec.add_argument("selector", nargs="?", default="all")
    sp_exec.add_argument("--write", action="store_true", help="allow destructive commands")
    sp_exec.add_argument(
        "argv", nargs=argparse.REMAINDER,
        help="the command to run; separate it with --, e.g. uts exec all -- ls -la",
    )

    sp_ls = sub.add_parser("ls", help="summarise a directory or glob: count, size, types, age")
    sp_ls.add_argument("selector")
    sp_ls.add_argument("path", help="directory or glob -- wrap it in single quotes")

    sp_find = sub.add_parser("find", help="filter files by name/time/size and list them")
    sp_find.add_argument("selector")
    sp_find.add_argument("path")
    sp_find.add_argument("--name", metavar="GLOB", help="filename filter, e.g. '*.log'")
    sp_find.add_argument("--since", metavar="AGE", help="only newer than, e.g. 2h / 7d (file mtime)")
    sp_find.add_argument("--min-size", metavar="SIZE", help="minimum size, e.g. 1M")
    sp_find.add_argument("--limit", type=int, default=40, help="max entries listed, default 40")
    sp_find.add_argument(
        "--sort", choices=("time", "size"), default="time", help="sort order, default time"
    )

    sp_peek = sub.add_parser(
        "peek",
        help="inspect structure without fetching: line/column/delimiter profile + a sample",
        description="Profiles a batch of files by default -- only side-by-side comparison "
                    "reveals that several runs are mixed together.",
    )
    sp_peek.add_argument("selector")
    sp_peek.add_argument("path")
    sp_peek.add_argument("-n", "--lines", type=int, default=5, help="sample lines, default 5")
    sp_peek.add_argument("--max-files", type=int, default=500, help="files to probe, default 500")

    sp_pull = sub.add_parser(
        "pull",
        help="fetch remote files into .uts/ (single remote tar+gzip stream)",
        description="Narrow with ls/peek first. Local paths mirror remote ones and every "
                    "fetch is recorded in the manifest.",
    )
    sp_pull.add_argument("selector")
    sp_pull.add_argument("path")
    sp_pull.add_argument("--since", metavar="AGE", help="only newer than, e.g. 24h (file mtime)")
    sp_pull.add_argument(
        "--max-size", default=PULL_DEFAULT_MAX_SIZE, metavar="SIZE",
        help=f"total size limit, default {PULL_DEFAULT_MAX_SIZE}",
    )
    sp_pull.add_argument(
        "--lines", type=int, metavar="N", help="first N lines of each file (max 50 files)"
    )
    sp_pull.add_argument("--dry-run", action="store_true", help="report what would be fetched")

    sub.add_parser("index", help="rebuild .uts/INDEX.md and print a workspace summary")

    return p


def _command_from_argv(argv: list[str]) -> str:
    """Accepts both `-- ls -la` and `'ls -la'`."""
    parts = argv[1:] if argv and argv[0] == "--" else argv
    return " ".join(parts)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        inventory = load_inventory(args.hosts)
        selected = select(inventory, getattr(args, "selector", "all"))
    except InventoryError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ALL_FAILED

    from .commands import browse, exec_cmd, hosts as hosts_cmd, peek as peek_cmd
    from .commands import ping as ping_cmd, pull as pull_cmd

    if args.command == "index":
        from .workspace import Workspace, plural

        ws = Workspace(args.workspace)
        print(f"workspace index rebuilt: {ws.write_index()}")
        entries = ws.latest_per_file()
        hosts_seen = len({h for h, _ in entries})
        print(f"{plural(len(entries), 'file')} from {plural(hosts_seen, 'host')}")
        return 0

    if args.command == "hosts":
        return hosts_cmd.run(selected, args.json)

    if args.command == "ping":
        return ping_cmd.run(selected, args.jobs, args.timeout, args.json)

    if args.command == "exec":
        limits = Limits(
            max_lines=args.max_lines, max_bytes=args.max_bytes, timeout=args.timeout
        )
        return exec_cmd.run(
            selected,
            _command_from_argv(args.argv),
            args.jobs,
            limits,
            args.json,
            args.write,
            args.max_cols,
        )

    if args.command == "ls":
        return browse.run_ls(selected, args.path, args.jobs, args.timeout, args.json)

    if args.command == "find":
        return browse.run_find(
            selected, args.path, args.jobs, args.timeout, args.json,
            args.name, args.since, args.min_size, args.limit, args.sort,
        )

    if args.command == "peek":
        return peek_cmd.run(
            selected, args.path, args.jobs, args.timeout, args.json,
            args.lines, args.max_cols, args.max_files,
        )

    if args.command == "pull":
        return pull_cmd.run(
            selected, args.path, args.jobs, args.timeout, args.json,
            args.max_size, args.since, args.lines, args.dry_run, args.workspace,
        )

    raise AssertionError(f"unhandled subcommand {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
