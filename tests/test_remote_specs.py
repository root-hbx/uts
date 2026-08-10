import pytest

from uts import remote


# ------------------------------------------------------------------ path specs


def test_path_spec_passes_through_globs_and_tilde():
    # Both must reach the remote shell verbatim; quoting them breaks expansion
    assert remote.check_path_spec("~/data/*.csv") == "~/data/*.csv"


@pytest.mark.parametrize("bad", ["~/d; rm -rf /", "a && b", "a | b", "`id`", "$(id)", "a > b"])
def test_path_spec_rejects_command_injection(bad):
    with pytest.raises(remote.PathSpecError):
        remote.check_path_spec(bad)


def test_path_spec_rejects_empty():
    with pytest.raises(remote.PathSpecError):
        remote.check_path_spec("   ")


# --------------------------------------------------------------- listing script


def test_list_files_uses_printf_not_dash_c():
    # Regression: GNU stat's -c does not expand \t and emits a literal backslash-t,
    # which breaks parsing. Only the glob path uses stat; directories go through
    # find -printf and look fine, which is what hid the bug.
    script = remote.list_files("~/x/*.txt")
    assert "stat --printf=" in script
    assert "stat -c" not in script


def test_list_files_embeds_spec_and_cap():
    script = remote.list_files("~/x/*.txt", cap=42)
    assert "~/x/*.txt" in script and "head -n 42" in script
    assert "__SPEC__" not in script and "__CAP__" not in script


def test_parse_listing():
    files = remote.parse_listing(
        "54480\t1772791055\t/home/a/1.txt\n"
        "60900\t1766841148.5\t/home/a/5.txt\n"
        "garbage\n"
        "\n"
    )
    assert [f["path"] for f in files] == ["/home/a/1.txt", "/home/a/5.txt"]
    assert files[0]["size"] == 54480
    assert files[1]["mtime"] == pytest.approx(1766841148.5)


def test_parse_listing_survives_paths_with_spaces():
    (f,) = remote.parse_listing("10\t100\t/home/a/my file.log\n")
    assert f["path"] == "/home/a/my file.log"


# ------------------------------------------------------------------ shape probe


def test_shape_probe_quotes_every_path():
    script = remote.shape_probe(["/a/b.txt", "/tmp/my file.txt", "/x/$weird.txt"])
    assert "'/tmp/my file.txt'" in script
    assert "'/x/$weird.txt'" in script


def test_parse_shape_infers_delimiter_and_columns():
    rows = remote.parse_shape(
        "110\t109\t0\t0\t0\t/a/5.txt\n"       # 109 commas -> 110 csv columns
        "50\t0\t3\t0\t0\t/a/b.tsv\n"          # tabs
        "7\t0\t0\t0\t0\t/a/damage_list.txt\n"  # no delimiter -> single column
    )
    assert (rows[0]["columns"], rows[0]["delimiter"], rows[0]["lines"]) == (110, ",", 110)
    assert (rows[1]["columns"], rows[1]["delimiter"]) == (4, "\t")
    assert (rows[2]["columns"], rows[2]["delimiter"]) == (1, None)


def test_parse_shape_ignores_malformed_rows():
    assert remote.parse_shape("one column only\n1\t2\t3\n") == []


# ---------------------------------------------------------------- time and size


@pytest.mark.parametrize(
    "text, seconds",
    [("30m", 1800), ("2h", 7200), ("7d", 604800), ("1w", 604800), (" 24H ", 86400)],
)
def test_parse_since(text, seconds):
    assert remote.parse_since(text) == seconds


@pytest.mark.parametrize("bad", ["3 hours", "h2", "", "yesterday", "-2h"])
def test_parse_since_rejects_junk(bad):
    with pytest.raises(ValueError, match="--since"):
        remote.parse_since(bad)


@pytest.mark.parametrize(
    "text, size",
    [("500", 500), ("1K", 1024), ("10M", 10 * 1024**2), ("2G", 2 * 1024**3), ("1.5M", 1572864)],
)
def test_parse_size(text, size):
    assert remote.parse_size(text) == size


@pytest.mark.parametrize("bad", ["big", "", "10X"])
def test_parse_size_rejects_junk(bad):
    with pytest.raises(ValueError):
        remote.parse_size(bad)


# ---------------------------------------------------------------------- packing


def test_tar_stream_strips_leading_slash():
    # Stripping the leading slash and pairing it with -C / keeps clean relative paths
    # in the archive: it unpacks as a mirror of the remote tree, and tar's
    # "Removing leading /" warning never reaches stderr.
    cmd = remote.tar_stream(["/home/a/1.txt", "/var/log/x.log"])
    assert "home/a/1.txt" in cmd and "'/home/a/1.txt'" not in cmd
    assert "-C /" in cmd and "tar czf -" in cmd
