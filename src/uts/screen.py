"""Turn terminal bytes into something an LLM can read.

A PTY changes what arrives: programs stop writing a transcript and start writing
*instructions* — move the cursor here, erase to end of line, set a colour. Two
shapes are useful downstream and they need different treatment.

An ordinary program under a PTY (`sudo`, anything that checks isatty, anything
that colours its output) produces a transcript with escape sequences sprinkled in;
stripping them recovers the text.

A full-screen program (btop, htop, vim) produces no transcript at all. Its bytes
only make sense when replayed against a screen, because it paints cell by cell and
overwrites as it goes — strip the escapes and you get the fragments in the order
they were emitted, which is not the order they appear. So those are fed through a
real VT emulator and the resulting screen is what gets reported.
"""

from __future__ import annotations

DEFAULT_COLS = 160
DEFAULT_ROWS = 48

# CSI/OSC and the shorter two-character escapes. Deliberately a lexer rather than
# one clever regex: OSC strings run until BEL or ST and can contain almost anything.
_ESC = "\x1b"


def strip_ansi(text: str) -> str:
    """Remove escape sequences, keep the text and the newlines.

    Also drops the carriage returns that a PTY inserts before every newline: left
    in, they make every line of a captured log end in \\r and break naive parsing
    downstream.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != _ESC:
            if ch != "\r":
                out.append(ch)
            i += 1
            continue

        i += 1
        if i >= n:
            break
        kind = text[i]
        i += 1
        if kind == "[":                      # CSI: params, then a final byte @-~
            while i < n and not ("\x40" <= text[i] <= "\x7e"):
                i += 1
            i += 1
        elif kind in "]P^_":                 # OSC/DCS/PM/APC: run to BEL or ESC \
            while i < n:
                if text[i] == "\x07":
                    i += 1
                    break
                if text[i] == _ESC and i + 1 < n and text[i + 1] == "\\":
                    i += 2
                    break
                i += 1
        elif "\x20" <= kind <= "\x2f":       # intermediates, then one final byte:
            while i < n and "\x20" <= text[i] <= "\x2f":   # ESC ( B, ESC # 8, ...
                i += 1
            i += 1
        # Anything else is a two-character escape and `kind` was its second half.
    return "".join(out)


def render_frame(
    data: bytes, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS
) -> str:
    """Replay a full-screen program's output and return the screen it drew.

    Trailing blank lines are dropped: a 48-row screen from a program using 20 of
    them would otherwise be mostly padding, and padding is not free to read.
    """
    import pyte

    screen = pyte.Screen(cols, rows)
    stream = pyte.ByteStream(screen)
    stream.feed(data)

    lines = [line.rstrip() for line in screen.display]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
