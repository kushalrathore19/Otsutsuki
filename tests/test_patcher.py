import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

import pytest

from patcher import apply_patch_diff


@pytest.fixture
def repo(tmp_path):
    """A fake repository root with one source file."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(
        "import os\n"
        "\n"
        "def greet(name):\n"
        "    return 'hello ' + name\n"
        "\n"
        "def farewell(name):\n"
        "    return 'bye ' + name\n"
    )
    return root


def test_exact_apply_single_hunk(repo):
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,4 +1,4 @@\n"
        " import os\n"
        " \n"
        " def greet(name):\n"
        "-    return 'hello ' + name\n"
        "+    return 'hi ' + name\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    assert "return 'hi ' + name" in (repo / "app.py").read_text()


def test_fuzzy_whitespace_drift(repo):
    # Context lines carry wrong indentation and tabs -- must still apply,
    # and the file's own indentation must be preserved for kept lines.
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -3,4 +3,4 @@\n"
        "def greet(name):\n"
        "\t    return 'hello ' + name\n"
        "+    return 'hello there ' + name\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    text = (repo / "app.py").read_text()
    assert "return 'hello there ' + name" in text
    assert "def greet(name):" in text
    assert "\t" not in text  # no tab was written into the file


def test_fuzzy_stale_line_numbers(repo):
    # Hunk header claims line 100 -- far from the real location (~line 4).
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -100,3 +100,4 @@\n"
        " def greet(name):\n"
        "     return 'hello ' + name\n"
        "+    print('greeted')\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    text = (repo / "app.py").read_text()
    assert text.index("def greet(name):") < text.index("print('greeted')") < text.index("def farewell")


def test_new_file_creation_with_fences():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        diff = (
            "Here is the patch:\n"
            "```diff\n"
            "--- /dev/null\n"
            "+++ b/new_dir/new.txt\n"
            "@@ -0,0 +1 @@\n"
            "+success\n"
            "```\n"
        )
        out = apply_patch_diff(diff, root)
        assert out.startswith("OK"), out
        assert (os.path.join(root, "new_dir", "new.txt")) and \
            open(os.path.join(root, "new_dir", "new.txt")).read() == "success\n"


def test_new_file_idempotent_reapply():
    import tempfile
    with tempfile.TemporaryDirectory() as root:
        diff = "--- /dev/null\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+success\n"
        assert apply_patch_diff(diff, root).startswith("OK")
        out = apply_patch_diff(diff, root)
        assert out.startswith("OK") and "idempotent" in out


def test_modification_idempotent_reapply(repo):
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,4 +1,4 @@\n"
        " import os\n"
        " \n"
        " def greet(name):\n"
        "-    return 'hello ' + name\n"
        "+    return 'hi ' + name\n"
    )
    assert apply_patch_diff(diff, str(repo)).startswith("OK")
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK") and "already" in out
    # The line must not be doubled or corrupted by the second apply.
    text = (repo / "app.py").read_text()
    assert text.count("return 'hi ' + name") == 1


def test_multi_hunk_apply(repo):
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1,2 +1,3 @@\n"
        " import os\n"
        "+import sys\n"
        " \n"
        "@@ -6,3 +7,3 @@\n"
        " def farewell(name):\n"
        "-    return 'bye ' + name\n"
        "+    return 'goodbye ' + name\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    text = (repo / "app.py").read_text()
    assert "import sys" in text
    assert "return 'goodbye ' + name" in text


def test_failure_is_concise_and_parseable(repo):
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -3,4 +3,4 @@\n"
        " def totally_bogus(name):\n"
        "     return 'nope' + name\n"
        "-    return 'hello ' + name\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("PATCH_ERROR")
    assert "app.py" in out
    assert "hunk #1" in out
    # File untouched on failure.
    assert "totally_bogus" not in (repo / "app.py").read_text()


def test_garbage_input_is_parseable_error(repo):
    out = apply_patch_diff("I think we should refactor things a bit.", str(repo))
    assert out.startswith("PATCH_ERROR")
    out2 = apply_patch_diff("", str(repo))
    assert out2.startswith("PATCH_ERROR")


def test_path_escape_rejected(repo):
    diff = (
        "--- /dev/null\n"
        "+++ b/../../../etc/evil.txt\n"
        "@@ -0,0 +1 @@\n"
        "+pwned\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("PATCH_ERROR")
    assert "outside the repository" in out


def test_git_internals_rejected(repo):
    diff = (
        "--- a/.git/config\n"
        "+++ b/.git/config\n"
        "@@ -1,2 +1,2 @@\n"
        "-[core]\n"
        "+[core]\n"
        "     bare = false\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("PATCH_ERROR")


def test_file_deletion(repo):
    (repo / "temp.txt").write_text("delete me\n")
    diff = (
        "--- a/temp.txt\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-delete me\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    assert not (repo / "temp.txt").exists()


def test_missing_context_in_fuzzy_window_preserves_file_lines(repo):
    # Fuzzy match where the hunk lost one context line: the window line the
    # hunk never mentions must survive the splice.
    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -3,3 +3,4 @@\n"
        " def greet(name):\n"
        "+    print('hi')\n"
        "     return 'hello ' + name\n"
    )
    out = apply_patch_diff(diff, str(repo))
    assert out.startswith("OK"), out
    text = (repo / "app.py").read_text()
    assert "print('hi')" in text
    assert "def farewell" in text and "return 'bye ' + name" in text
