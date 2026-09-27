import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import pytest

from safe_reader import safe_read_file, MAX_LINES_PER_READ, DEFAULT_MAX_CHARS


@pytest.fixture
def source_file(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    lines = [f"line {i} of code" for i in range(1, 1001)]
    (root / "big.py").write_text("\n".join(lines) + "\n")
    return root


def test_range_read_with_line_numbers(source_file):
    out = safe_read_file("big.py", 41, 45, repo_root=str(source_file))
    assert out.startswith("[Otsutsuki safe_read]")
    assert "total_lines=1000" in out
    assert "showing=41-45" in out
    assert "   41 | line 41 of code" in out
    assert "   45 | line 45 of code" in out
    assert "line 46" not in out


def test_default_window_is_capped(source_file):
    out = safe_read_file("big.py", 1, 5000, repo_root=str(source_file))
    assert f"showing=1-{MAX_LINES_PER_READ}" in out
    assert "clamped" in out
    assert len(out) < 100_000


def test_max_chars_trims_from_end(source_file):
    out = safe_read_file("big.py", 1, 200, max_chars=1000, repo_root=str(source_file))
    assert out.startswith("[Otsutsuki safe_read]")
    assert "TRUNCATED" in out
    assert len(out) <= 1000


def test_start_beyond_eof_reports_size(source_file):
    out = safe_read_file("big.py", 2000, repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "1000" in out


def test_minified_file_rejected(source_file):
    (source_file / "bundle.js").write_text("var a=1;" + "".join(f"x{i}={i};" for i in range(5000)))
    out = safe_read_file("bundle.js", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "minified" in out


def test_single_long_line_rejected(source_file):
    # Mostly normal file, but one embedded blob: still refused.
    (source_file / "blob.py").write_text("import os\n" + "x = '" + "A" * 5000 + "'\n")
    out = safe_read_file("blob.py", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "minified" in out


def test_binary_file_rejected(source_file):
    (source_file / "image.png").write_bytes(b"PNG\r\n\x00\x00\x01" + os.urandom(2000))
    out = safe_read_file("image.png", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "binary" in out


def test_missing_file_and_directory(source_file):
    out = safe_read_file("nope.py", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "not found" in out
    out = safe_read_file("subdir", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")


def test_outside_repo_rejected(source_file):
    out = safe_read_file("/etc/hosts", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "outside the repository" in out


def test_git_and_env_rejected(source_file):
    (source_file / ".git").mkdir()
    (source_file / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    out = safe_read_file(".git/HEAD", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")

    (source_file / ".env").write_text("AI_API_KEY=supersecret\n")
    out = safe_read_file(".env", repo_root=str(source_file))
    assert out.startswith("READ_ERROR")
    assert "supersecret" not in out


def test_string_line_args_coerced(source_file):
    # The LLM may send numbers as JSON strings; they must still work.
    out = safe_read_file("big.py", "41", "42", repo_root=str(source_file))
    assert out.startswith("[Otsutsuki safe_read]")
    assert "line 41 of code" in out


def test_end_before_start_rejected(source_file):
    out = safe_read_file("big.py", 50, 10, repo_root=str(source_file))
    assert out.startswith("READ_ERROR")


def test_empty_file(source_file):
    (source_file / "empty.py").write_text("")
    out = safe_read_file("empty.py", repo_root=str(source_file))
    assert "empty file" in out


def test_no_repo_root_still_works(source_file):
    out = safe_read_file(str(source_file / "big.py"), 1, 3)
    assert "line 1 of code" in out
