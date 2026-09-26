import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../src')))

from sandbox import check_paths

def test_path_rejection():
    cwd = "/my/repo/root"
    # Should reject path outside repo
    is_valid, err = check_paths("cat ../outside.txt", cwd)
    assert not is_valid
    assert "resolves outside" in err
    
    is_valid, err = check_paths("rm /etc/passwd", cwd)
    assert not is_valid
    
def test_path_allowance():
    cwd = "/my/repo/root"
    # Should allow /tmp
    is_valid, err = check_paths("patch -p0 < /tmp/patch.diff", cwd)
    assert is_valid
    
    # Should allow inside repo
    is_valid, err = check_paths("cat src/main.py", cwd)
    assert is_valid
    
    # Should allow /bin
    is_valid, err = check_paths("/bin/ls -la", cwd)
    assert is_valid
