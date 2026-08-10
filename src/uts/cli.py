"""uts command line entry point."""

from __future__ import annotations

import argparse
import shlex
import sys

from .conn import DEFAULT_EXEC_TIMEOUT, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, Limits
from .inventory import InventoryError, load_inventory, select
from .commands.pull import DEFAULT_MAX_SIZE as PULL_DEFAULT_MAX_SIZE
from .commands.push import DEFAULT_MAX_SIZE as PUSH_DEFAULT_MAX_SIZE
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

the other direction:
  uts push @gpu ./setup.sh ./lib '~/bin/'       send files out; --force to overwrite

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
        description="Run one command concurrently. Destructive commands are blocked unless "
                    "--write. Two forms: after `--` each argument is reproduced on the remote "
                    "side exactly as typed (uts exec test -- pgrep -af 'sleep 600'); a single "
                    "quoted string is handed to the remote shell as-is, which is what you want "
                    "for pipes, redirection and globs (uts exec test 'ls ~/data/*.csv | wc -l').",
    )
    sp_exec.add_argument("selector", nargs="?", default="all")
    sp_exec.add_argument(
        "--write", action="store_true",
        help="allow destructive commands; accepted before or after the selector",
    )
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

    sp_push = sub.add_parser(
        "push",
        help="copy local files to the selected hosts (single tar+gzip stream)",
        description="The mirror of pull, and it reads like cp: every path but the last is "
                    "local, the last one is the remote destination directory "
                    "(uts push @gpu ./setup.sh ./lib '~/bin/'). A source directory is "
                    "reproduced under the destination by its own name. Existing remote "
                    "files are never overwritten without --force.",
    )
    sp_push.add_argument("selector")
    sp_push.add_argument(
        "paths", nargs="+", metavar="SRC ... DEST",
        help="one or more local sources, then the remote destination directory",
    )
    sp_push.add_argument(
        "--force", action="store_true", help="overwrite remote files that already exist"
    )
    sp_push.add_argument("--dry-run", action="store_true", help="report what would be sent")
    sp_push.add_argument(
        "--max-size", default=PUSH_DEFAULT_MAX_SIZE, metavar="SIZE",
        help=f"total size limit, default {PUSH_DEFAULT_MAX_SIZE}",
    )

    sub.add_parser("index", help="rebuild .uts/INDEX.md and print a workspace summary")

    return p


# exec's own flags, mapped to whether each one takes a value. argparse.REMAINDER
# swallows everything after the selector, flags included, so every flag added to
# `exec` has to be listed here too — otherwise it silently becomes the first word
# of the remote command.
EXEC_OWN_FLAGS = ("--write",)


def split_exec_argv(argv: list[str], separator_used: bool = False) -> tuple[bool, str]:
    """Split `exec`'s REMAINDER into (write, command).

    argparse.REMAINDER swallows everything after the selector, flags included, so
    `uts exec test --write -- rm x` used to send `--write` to the remote shell and
    silently leave write mode off. Leading flags before the command are therefore
    hoisted back out here. Anything after an explicit `--` belongs to the remote
    command and is never touched, so `uts exec test -- mytool --write` still works.

    Quoting is the other half. Joining the tokens with a plain space loses the
    quotes the local shell already stripped, so `-- pgrep -af 'sleep 600'` arrives
    as two arguments and fails. shlex.join reproduces the local argv verbatim on
    the far side. The single-token form stays untouched: it is a shell snippet, and
    quoting it would turn `'a | b'` into the name of a program to run.

    `separator_used` has to be supplied by the caller from the untouched argv:
    argparse eats the `--` in `exec test -- rm x` but leaves it in
    `exec test --write -- rm x`, so by this point it is no longer a reliable signal
    of what the user typed.
    """
    write = False
    i = 0
    while i < len(argv) and argv[i] in EXEC_OWN_FLAGS:
        write = True  # only --write lives in EXEC_OWN_FLAGS today
        i += 1
    if i < len(argv) and argv[i] == "--":
        separator_used = True
        i += 1

    parts = argv[i:]
    # Buried in the middle with no `--` in sight, `--write` is far more likely to be
    # a misplaced uts flag than an argument the remote program wants. Guessing either
    # way would be wrong sometimes, so it is passed through with a note.
    if not separator_used and any(flag in parts for flag in EXEC_OWN_FLAGS):
        leaked = [flag for flag in EXEC_OWN_FLAGS if flag in parts]
        print(
            f"note: {', '.join(leaked)} was sent to the remote command, not read by uts.\n"
            f"      Put it before the command: uts exec {leaked[0]} <selector> ...",
            file=sys.stderr,
        )

    if len(parts) == 1:
        return write, parts[0]
    return write, shlex.join(parts)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw_argv)

    try:
        inventory = load_inventory(args.hosts)
        selected = select(inventory, getattr(args, "selector", "all"))
    except InventoryError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ALL_FAILED

    from .commands import browse, exec_cmd, hosts as hosts_cmd, peek as peek_cmd
    from .commands import ping as ping_cmd, pull as pull_cmd, push as push_cmd

    if args.command == "index":
        from .workspace import Workspace, plural

        ws = Workspace(args.workspace)
        print(f"workspace index rebuilt: {ws.write_index()}")
        entries = ws.latest_per_file()
        hosts_seen = len({host for host, _, _ in entries})
        pushed = sum(1 for _, direction, _ in entries if direction == "push")
        summary = f"{plural(len(entries) - pushed, 'file')} pulled"
        if pushed:
            summary += f", {plural(pushed, 'file')} pushed"
        print(f"{summary} across {plural(hosts_seen, 'host')}")
        return 0

    if args.command == "hosts":
        return hosts_cmd.run(selected, args.json)

    if args.command == "ping":
        return ping_cmd.run(selected, args.jobs, args.timeout, args.json)

    if args.command == "exec":
        limits = Limits(
            max_lines=args.max_lines, max_bytes=args.max_bytes, timeout=args.timeout
        )
        hoisted_write, command = split_exec_argv(args.argv, separator_used="--" in raw_argv)
        return exec_cmd.run(
            selected,
            command,
            args.jobs,
            limits,
            args.json,
            args.write or hoisted_write,
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

    if args.command == "push":
        return push_cmd.run(
            selected, args.paths, args.jobs, args.timeout, args.json,
            args.force, args.dry_run, args.max_size, args.workspace,
        )

    raise AssertionError(f"unhandled subcommand {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
