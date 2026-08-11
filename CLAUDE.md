# Working on uts

`uts` is a local CLI that gives an AI agent reach over other machines on the subnet
via one-shot SSH. **Nothing is installed on the remote side** — that promise shapes
every design decision below, so check it before proposing anything resident.

If you want to *use* uts rather than change it, read `.claude/skills/uts/SKILL.md`
instead; this file is about the code.

## Running things

```bash
.venv/bin/python -m pytest tests/ -q          # 295 unit tests
.venv/bin/python -m pytest tests/ -q -m live  # 56 live tests, needs a reachable host
./uts --help
```

**Use `.venv/bin/python -m pytest`, not `uv run pytest`.** Both `uv run pytest` and
`uv run --project . pytest` resolve to the anaconda python on this machine and fail
with `ModuleNotFoundError: No module named 'uts'`.

Live tests are opt-in (`pyproject.toml` sets `addopts = "-m 'not live'"`) and target
the host literally named `test` in `hosts.json`.

## Map

`./uts` is a `uv run --project` shim, so there is no install step. It resolves
symlinks, because `install-skill` links it onto `PATH`.

- `cli.py` — argparse; the only place argv becomes a remote command string
- `conn.py` — paramiko transport, `Conn.run()` / `stream_stdout()`, `run_many()` fan-out
- `guard.py` — destructive-command blacklist, gated by `--write`
- `inventory.py` — `hosts.json` + `select(hosts, names, all_)`; host **names** only
- `remote.py` — every remote shell snippet, each with a matching pure parser
- `output.py` — rendering, the `--json` envelope, exit codes
- `workspace.py` — `.uts/` layout + append-only provenance manifest
- `session.py` — named `-s` session state (cwd + env delta) in `.uts/sessions/`
- `screen.py` — `strip_ansi`, `render_frame` (pyte) for full-screen programs
- `commands/` — one module per subcommand; `sessions.py` holds `start`/`ps`/`logs`/`stop`

Layering: `conn` / `inventory` / `remote` / `session` / `workspace` / `guard` /
`screen` are **pure** — no `print`, no `sys.exit`, no argparse. All printing happens
in `commands/` and `output.py`. Keep it that way; it is what makes the tool testable
without a network.

## Load-bearing decisions — do not "simplify" these

- **Output caps apply while reading**, not after (`conn.py` `_Capped`). Capping
  afterwards means `cat 2GB.log` lands 2GB in local memory first.
- **`connect()` sets `look_for_keys=False, allow_agent=False`.** With password auth
  paramiko would otherwise try every key in `~/.ssh` and hit `MaxAuthTries` before
  the password is ever offered.
- **`guard.py` is a fumble guard, not a security boundary** — its own docstring says
  so. It exists because one slipped `rm` destroys the data you came to analyse.
- **`exec`/`start` take the command as exactly one quoted string** (`cli.py`
  `one_command`, dest `command_` because the subparsers already own `command`). The
  v2 `--`/`REMAINDER` form is **gone**: a REMAINDER also swallowed every uts flag
  typed *after* the command, so `uts exec -a 'ls' --pty` ran `ls --pty` remotely and
  could only warn afterwards. Do not reintroduce a second form.
- **Globals are registered twice** (`_add_globals`): on the top-level parser, and on
  a parent every subcommand inherits with `default=argparse.SUPPRESS`. Without
  SUPPRESS the subparser's own default overwrites the top-level value and
  `uts --json status` silently returns text.
- **Under `--json` there is exactly one envelope on stdout, on every exit path.**
  Pre-flight refusals go through `output.fail()`, including argparse usage errors via
  `cli._Parser.error`. Never add a `print(..., file=sys.stderr); return EXIT_*` — that
  is the hole the envelope was built to close. Usage errors are `EXIT_BLOCKED`, not
  argparse's 2, because 2 already means "partially unreachable".
- **`ps` only reads; `stop` owns forgetting.** `--clean` says how far to go, never
  what gets stopped — it means the same at both widths, so `stop -a --clean` stops
  running jobs too. Cleaning signals then *waits remotely* in one round trip: SIGTERM
  returns before the job's trap has written `rc`, and a clean arriving in that window
  would see a live pid and refuse, leaving the name taken. Liveness is **`rc` first,
  pid second** — pids get reused.
- **A background job records its own pid (`echo $$`), never `$!`.** `setsid` forks
  when the calling shell is a process-group leader, which it is here, so `$!` would
  be setsid's pid and every later `kill -0` would ask about the wrong process. The
  job also traps SIGTERM to write `rc=143`, which is what lets `ps` say "killed"
  rather than "vanished".
- **A session has two homes and both are cleaned together**: `~/.uts/jobs/<name>/`
  on the host, `.uts/sessions/<name>.json` here. `Session.forget(host)` is per host,
  because a session can be finished on one machine and running on another.
- **Session state is read back via a nonce'd sentinel**, split on the *last*
  occurrence so a command that prints an old transcript cannot forge it. A truncated
  output loses the trailer, and that case must stay loud.
- **PTY merges stderr into stdout**, and `_head_tags` says so in the block header;
  without it an empty stderr reads as "nothing went wrong".
- **Every command runs under `sh -c`** (`remote.posix_wrap`, applied at the four
  `chan.exec_command()` sites in `conn.py`). sshd hands an exec request to the
  *account's login shell*; bash and zsh are near enough to POSIX that the snippets
  mostly worked, but fish is not a POSIX shell and rejects `n=$(...)` outright,
  which reduced `uts status` on a fish host to `clock ?` plus a stderr blurt. The
  wrap sits at that boundary rather than at the end of each builder in `remote.py`
  so that it covers the command the *user* typed too — one dialect on every host is
  the point. It does not lose the login shell's environment: that shell still runs
  and `sh` inherits from it. **`uts shell` is the deliberate exception** — it uses
  `invoke_shell()`, and an interactive terminal should stay the user's own fish.
  The cost is that bash-only syntax no longer works implicitly; `bash -c '...'` is
  the escape hatch, and it now means the same thing everywhere.

## Gotchas

- **A glob that matches nothing is still a hazard, even under `sh`.** POSIX sh leaves
  an unmatched pattern as itself, so `for d in "$root"/*/` iterates once over a
  literal path that does not exist — `uts jobs` returned rc=1 on an empty jobs
  directory for all of v2 (then via zsh, which aborts outright instead). Remote
  snippets iterate with `find … | while IFS= read -r`. Do not simplify it back.
- `uts shell` cannot be tested with plain pytest (it refuses without a tty). The live
  test builds one with `pty.openpty()` + `subprocess`, **not** `pty.fork()` — by then
  the fan-out tests have left threads in the process and forking one can deadlock.
- `pkill -f '<pattern>'` over uts can kill the SSH session itself: that session's own
  `sh -c …` cmdline contains the pattern. Learned the hard way.
- Tests never mock paramiko. The house style is to assert on the shell string a
  builder produced, and to feed a hand-written fake reply to the matching parser.
  Follow it rather than introducing a mock layer.

## Out of scope

Network and system security — plaintext credentials in `hosts.json`,
`AutoAddPolicy`, MITM exposure, the porousness of `guard.py` as a permission system.
This is a LAN-local tool for the author's own machines, and treating it as a security
product would distort the design. Do not append security caveats to unrelated work.
