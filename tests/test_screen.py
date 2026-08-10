"""Terminal bytes into readable text.

The distinction being tested is why two functions exist: stripping escape sequences
recovers a transcript, but a full-screen program never wrote a transcript. It gave
painting instructions, and the order it emitted them in is not the order they
appear on screen.
"""

import pytest

from uts.screen import render_frame, strip_ansi

ESC = "\x1b"


# ------------------------------------------------------------------ strip_ansi


def test_plain_text_is_untouched():
    assert strip_ansi("hello world\n") == "hello world\n"


def test_colour_sequences_are_removed_but_the_text_stays():
    assert strip_ansi(f"{ESC}[31mred{ESC}[0m and normal") == "red and normal"


def test_cursor_movement_is_removed():
    assert strip_ansi(f"{ESC}[2J{ESC}[H{ESC}[3;1Hafter") == "after"


def test_carriage_returns_from_the_pty_are_dropped():
    # A pty turns every \n into \r\n; left in, every captured line ends in \r.
    assert strip_ansi("one\r\ntwo\r\n") == "one\ntwo\n"


def test_window_title_sequences_do_not_swallow_the_output():
    # OSC runs until BEL, and a naive regex either eats the rest of the line or
    # leaves the title text in the output.
    assert strip_ansi(f"{ESC}]0;my title\x07real output") == "real output"


def test_osc_terminated_by_string_terminator_also_ends():
    assert strip_ansi(f"{ESC}]0;title{ESC}\\real output") == "real output"


def test_charset_designation_is_consumed_whole():
    # ESC ( B is three bytes, not two: an intermediate then a final. Treating it as
    # a two-character escape leaves a stray "B" at the head of the output.
    assert strip_ansi(f"{ESC}(Btext") == "text"
    assert strip_ansi(f"{ESC}#8text") == "text"


def test_plain_two_character_escapes_are_consumed_whole():
    assert strip_ansi(f"{ESC}7saved{ESC}8") == "saved"


def test_a_truncated_escape_at_the_end_does_not_hang_or_leak():
    assert strip_ansi(f"text{ESC}[") == "text"
    assert strip_ansi(f"text{ESC}") == "text"


def test_stripping_is_not_enough_for_a_screen():
    # Paint "BBBB" then go back and overwrite the middle with "--". Stripped, the
    # emission order survives and reads as if nothing was overwritten.
    painted = f"BBBB{ESC}[1;2H--"
    assert strip_ansi(painted) == "BBBB--"


# ------------------------------------------------------------------ render_frame


def test_a_screen_shows_what_was_drawn_not_what_was_emitted():
    painted = f"BBBB{ESC}[1;2H--".encode()
    assert render_frame(painted, cols=10, rows=3) == "B--B"


def test_the_cursor_can_move_anywhere_and_the_layout_holds():
    data = f"{ESC}[2J{ESC}[3;5Hdeep{ESC}[1;1Htop".encode()
    assert render_frame(data, cols=20, rows=5).splitlines() == ["top", "", "    deep"]


def test_a_cleared_screen_is_really_cleared():
    data = f"garbage everywhere{ESC}[2J{ESC}[1;1Hclean".encode()
    assert render_frame(data, cols=30, rows=4) == "clean"


def test_trailing_blank_rows_are_not_padding_out_the_answer():
    # A 48-row screen from a program using three of them is mostly nothing, and
    # nothing is not free for a model to read.
    assert render_frame(b"just this", cols=20, rows=48) == "just this"


def test_colours_do_not_reach_the_text():
    data = f"{ESC}[1;32mgreen bold{ESC}[0m".encode()
    assert render_frame(data, cols=20, rows=2) == "green bold"


def test_box_drawing_and_wide_characters_survive():
    data = "╭─┐cpu┌─╮".encode()
    assert "cpu" in render_frame(data, cols=20, rows=2)


def test_output_wider_than_the_screen_wraps_rather_than_vanishing():
    frame = render_frame(b"abcdefghij", cols=5, rows=3)
    assert frame.splitlines() == ["abcde", "fghij"]


@pytest.mark.parametrize("junk", [b"", b"\x00\x00", b"\xff\xfe invalid utf-8"])
def test_nothing_and_nonsense_render_without_raising(junk):
    render_frame(junk, cols=10, rows=3)
