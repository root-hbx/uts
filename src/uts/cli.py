"""uts command line entry point."""

from __future__ import annotations

import argparse
import shlex
import sys

from .conn import DEFAULT_EXEC_TIMEOUT, DEFAULT_MAX_BYTES, DEFAULT_MAX_LINES, Limits
from .inventory import InventoryError, load_inventory, select
from .commands.pull import DEFAULT_MAX_SIZE as PULL_DEFAULT_MAX_SIZE
from .commands.push import DEFAULT_MAX_SIZE as PUSH_DEFAULT_MAX_SIZE
from .output import DEFAULT_MAX_COLS, EXIT_ALL_FAILED, EXIT_BLOCKED

EPILOG = """\
choosing hosts -- everything that touches the network takes one of these:
  -H NAME          one host, by its "name" in hosts.json
  -H a,b -H c      several: repeat the flag, or separate with commas
  -a               all of them
  uts hosts        what the names are

typical flow -- look around, narrow down, then fetch:
  uts status -a                                 are the machines alive
  uts ls -H a '~/data/'                         how many, how big, what types
  uts peek -H a '~/data/*.csv'                  right shape? several runs mixed?
  uts find -H a '~/data/' --name '*.log' --since 24h
  uts pull -H a '~/data/*.csv' --dry-run        see what would be fetched
  uts pull -H a '~/data/*.csv'                  fetch into .uts/ and record it

the other direction:
  uts push -H a ./setup.sh ./lib --to '~/bin/'  send files out; --force to overwrite

running things -- the command is always one quoted string:
  uts exec -H a 'nvidia-smi'                    one command, and you wait for it
  uts exec -H a 'ls ~/data/*.csv | wc -l'       pipes and globs, run on that side
  uts exec -H a -s build 'cd ~/proj'            -s carries cwd and exports forward
  uts start -H a -s train 'python train.py'     same name, now in the background
  uts ps -a                                     what each session is doing
  uts logs -H a -s train                        its output so far; --tail N for more
  uts stop -H a -s train                        -a with no -s stops all of them
  uts stop -a --clean                           the same, and forget them all

Quote path specs and commands in single quotes: `~`, `*` and `|` are for the remote
shell, not this one.
"""


def _targeting() -> argparse.ArgumentParser:
    """`-H` / `-a`, shared by every subcommand that opens a connection.

    Carried on a parent parser rather than a positional: a path and a host name are
    both bare words, and `uts ls data` should never have to be guessed at. Neither
    flag is an error -- see inventory.select.
    """
    p = argparse.ArgumentParser(add_help=False)
    g = p.add_argument_group("hosts")
    g.add_argument(
        "-H", "--host", action="append", metavar="NAME",
        help='host "name" from hosts.json; repeat or comma-separate for several',
    )
    g.add_argument("-a", "--all", action="store_true", help="every host in the inventory")
    return p


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uts",
        description="Inspect and analyse logs and data on other machines over SSH",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--inventory", metavar="PATH", help="host inventory file, default ./hosts.json"
    )
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

    target = _targeting()
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("hosts", help="list the inventory (no network)")

    sub.add_parser(
        "status", parents=[target],
        help="reachability, machine profile, clock skew",
    )

    sp_ls = sub.add_parser(
        "ls", parents=[target],
        help="summarise a directory or glob: count, size, types, age",
    )
    sp_ls.add_argument("path", help="directory or glob -- wrap it in single quotes")

    sp_find = sub.add_parser(
        "find", parents=[target], help="filter files by name/time/size and list them"
    )
    sp_find.add_argument("path")
    sp_find.add_argument("--name", metavar="GLOB", help="filename filter, e.g. '*.log'")
    sp_find.add_argument("--since", metavar="AGE", help="only newer than, e.g. 2h / 7d (file mtime)")
    sp_find.add_argument("--min-size", metavar="SIZE", help="minimum size, e.g. 1M")
    sp_find.add_argument("--limit", type=int, default=40, help="max entries listed, default 40")
    sp_find.add_argument(
        "--sort", choices=("time", "size"), default="time", help="sort order, default time"
    )

    sp_peek = sub.add_parser(
        "peek", parents=[target],
        help="inspect structure without fetching: line/column/delimiter profile + a sample",
        description="Profiles a batch of files by default -- only side-by-side comparison "
                    "reveals that several runs are mixed together.",
    )
    sp_peek.add_argument("path")
    sp_peek.add_argument("-n", "--lines", type=int, default=5, help="sample lines, default 5")
    sp_peek.add_argument("--max-files", type=int, default=500, help="files to probe, default 500")

    sp_pull = sub.add_parser(
        "pull", parents=[target],
        help="fetch remote files into .uts/ (single remote tar+gzip stream)",
        description="Narrow with ls/peek first. Local paths mirror remote ones and every "
                    "fetch is recorded in the manifest.",
    )
    sp_pull.add_argument("path")
    sp_pull.add_argument(
        "--to", metavar="DIR",
        help="local destination, default ./.uts; files keep their "
             "<host>/<remote path> layout underneath it, and the manifest stays in .uts",
    )
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
        "push", parents=[target],
        help="copy local files to the selected hosts (single tar+gzip stream)",
        description="The mirror of pull: local sources, then --to for the remote "
                    "destination directory (uts push -H a ./setup.sh ./lib --to '~/bin/'). "
                    "A source directory is reproduced under the destination by its own "
                    "name. Existing remote files are never overwritten without --force.",
    )
    sp_push.add_argument(
        "srcs", nargs="+", metavar="SRC", help="one or more local files or directories",
    )
    sp_push.add_argument(
        "--to", metavar="DIR", required=True,
        help="remote destination directory -- wrap it in single quotes",
    )
    sp_push.add_argument(
        "--force", action="store_true", help="overwrite remote files that already exist"
    )
    sp_push.add_argument("--dry-run", action="store_true", help="report what would be sent")
    sp_push.add_argument(
        "--max-size", default=PUSH_DEFAULT_MAX_SIZE, metavar="SIZE",
        help=f"total size limit, default {PUSH_DEFAULT_MAX_SIZE}",
    )

    sp_exec = sub.add_parser(
        "exec", parents=[target],
        help="run one command on the selected hosts",
        description="Run one command concurrently. The command is one quoted string, handed "
                    "to the remote shell as-is -- so pipes, redirection and globs work the way "
                    "they read (uts exec -H a 'ls ~/data/*.csv | wc -l'). Destructive commands "
                    "are blocked unless --write.",
    )
    sp_exec.add_argument(
        "--write", action="store_true", help="allow destructive commands",
    )
    sp_exec.add_argument(
        "-s", "--session", metavar="NAME",
        help="carry cwd and exported variables over from earlier commands in this "
             "session; without it every command starts from a clean login",
    )
    sp_exec.add_argument(
        "--pty", action="store_true",
        help="attach a terminal, so programs that check for one behave normally; "
             "note this merges stderr into stdout",
    )
    sp_exec.add_argument(
        "--duration", type=float, metavar="S",
        help="with --pty: let a full-screen program (btop, htop) paint for S seconds, "
             "then return the screen it drew as text",
    )
    # dest is `command_`: the subparsers already own `command` as the subcommand name.
    sp_exec.add_argument(
        "command_", metavar="COMMAND", nargs="*",
        help="the command, as one quoted string: uts exec -a 'ls -la /var/log'",
    )

    sub.add_parser(
        "shell", parents=[target],
        help="open an interactive terminal on one host (for a person, not an agent)",
        description="Needs a real terminal and exactly one host. For scripted use, "
                    "uts exec --pty gives the same terminal without the interaction.",
    )

    sp_start = sub.add_parser(
        "start", parents=[target],
        help="run a command in the background, under a name you choose",
        description="One quoted string, same as uts exec. The command outlives this call "
                    "and the connection that made it. "
                    "The session name is the handle for everything afterwards: "
                    "uts ps, uts logs -s NAME, uts stop -s NAME. If that session "
                    "already carries a cwd and exports from uts exec -s NAME, the job "
                    "inherits them.",
    )
    sp_start.add_argument(
        "-s", "--session", metavar="NAME", required=True,
        help="name for this piece of work; one running job per session",
    )
    sp_start.add_argument(
        "--write", action="store_true", help="allow destructive commands",
    )
    sp_start.add_argument(
        "--force", action="store_true",
        help="reuse a session name whose last run has finished, discarding its log",
    )
    sp_start.add_argument(
        "command_", metavar="COMMAND", nargs="*",
        help="the command, as one quoted string: uts start -H a -s train 'python train.py'",
    )

    sp_ps = sub.add_parser(
        "ps", parents=[target],
        help="what each session is doing: running / exited / idle, and where",
        description="One table for both halves of a session: the background job on "
                    "the remote side, and the cwd and exports recorded for it here. "
                    "A session with state but nothing running shows as idle.",
    )
    sp_ps.add_argument("-s", "--session", metavar="NAME", help="only this session")

    sp_logs = sub.add_parser("logs", parents=[target], help="show a session's output")
    sp_logs.add_argument("-s", "--session", metavar="NAME", required=True)
    sp_logs.add_argument("--tail", type=int, default=50, help="last N lines, default 50")

    sp_stop = sub.add_parser(
        "stop", parents=[target], help="stop a running session, and optionally forget it",
        description="-s NAME is one session, its absence is every session on the "
                    "selected hosts, and --clean is how far to go: stopping keeps the "
                    "log, because how a run ended is usually why you stopped it, so a "
                    "name is only freed by --clean.",
    )
    sp_stop.add_argument(
        "-s", "--session", metavar="NAME",
        help="one session; without it, every session on the selected hosts",
    )
    sp_stop.add_argument(
        "--force", action="store_true", help="SIGKILL instead of SIGTERM"
    )
    sp_stop.add_argument(
        "--clean", action="store_true",
        help="forget it afterwards, freeing the name: the job directory on the host "
             "and the cwd and exports recorded here. Applies at whatever width -s "
             "gave, including sessions that are merely idle",
    )

    sub.add_parser("index", help="rebuild .uts/INDEX.md and print a workspace summary")

    return p


def quote_for_display(command: str) -> str:
    """Wrap a command the way the user would have to type it, for an error message."""
    return f'"{command}"' if "'" in command else f"'{command}'"


def one_command(parts: list[str], subcommand: str) -> str | None:
    """The command `exec` and `start` send is exactly one string, or it is an error.

    One form, not two. The v2 `--` form rejoined argv with shlex.join so the remote
    argv matched the local one; it earned that by being a REMAINDER, which also
    swallowed every uts flag typed after the command -- `uts exec -a 'ls' --pty` ran
    `ls --pty` on the far side and could only warn about it afterwards. A plain
    positional makes exec and start parse like every other subcommand, and what you
    quote is exactly what the remote shell gets, so pipes, redirection and globs need
    no second set of rules.

    The cost is that inner quoting is now yours to write: `-- pgrep -af 'sleep 600'`
    becomes `"pgrep -af 'sleep 600'"`. Which is why the error below hands back the
    rewritten command rather than merely refusing.
    """
    if not parts:
        return ""  # the subcommand turns this into its own usage error
    if len(parts) == 1:
        return parts[0]

    rewritten = quote_for_display(shlex.join(parts))
    print(
        f"uts {subcommand} takes the command as one quoted string, not as separate "
        f"arguments.\n"
        f"      try:  uts {subcommand} ... {rewritten}",
        file=sys.stderr,
    )
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)

    try:
        inventory = load_inventory(args.inventory)
        # These describe what is already on this machine, so there is nothing for
        # -H to narrow.
        if args.command in ("hosts", "index"):
            selected = list(inventory)
        else:
            selected = select(inventory, args.host, args.all)
    except InventoryError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ALL_FAILED

    from .commands import browse, exec_cmd, hosts as hosts_cmd, peek as peek_cmd
    from .commands import pull as pull_cmd, push as push_cmd
    from .commands import sessions as sessions_cmd, status as status_cmd

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

    if args.command == "status":
        return status_cmd.run(selected, args.jobs, args.timeout, args.json)

    if args.command == "exec":
        command = one_command(args.command_, "exec")
        if command is None:
            return EXIT_BLOCKED
        limits = Limits(
            max_lines=args.max_lines, max_bytes=args.max_bytes, timeout=args.timeout
        )
        return exec_cmd.run(
            selected,
            command,
            args.jobs,
            limits,
            args.json,
            args.write,
            args.max_cols,
            args.session,
            args.workspace,
            args.pty,
            args.duration,
        )

    if args.command == "shell":
        from .commands import shell as shell_cmd

        return shell_cmd.run(selected)

    if args.command == "start":
        command = one_command(args.command_, "start")
        if command is None:
            return EXIT_BLOCKED
        limits = Limits(
            max_lines=args.max_lines, max_bytes=args.max_bytes, timeout=args.timeout
        )
        return sessions_cmd.run_start(
            selected, command, args.session, args.jobs, limits,
            args.json, args.write, args.force, args.workspace,
        )

    if args.command == "ps":
        return sessions_cmd.run_ps(
            selected, args.jobs, args.timeout, args.json, args.session, args.workspace,
        )

    if args.command == "logs":
        limits = Limits(
            max_lines=args.max_lines, max_bytes=args.max_bytes, timeout=args.timeout
        )
        return sessions_cmd.run_logs(
            selected, args.session, args.jobs, limits, args.json, args.tail, args.max_cols
        )

    if args.command == "stop":
        return sessions_cmd.run_stop(
            selected, args.session, args.jobs, args.timeout, args.json, args.force,
            args.clean, args.workspace,
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
            args.max_size, args.since, args.lines, args.dry_run, args.workspace, args.to,
        )

    if args.command == "push":
        return push_cmd.run(
            selected, args.srcs, args.to, args.jobs, args.timeout, args.json,
            args.force, args.dry_run, args.max_size, args.workspace,
        )

    raise AssertionError(f"unhandled subcommand {args.command!r}")


if __name__ == "__main__":
    sys.exit(main())
