
import os
import stat
import tempfile
import time
from pathlib import Path

def _ensure_writable(path: str) -> None:
    """Ensure a file is writable before operations."""
    if not path:
        return
    try:
        if os.path.exists(path) and not os.path.isdir(path):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
            print(f"Ensured writable: {path}")
    except Exception as e:
        print(f"Failed to ensure writable: {e}")

def save_file(path, content):
    print(f"Attempting to save to: {path}")
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    _ensure_writable(path)
    
    # Use atomic write: write to a temp file in the same dir then replace
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dirname or None)
        print(f"Created temp file: {tmp_path}")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        
        # Ensure the target (if existing) is writable before replacing
        _ensure_writable(path)
        
        print(f"Replacing {path} with {tmp_path}")
        os.replace(tmp_path, path)
        print("Save successful")
    except Exception as e:
        print(f"Save failed: {e}")
    finally:
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

# Test logic
target_dir = r"C:\dev\Grant-System"
test_file = os.path.join(target_dir, "test_write_perm.txt")

print(f"Testing write to {test_file}")
save_file(test_file, "Hello World")

# Clean up
if os.path.exists(test_file):
    os.remove(test_file)
    print("Cleaned up test file")
