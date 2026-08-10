"""SSH transport: connect to one host, run one command, cap output while reading.

Two things here are load-bearing:

1. connect() explicitly disables keys and the agent. With password auth, paramiko
   would otherwise try every key under ~/.ssh first — slow, and it can hit the
   server's MaxAuthTries before the password is ever offered.
2. Output caps apply *while reading*. Capping after the fact means `cat 2GB.log`
   lands 2GB in local memory first. Past the hard limit the channel is closed.
"""

from __future__ import annotations

import select
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

import paramiko
from paramiko.ssh_exception import NoValidConnectionsError

from .inventory import Host

DEFAULT_MAX_LINES = 200
DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_EXEC_TIMEOUT = 60.0
STDERR_MAX_LINES = 40
STDERR_MAX_BYTES = 8 * 1024
HARD_ABORT_BYTES = 8 * 1024 * 1024


@dataclass
class Limits:
    max_lines: int = DEFAULT_MAX_LINES
    max_bytes: int = DEFAULT_MAX_BYTES
    timeout: float = DEFAULT_EXEC_TIMEOUT
    # pull raises this: moving data is the whole point there.
    hard_abort_bytes: int = HARD_ABORT_BYTES


@dataclass
class Result:
    """One command on one host. Transport failures set `error` and leave rc at -1."""

    host: Host
    rc: int = -1
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    error: str | None = None
    # Host is fine, uts declined — size cap, too many files, and so on.
    # Kept separate from `error` so exit codes can tell "broken" from "refused".
    refused: str | None = None
    truncated: bool = False
    dropped_lines: int = 0
    dropped_bytes: int = 0
    aborted: bool = False
    extra: dict = field(default_factory=dict)
    # Local wall clock around the exec window (excluding connect); clock skew needs it.
    wall_start: float = 0.0
    wall_end: float = 0.0

    @property
    def reachable(self) -> bool:
        return self.error is None


class _Capped:
    """Buffer that stops storing once full, but keeps counting what it drops."""

    def __init__(self, max_bytes: int, max_lines: int) -> None:
        self._max_bytes = max_bytes
        self._max_lines = max_lines
        self._chunks: list[bytes] = []
        self._bytes = 0
        self._lines = 0
        self.dropped_bytes = 0
        self.dropped_lines = 0

    @property
    def truncated(self) -> bool:
        return self.dropped_bytes > 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        newlines = chunk.count(b"\n")
        if self._bytes >= self._max_bytes or self._lines >= self._max_lines:
            self.dropped_bytes += len(chunk)
            self.dropped_lines += newlines
            return

        room_bytes = self._max_bytes - self._bytes
        keep = chunk
        if len(chunk) > room_bytes:
            keep, rest = chunk[:room_bytes], chunk[room_bytes:]
            self.dropped_bytes += len(rest)
            self.dropped_lines += rest.count(b"\n")

        room_lines = self._max_lines - self._lines
        keep_newlines = keep.count(b"\n")
        if keep_newlines > room_lines:
            cut = _index_after_nth_newline(keep, room_lines)
            rest = keep[cut:]
            keep = keep[:cut]
            self.dropped_bytes += len(rest)
            self.dropped_lines += rest.count(b"\n")
            keep_newlines = room_lines

        if keep:
            self._chunks.append(keep)
            self._bytes += len(keep)
            self._lines += keep_newlines

    def text(self) -> str:
        return b"".join(self._chunks).decode("utf-8", errors="replace")


def _index_after_nth_newline(data: bytes, n: int) -> int:
    pos = 0
    for _ in range(n):
        nxt = data.find(b"\n", pos)
        if nxt < 0:
            return len(data)
        pos = nxt + 1
    return pos


class Conn:
    """A connection to one host. Connects lazily, reusable within a process."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self._client: paramiko.SSHClient | None = None

    def client(self) -> paramiko.SSHClient:
        if self._client is None:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=self.host.ip,
                port=self.host.port,
                username=self.host.user,
                password=self.host.password,
                timeout=self.host.timeout,
                banner_timeout=self.host.timeout,
                auth_timeout=self.host.timeout,
                look_for_keys=False,
                allow_agent=False,
            )
            self._client = client
        return self._client

    def run(self, command: str, limits: Limits | None = None) -> Result:
        limits = limits or Limits()
        started = time.monotonic()
        wall_start = time.time()
        transport = self.client().get_transport()
        if transport is None:
            raise paramiko.SSHException("connection dropped")
        chan = transport.open_session()
        chan.settimeout(limits.timeout)
        try:
            chan.exec_command(command)
            out, err, aborted = _drain(chan, limits)
            rc = chan.recv_exit_status() if not aborted else -1
        finally:
            chan.close()

        return Result(
            host=self.host,
            rc=rc,
            stdout=out.text(),
            stderr=err.text(),
            duration=time.monotonic() - started,
            truncated=out.truncated or err.truncated,
            dropped_lines=out.dropped_lines,
            dropped_bytes=out.dropped_bytes,
            aborted=aborted,
            wall_start=wall_start,
            wall_end=time.time(),
        )

    def stream_stdout(
        self,
        command: str,
        sink,
        timeout: float = 600.0,
        max_bytes: int | None = None,
    ) -> tuple[int, int, str]:
        """Write remote stdout straight into `sink` (a binary file object).

        Transfers must use this rather than run(), which keeps the whole output in
        memory. Returns (rc, bytes written, stderr).
        """
        transport = self.client().get_transport()
        if transport is None:
            raise paramiko.SSHException("connection dropped")
        chan = transport.open_session()
        chan.settimeout(timeout)
        err = _Capped(STDERR_MAX_BYTES, STDERR_MAX_LINES)
        written = 0
        deadline = time.monotonic() + timeout
        idle_after_exit = 0
        try:
            chan.exec_command(command)
            while True:
                ready, _, _ = select.select([chan], [], [], 0.2)
                got = False
                if ready:
                    while chan.recv_ready():
                        chunk = chan.recv(65536)
                        if not chunk:
                            break
                        sink.write(chunk)
                        written += len(chunk)
                        got = True
                    while chan.recv_stderr_ready():
                        chunk = chan.recv_stderr(32768)
                        if not chunk:
                            break
                        err.feed(chunk)
                        got = True

                if max_bytes is not None and written > max_bytes:
                    raise RuntimeError(
                        f"transfer exceeded the {max_bytes:,} byte limit and was aborted "
                        f"— raise --max-size, or narrow the file set first"
                    )

                drained = not chan.recv_ready() and not chan.recv_stderr_ready()
                if chan.eof_received and drained:
                    break
                if chan.exit_status_ready() and drained and not got:
                    idle_after_exit += 1
                    if idle_after_exit >= 5:
                        break
                elif got:
                    idle_after_exit = 0

                if time.monotonic() > deadline:
                    raise TimeoutError(f"transfer did not finish within {timeout:g}s")
            rc = chan.recv_exit_status()
        finally:
            chan.close()
        return rc, written, err.text()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "Conn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _drain(chan, limits: Limits) -> tuple[_Capped, _Capped, bool]:
    out = _Capped(limits.max_bytes, limits.max_lines)
    err = _Capped(STDERR_MAX_BYTES, STDERR_MAX_LINES)
    deadline = time.monotonic() + limits.timeout
    received = 0
    idle_after_exit = 0

    while True:
        ready, _, _ = select.select([chan], [], [], 0.2)
        got = False
        if ready:
            while chan.recv_ready():
                chunk = chan.recv(32768)
                if not chunk:
                    break
                received += len(chunk)
                out.feed(chunk)
                got = True
            while chan.recv_stderr_ready():
                chunk = chan.recv_stderr(32768)
                if not chunk:
                    break
                received += len(chunk)
                err.feed(chunk)
                got = True

        if received > limits.hard_abort_bytes:
            return out, err, True

        drained = not chan.recv_ready() and not chan.recv_stderr_ready()
        if chan.eof_received and drained:
            break
        if chan.exit_status_ready() and drained and not got:
            # Exit status can arrive before EOF; wait ~1s so the tail isn't cut off.
            idle_after_exit += 1
            if idle_after_exit >= 5:
                break
        elif got:
            idle_after_exit = 0

        if time.monotonic() > deadline:
            raise TimeoutError(f"command did not finish within {limits.timeout:g}s")

    return out, err, False


def describe_failure(host: Host, exc: BaseException) -> str:
    """Turn a connection exception into one actionable sentence."""
    if isinstance(exc, paramiko.AuthenticationException):
        return f"auth failed — user/password for {host.name} in hosts.json is wrong"
    if isinstance(exc, paramiko.BadHostKeyException):
        return f"host key verification failed: {exc}"
    if isinstance(exc, socket.gaierror):
        return f"cannot resolve {host.ip}"
    if isinstance(exc, NoValidConnectionsError):
        return f"cannot reach {host.ip}:{host.port} — sshd not running, or wrong port"
    if isinstance(exc, ConnectionRefusedError):
        return f"{host.ip}:{host.port} refused the connection — sshd not running, or wrong port"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return f"timed out connecting to {host.ip}:{host.port} ({host.timeout:g}s) — network or firewall"
    if isinstance(exc, OSError):
        return f"network error: {exc}"
    if isinstance(exc, paramiko.SSHException):
        return f"SSH error: {exc}"
    return f"{type(exc).__name__}: {exc}"


def run_many(
    hosts: list[Host],
    task: Callable[[Conn], Result],
    jobs: int = 8,
) -> list[Result]:
    """Run concurrently, return in input order. One dead host never raises."""

    def one(host: Host) -> Result:
        started = time.monotonic()
        try:
            with Conn(host) as conn:
                return task(conn)
        except Exception as exc:  # noqa: BLE001 — one host failing must not sink the rest
            return Result(
                host=host,
                error=describe_failure(host, exc),
                duration=time.monotonic() - started,
            )

    if len(hosts) == 1:
        return [one(hosts[0])]
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(hosts)))) as pool:
        return list(pool.map(one, hosts))


def run_command(hosts: list[Host], command: str, limits: Limits, jobs: int = 8) -> list[Result]:
    return run_many(hosts, lambda conn: conn.run(command, limits), jobs=jobs)
