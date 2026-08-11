"""Build the POSIX shell commands that run on the remote side.

This is the only module that assembles shell strings. Everything foreign goes
through shlex.quote — not for security, for correctness: spaces, `*` and `$` in
a log path silently produce wrong results.

All targets are Linux, so coreutils is used directly with no dialect layer.
"""

from __future__ import annotations

import re
import shlex

# Log-directory probing stops counting here — /var/log with tens of thousands of
# files makes find slow. Hitting this value means "at least this many", so it has
# to be displayed as 500+.
LOGDIR_PROBE_CAP = 500

# Machine profile. One "key\tvalue" per line, sections split by ---name---, so
# parsing never depends on column widths. The 2>/dev/null and || fallbacks matter:
# a failing probe must not take the whole command down.
FACTS = r"""
printf 'epoch\t%s\n' "$(date +%s)"
printf 'time\t%s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
printf 'hostname\t%s\n' "$(hostname 2>/dev/null || uname -n)"
printf 'kernel\t%s\n' "$(uname -sr)"
printf 'arch\t%s\n' "$(uname -m)"
printf 'os\t%s\n' "$( (. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME") || uname -s )"
printf 'cpus\t%s\n' "$(nproc 2>/dev/null || echo '?')"
printf 'mem\t%s\n' "$(free -h 2>/dev/null | awk 'NR==2{print $3"/"$2}')"
printf 'load\t%s\n' "$(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
printf 'uptime\t%s\n' "$(uptime -p 2>/dev/null || uptime)"
printf 'shell\t%s\n' "$SHELL"
echo '---disk---'
df -h -x tmpfs -x devtmpfs -x overlay 2>/dev/null | tail -n +2 | head -8
echo '---logdirs---'
for d in /var/log /var/log/journal /data /opt /srv /home "$HOME"; do
  [ -d "$d" ] || continue
  n=$(find "$d" -maxdepth 3 -type f \( -name '*.log' -o -name '*.log.*' \) 2>/dev/null | head -__CAP__ | wc -l)
  [ "$n" -gt 0 ] && printf '%s\t%s\n' "$d" "$n"
done
exit 0
"""


def facts() -> str:
    return FACTS.replace("__CAP__", str(LOGDIR_PROBE_CAP))


def parse_facts(stdout: str) -> dict:
    """FACTS output -> {kv..., disk: [...], logdirs: [(path, count)]}."""
    out: dict = {"disk": [], "logdirs": []}
    section = "kv"
    for line in stdout.splitlines():
        if line == "---disk---":
            section = "disk"
            continue
        if line == "---logdirs---":
            section = "logdirs"
            continue
        if not line.strip():
            continue
        if section == "disk":
            out["disk"].append(line.rstrip())
        elif section == "logdirs":
            path, _, count = line.partition("\t")
            out["logdirs"].append((path, count.strip()))
        else:
            key, _, value = line.partition("\t")
            out[key] = value.strip()
    return out


def q(value: str) -> str:
    return shlex.quote(value)


def posix_wrap(command: str) -> str:
    """Run `command` under POSIX sh, whatever the account's login shell is.

    sshd hands an exec request to the user's login shell, so without this every
    snippet in this module is parsed by whatever that account happens to use. bash
    and zsh are close enough to POSIX to have mostly worked; fish is not a POSIX
    shell at all and rejects `n=$(...)` outright, which took `uts status` down to
    `clock ?` on a fish host. Applied at the four exec_command() sites in conn.py
    rather than at the end of each builder here, so that it covers the command the
    *user* typed too — one dialect on every host is the whole point.

    This does not discard the user's environment. The login shell still runs
    (`fish -c "sh -c '...'"`), so anything its rc files export is inherited by sh
    as an ordinary child process.

    `uts shell` is deliberately exempt: it uses invoke_shell(), and an interactive
    terminal should be the user's own shell.
    """
    return "sh -c " + q(command)


# ------------------------------------------------------------------ path specs

class PathSpecError(ValueError):
    pass


# The path spec is the one thing deliberately left unquoted — `~` and `*` have to
# reach the remote shell to expand. The cost is that a command could be smuggled
# in, so separators and substitution characters are rejected: they never appear in
# a legitimate path, and when they do it's a slip or a misunderstanding.
_FORBIDDEN = (";", "&", "|", "`", "$(", "\n", ">", "<")


def check_path_spec(spec: str) -> str:
    if not spec.strip():
        raise PathSpecError("path cannot be empty")
    for bad in _FORBIDDEN:
        if bad in spec:
            raise PathSpecError(
                f"{bad!r} is not allowed in a path. The path spec is handed to the remote "
                f"shell verbatim (that's what makes ~ and * work), so command separators "
                f"and substitutions are rejected."
            )
    return spec


# One line per file: "bytes\tmtime(epoch)\tpath".
# Directories recurse, globs are expanded by the shell, single files pass through.
LIST_FILES = r"""
_uts_list() {
  for p in "$@"; do
    if [ -d "$p" ]; then
      find "$p" -type f -printf '%s\t%T@\t%p\n' 2>/dev/null
    elif [ -f "$p" ]; then
      # --printf, not -c: GNU stat's -c does not expand \t and emits a literal backslash-t
      stat --printf='%s\t%Y\t%n\n' "$p" 2>/dev/null
    fi
  done
}
_uts_list __SPEC__ | head -n __CAP__
"""


def list_files(spec: str, cap: int = 5000) -> str:
    return LIST_FILES.replace("__SPEC__", check_path_spec(spec)).replace("__CAP__", str(cap))


def parse_listing(stdout: str) -> list[dict]:
    files = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        try:
            files.append({"size": int(parts[0]), "mtime": float(parts[1]), "path": parts[2]})
        except ValueError:
            continue
    return files


# ------------------------------------------------------------- time and size

_SINCE = re.compile(r"^(\d+)\s*([mhdw])$")
_SINCE_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_since(since: str) -> float:
    """'2h' / '30m' / '7d' / '1w' -> seconds. Filters on file mtime, not line timestamps."""
    m = _SINCE.match(since.strip().lower())
    if not m:
        raise ValueError(f"--since cannot parse {since!r}; write it like 30m / 2h / 7d / 1w")
    return int(m.group(1)) * _SINCE_SECONDS[m.group(2)]


_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgt]?)b?$")
_SIZE_MULT = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}


def parse_size(value: str) -> int:
    m = _SIZE.match(str(value).strip().lower())
    if not m:
        raise ValueError(f"cannot parse size {value!r}; write it like 500K / 10M / 2G")
    return int(float(m.group(1)) * _SIZE_MULT[m.group(2)])


# ------------------------------------------------------------- shape probing

# Count lines and delimiter occurrences across a batch. Statistics only, no content.
# One line per file: "lines\tcommas\ttabs\tsemicolons\tpipes\tpath"
SHAPE = r"""
for f in __FILES__; do
  [ -f "$f" ] || continue
  lines=$(wc -l < "$f" 2>/dev/null || echo 0)
  head -n 1 "$f" 2>/dev/null | awk -v L="$lines" -v F="$f" '
    { s=$0; c=gsub(/,/,"",s); s=$0; t=gsub(/\t/,"",s); s=$0; m=gsub(/;/,"",s); s=$0; p=gsub(/\|/,"",s);
      printf "%s\t%d\t%d\t%d\t%d\t%s\n", L, c, t, m, p, F; exit }
    END { if (NR==0) printf "%s\t0\t0\t0\t0\t%s\n", L, F }'
done
"""


def shape_probe(paths: list[str]) -> str:
    return SHAPE.replace("__FILES__", " ".join(q(p) for p in paths))


def parse_shape(stdout: str) -> list[dict]:
    out = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6:
            continue
        try:
            lines_, comma, tab, semi, pipe, path = (
                int(parts[0]), int(parts[1]), int(parts[2]),
                int(parts[3]), int(parts[4]), parts[5],
            )
        except ValueError:
            continue
        counts = {",": comma, "\t": tab, ";": semi, "|": pipe}
        delim, n = max(counts.items(), key=lambda kv: kv[1])
        out.append({
            "path": path,
            "lines": lines_,
            "delimiter": delim if n > 0 else None,
            "columns": n + 1 if n > 0 else 1,
        })
    return out


# ----------------------------------------------------------------- transfers

def tar_stream(paths: list[str]) -> str:
    """Emit these files as one gzip stream on stdout.

    Logs and numeric matrices compress hard (7.2MB of delay matrices came back as
    182KB), so per-file SFTP wastes both time and bandwidth.

    Leading slashes are stripped and paired with `-C /`, so the archive holds clean
    relative paths that unpack under `.uts/<host>/` as a mirror of the remote
    tree — and tar's "Removing leading /" warning never pollutes stderr.
    """
    listing = "\n".join(p.lstrip("/") for p in paths)
    return f"printf '%s\\n' {q(listing)} | tar czf - -C / --no-recursion -T -"


def head_file(path: str, lines: int) -> str:
    return f"head -n {int(lines)} {q(path)}"


# A push destination is one directory, not a pattern: expanding a glob would leave
# "which of the matches did it mean" unanswerable, and silently picking the first is
# the kind of guess that loses files.
_DEST_GLOB = set("*?[")


def check_dest(dest: str) -> str:
    check_path_spec(dest)
    if _DEST_GLOB & set(dest):
        raise PathSpecError(
            f"{dest!r} looks like a glob. A push destination has to be a single "
            f"directory — `~` still expands, `*` does not."
        )
    return dest


def push_probe(dest: str, members: list[str]) -> str:
    """Resolve dest and report which members are already there.

    dest is left unquoted so `~` expands, the same rule as every other path spec in
    this module; the member names come off the local filesystem and are quoted.
    Emits "dest\t<resolved>", optionally "notdir\t<path>", then "exists\t<member>".
    """
    listing = " ".join(q(m) for m in members)
    return f"""
d={check_dest(dest)}
case "$d" in */) d=${{d%/}} ;; esac
[ -n "$d" ] || d=.
printf 'dest\\t%s\\n' "$d"
if [ -e "$d" ] && [ ! -d "$d" ]; then
  printf 'notdir\\t%s\\n' "$d"
  exit 0
fi
for p in {listing}; do
  [ -e "$d/$p" ] && printf 'exists\\t%s\\n' "$p"
done
exit 0
"""


def parse_push_probe(stdout: str) -> dict:
    out: dict = {"dest": None, "notdir": None, "exists": []}
    for line in stdout.splitlines():
        key, _, value = line.partition("\t")
        if key == "dest":
            out["dest"] = value
        elif key == "notdir":
            out["notdir"] = value
        elif key == "exists":
            out["exists"].append(value)
    return out


# ----------------------------------------------------------------------- sessions

# Everything a background session leaves behind lives here. It is the only footprint
# uts has on the far side, it is plain files rather than anything running, and
# `uts ps --clean` removes it.
JOBS_ROOT = '"$HOME/.uts/jobs"'


def start_job(
    name: str, command: str, prefix: list[str] | None = None, force: bool = False
) -> str:
    """Launch a command detached and report the pid it is actually running under.

    The pid is recorded by the job itself (`echo $$`) rather than taken from `$!`.
    setsid forks when the calling shell is already a process-group leader, which it
    is here, so `$!` would be setsid's pid and not the job's — and every later
    `kill -0` would then be asking about the wrong process.

    Being a session leader also means $$ doubles as the process-group id, which is
    what lets `uts stop` take down a whole pipeline rather than just its head.

    The name is the user's now, so the collision check happens here, in the same
    round trip that starts the job: a running session is never disturbed, and a
    finished one is only replaced on --force, because replacing it discards the log
    that says how the last run went.
    """
    d = f'"$HOME/.uts/jobs/{check_session_name(name)}"'
    inner = "\n".join([
        f'echo $$ > {d}/pid',
        # Without this a SIGTERM'd job leaves no rc, and `uts ps` can only report
        # that it is no longer there — indistinguishable from a reboot. 143 is the
        # conventional 128+SIGTERM.
        f"trap 'printf %s 143 > {d}/rc; exit 143' TERM",
        *(prefix or []),
        "{ " + command + "\n}; __uts_rc=$?",
        f'printf %s "$__uts_rc" > {d}/rc',
        "exit $__uts_rc",
    ])
    replace = 'rm -rf "$d"' if force else (
        f'{{ printf \'finished\\n\'; exit 1; }}'
    )
    return f"""
d={d}
if [ -d "$d" ]; then
  pid=$(cat "$d/pid" 2>/dev/null)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    printf 'busy\\t%s\\n' "$pid"
    exit 1
  fi
  {replace}
fi
mkdir -p "$d" || exit 1
printf '%s' {q(command)} > "$d/cmd"
date +%s > "$d/started"
if command -v setsid >/dev/null 2>&1; then
  setsid sh -c {q(inner)} > "$d/log" 2>&1 < /dev/null &
else
  nohup sh -c {q(inner)} > "$d/log" 2>&1 < /dev/null &
fi
# The launcher returns before the job has written its pid; without this the answer
# would be "started, pid unknown" most of the time.
i=0
while [ ! -s "$d/pid" ] && [ $i -lt 20 ]; do sleep 0.05; i=$((i+1)); done
printf 'job\\t{name}\\t%s\\n' "$(cat "$d/pid" 2>/dev/null)"
"""


# `find | while read` rather than a `"$root"/*/` glob, because an empty jobs
# directory is the normal case and must not read as an error. This predates
# posix_wrap, where the reason was that zsh aborts a command outright on a glob
# that matches nothing — that reason is gone now that snippets always run under sh.
# The code stays: POSIX sh leaves an unmatched pattern *as itself*, so the loop
# would run once over a literal `$root/*/` that does not exist. Same bug, different
# shape. Do not "simplify" this back to a glob.
LIST_JOBS = r"""
root="$HOME/.uts/jobs"
printf 'now\t%s\n' "$(date +%s)"
[ -d "$root" ] || exit 0
find "$root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while IFS= read -r d; do
  [ -d "$d" ] || continue
  id=$(basename "$d")
  pid=$(cat "$d/pid" 2>/dev/null)
  rc=$(cat "$d/rc" 2>/dev/null)
  started=$(cat "$d/started" 2>/dev/null)
  cmd=$(head -c 300 "$d/cmd" 2>/dev/null | tr '\n\t' '  ')
  if [ -n "$rc" ]; then
    state="exited:$rc"
  elif [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    state=running
  else
    state=vanished
  fi
  printf 'job\t%s\t%s\t%s\t%s\t%s\n' "$id" "$state" "${started:-0}" "${pid:-?}" "$cmd"
done
exit 0
"""


def list_jobs() -> str:
    return LIST_JOBS


def parse_jobs(stdout: str) -> tuple[float, list[dict]]:
    """(remote clock, jobs). Elapsed has to be computed against the remote clock —
    this whole tool exists partly because machine clocks disagree."""
    now = 0.0
    jobs = []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "now" and len(parts) == 2:
            try:
                now = float(parts[1])
            except ValueError:
                pass
        elif parts[0] == "job" and len(parts) == 6:
            _, job_id, state, started, pid, cmd = parts
            try:
                started_at = float(started)
            except ValueError:
                started_at = 0.0
            jobs.append({
                "id": job_id, "state": state, "started": started_at,
                "pid": pid, "command": cmd,
            })
    return now, jobs


def job_log(name: str, tail: int) -> str:
    d = f'"$HOME/.uts/jobs/{check_session_name(name)}"'
    return f"""
d={d}
[ -d "$d" ] || {{ echo "no session named {name} on this host" >&2; exit 1; }}
tail -n {int(tail)} "$d/log"
"""


def kill_job(name: str | None, force: bool) -> str:
    """Signal the job's whole process group, falling back to the single pid.

    The group is the point: `python train.py | tee log` started as one session
    leaves two processes, and killing only the leader orphans the rest.

    Without a name, every session running on this host is signalled. That sweep
    stays silent about the ones that have already finished — they are not a failure
    to stop, and saying so per host per session would bury the lines that matter.
    """
    sig = "KILL" if force else "TERM"
    if name is None:
        return f"""
root="$HOME/.uts/jobs"
[ -d "$root" ] || exit 0
find "$root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | while IFS= read -r d; do
  pid=$(cat "$d/pid" 2>/dev/null)
  [ -n "$pid" ] || continue
  kill -0 "$pid" 2>/dev/null || continue
  if kill -{sig} -"$pid" 2>/dev/null || kill -{sig} "$pid" 2>/dev/null; then
    printf 'signalled %s with SIG{sig} (pid %s)\\n' "$(basename "$d")" "$pid"
  fi
done
exit 0
"""
    d = f'"$HOME/.uts/jobs/{check_session_name(name)}"'
    return f"""
d={d}
pid=$(cat "$d/pid" 2>/dev/null)
[ -n "$pid" ] || {{ echo "no session named {name} on this host" >&2; exit 1; }}
if kill -{sig} -"$pid" 2>/dev/null || kill -{sig} "$pid" 2>/dev/null; then
  printf 'signalled %s with SIG{sig} (pid %s)\\n' {q(name)} "$pid"
else
  echo "session {name} is no longer running (pid $pid)" >&2
  exit 1
fi
"""


def stop_and_clean(name: str | None, force: bool) -> str:
    """`uts stop --clean`: stop it if it is running, then remove what it left behind.

    One rule at both widths — a named session, or every session on the host. `stop`
    without --clean already kills in bulk, so the flag never decides *whether* work
    is stopped, only whether the name is freed afterwards. Making the unnamed form
    spare running jobs would mean one flag with two meanings.

    Stopping and cleaning have to travel together, in one round trip: SIGTERM
    returns long before the job's own trap has written rc, and a clean arriving in
    that window sees a live pid and refuses to remove anything — leaving the name
    still taken, which is the one thing --clean is for. Waiting here is the only
    place that can watch the process actually go.

    The `rc` file, not the pid, is what says a job is over: pids are reused, and a
    session whose rc is already written must never be signalled again.
    """
    sig = "KILL" if force else "TERM"
    listing = (
        f'printf \'%s\\n\' "$root/{check_session_name(name)}"'
        if name
        else 'find "$root" -mindepth 1 -maxdepth 1 -type d 2>/dev/null'
    )
    live = '[ ! -f "$d/rc" ] && [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null'
    return f"""
root="$HOME/.uts/jobs"
[ -d "$root" ] || exit 0
{listing} | while IFS= read -r d; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  pid=$(cat "$d/pid" 2>/dev/null)
  if {live}; then
    kill -{sig} -"$pid" 2>/dev/null || kill -{sig} "$pid" 2>/dev/null
    i=0
    while kill -0 "$pid" 2>/dev/null && [ $i -lt 30 ]; do sleep 0.1; i=$((i+1)); done
  fi
  if {live}; then
    printf 'busy\\t%s\\n' "$n"
  else
    rm -rf "$d" && printf 'cleaned\\t%s\\n' "$n"
  fi
done
exit 0
"""


def parse_clean(stdout: str) -> tuple[list[str], list[str]]:
    """(removed, still running)."""
    cleaned, busy = [], []
    for line in stdout.splitlines():
        key, _, value = line.partition("\t")
        if key == "cleaned":
            cleaned.append(value)
        elif key == "busy":
            busy.append(value)
    return cleaned, busy


# The name is the user's, and it is interpolated into a path inside a shell
# snippet. Quoting alone would still allow `../../etc`, so the shape is checked.
_SESSION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")


def check_session_name(name: str) -> str:
    if not _SESSION_NAME.match(name or ""):
        raise PathSpecError(
            f"{name!r} is not a usable session name. Letters, digits, dot, dash and "
            f"underscore, up to 32 characters, starting with a letter or digit "
            f"(train, build-2, eval.v3)."
        )
    return name


# ------------------------------------------------------------------- sessions

# `env -0` and `pwd` are appended after the user's command, so the reply has to be
# separable from whatever the command itself printed. The nonce is regenerated per
# invocation and the split is on the *last* occurrence, which is what makes
# `uts exec -H a -s s 'cat some-uts-transcript.log'` safe.
SESSION_SENTINEL = "---uts-session-{nonce}---"

# Record separator between cwd and the environment dump. Neither appears in a path
# or in `env` output, where entries are already NUL-separated.
_RS = "\036"


def session_prefix(cwd: str | None, env: dict[str, str]) -> list[str]:
    """The `cd` and `export` lines that put a command back where the session left it."""
    lines = []
    if cwd:
        # Falling back to $HOME rather than failing: a directory that has since been
        # deleted should not make every later command in the session unusable.
        lines.append(f'cd {q(cwd)} 2>/dev/null || cd "$HOME"')
    for key in sorted(env):
        lines.append(f"export {key}={q(env[key])}")
    return lines


def session_wrap(command: str, cwd: str | None, env: dict[str, str], nonce: str) -> str:
    """Replay a session's cwd and exports, run the command, then report the new state.

    Known edge: a command that calls `exit` itself terminates the shell before the
    trailer runs, so that invocation leaves the session unchanged. The rc is still
    correct, and the alternative — running the command in a subshell so we survive
    it — would throw away every `cd` and `export` it performed, which is the whole
    point of a session.
    """
    lines = session_prefix(cwd, env)
    lines.append("{ " + command + "\n}; __uts_rc=$?")
    lines.append(f"printf '\\n%s\\n' {q(SESSION_SENTINEL.format(nonce=nonce))}")
    lines.append("pwd")
    lines.append(f"printf '{_RS}'")
    lines.append("env -0")
    lines.append("exit $__uts_rc")
    return "\n".join(lines)


def parse_session_trailer(stdout: str, nonce: str) -> tuple[str, str | None, dict[str, str]]:
    """(command output, cwd, env). cwd is None when the trailer never arrived."""
    marker = "\n" + SESSION_SENTINEL.format(nonce=nonce) + "\n"
    idx = stdout.rfind(marker)
    if idx < 0:
        return stdout, None, {}

    body = stdout[:idx]
    cwd, sep, blob = stdout[idx + len(marker):].partition(_RS)
    if not sep:
        return body, None, {}

    env: dict[str, str] = {}
    for item in blob.split("\0"):
        key, eq, value = item.partition("=")
        if eq and key:
            env[key] = value
    return body, cwd.strip("\n"), env


def env_probe() -> str:
    """A clean login environment, captured once per session+host as the baseline."""
    return "env -0"


def parse_env0(stdout: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in stdout.split("\0"):
        key, eq, value = item.partition("=")
        if eq and key:
            env[key] = value
    return env


def untar_stream(dest: str) -> str:
    """Unpack a gzip stream arriving on stdin into dest.

    `mkdir -p` first: pushing into a directory that does not exist yet is the normal
    case, and tar's own complaint about it does not say which path was missing.
    """
    return f"""
d={check_dest(dest)}
mkdir -p "$d" && tar xzf - -C "$d"
"""
