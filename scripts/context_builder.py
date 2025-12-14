"""
Context building utilities for the dialectical loop.

This module provides:
- build_codebase_snapshot: Create snapshot of project files respecting gitignore
- build_changed_files_snapshot: Create snapshot of changed files only
- apply_file_ops: Execute filesystem operations (move/delete/mkdir)
- Helper functions for file filtering and permissions
"""

import os
import stat
import shutil
import subprocess
import time
import getpass
from pathlib import Path

SUBPROCESS_TEXT_ENCODING = "utf-8"

# Default configuration constants (can be overridden by caller)
DEFAULT_CONTEXT_EXTS = [
    ".md",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".env",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".css",
    ".scss",
    ".sql",
    ".prisma",
]

DEFAULT_EXCLUDE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    ".next",
    "dist",
    "build",
    "out",
    "coverage",
    "__pycache__",
}

DEFAULT_CONTEXT_MAX_BYTES = 200_000
DEFAULT_CONTEXT_MAX_FILE_BYTES = 30_000
DEFAULT_CONTEXT_MAX_FILES = 60


def _ensure_writable(path: str) -> None:
    """Ensure a file is writable before operations."""
    if not path:
        return
    try:
        if os.path.exists(path) and not os.path.isdir(path):
            os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except Exception:
        pass


def _gather_write_diagnostics(path: str, exc: Exception) -> str:
    """Return a short diagnostic string with context when a write fails."""
    out = []
    try:
        out.append(f"Current user: {getpass.getuser()}")
    except Exception:
        pass
    try:
        p = Path(path)
        parent = p.parent if p.parent else Path(".")
        out.append(f"Target path: {p}")
        out.append(f"Parent exists: {parent.exists()}")
        try:
            mode = oct(parent.stat().st_mode & 0o777)
            out.append(f"Parent mode (oct): {mode}")
        except Exception:
            pass
        # Check quick writability probe
        probe = parent / f".dialectical_write_probe_{int(time.time())}.tmp"
        try:
            with open(probe, "wb") as f:
                f.write(b"ok")
            probe.unlink(missing_ok=True)
            out.append("Quick write probe: success")
        except PermissionError as pe:
            out.append(f"Quick write probe: PermissionError: {pe}")
        except Exception as e:
            out.append(f"Quick write probe: failed: {e}")
    except Exception as e:
        out.append(f"Diagnostics error: {e}")
    # Add hint from known Windows Controlled Folder Access
    if isinstance(exc, PermissionError) or "Permission" in str(exc):
        out.append(
            "Hint: On Windows this may be Controlled Folder Access (Windows Security > Ransomware protection)."
        )
    return "\n".join(out)


def _rmtree_force(path: str) -> None:
    """Force remove directory tree, handling readonly files."""
    def _onerror(func, p, _exc_info):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=_onerror)


def _split_csv_arg(value):
    """Split comma-separated argument string."""
    if not value:
        return []
    items = [v.strip() for v in value.split(",")]
    return [v for v in items if v]


def _normalize_ext_list(exts):
    """Normalize file extensions to lowercase with leading dot."""
    normalized = []
    for ext in exts:
        if not ext:
            continue
        e = ext.strip()
        if not e:
            continue
        if not e.startswith("."):
            e = f".{e}"
        normalized.append(e.lower())
    return normalized


def _should_exclude_dir(dirname, exclude_dirs):
    """Check if directory should be excluded from scanning."""
    return dirname in exclude_dirs


def _run_capture(argv, cwd="."):
    """Run subprocess and capture output."""
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
            shell=False,
        )
        return result.returncode, result.stdout, result.stderr
    except Exception as e:
        return 1, "", str(e)


def get_git_changed_paths(repo_dir="."):
    """Get list of changed file paths from git status."""
    code, out, _err = _run_capture(["git", "status", "--porcelain"], cwd=repo_dir)
    if code != 0:
        return None

    paths = []
    for line in out.splitlines():
        if not line.strip():
            continue

        payload = line[3:] if len(line) >= 4 else ""
        if "->" in payload:
            payload = payload.split("->", 1)[1]
        path = payload.strip()
        if path:
            paths.append(path)
    return paths


def _build_file_list(mode, root_dir, changed_paths, include_exts, exclude_dirs):
    """Build list of files based on mode (snapshot or changed)."""
    if mode == "snapshot":
        # Try git ls-files first
        git_files = []
        try:
            code, out, _ = _run_capture(["git", "ls-files"], cwd=root_dir)
            if code == 0:
                git_files = [f.strip() for f in out.splitlines() if f.strip()]
        except Exception:
            pass

        if git_files:
            return git_files
        else:
            # Fallback to os.walk
            file_list = []
            for root, dirs, files in os.walk(root_dir):
                dirs[:] = [d for d in sorted(dirs) if not _should_exclude_dir(d, exclude_dirs)]
                for filename in sorted(files):
                    path = Path(root) / filename
                    rel_path = os.path.relpath(path, root_dir)
                    file_list.append(rel_path)
            return file_list
    elif mode == "changed":
        return changed_paths
    else:
        raise ValueError(f"Invalid mode: {mode}")


def build_context(
    mode="snapshot",
    changed_paths=None,
    root_dir=".",
    include_exts=None,
    exclude_dirs=None,
    max_total_bytes=DEFAULT_CONTEXT_MAX_BYTES,
    max_file_bytes=DEFAULT_CONTEXT_MAX_FILE_BYTES,
    max_files=DEFAULT_CONTEXT_MAX_FILES,
):
    """
    Build a context snapshot of files.
    
    Args:
        mode: "snapshot" for full codebase or "changed" for specific files
        changed_paths: List of relative paths (required when mode="changed")
        root_dir: Root directory for file resolution
        include_exts: File extensions to include
        exclude_dirs: Directories to exclude (only used in snapshot mode)
        max_total_bytes: Maximum total bytes to include
        max_file_bytes: Maximum bytes per file
        max_files: Maximum number of files
        
    Returns:
        (snapshot_text, metadata_dict): Snapshot contains file headers and contents.
                                       Metadata includes list of files, byte counts, etc.
    """
    if mode == "changed" and changed_paths is None:
        raise ValueError("changed_paths required when mode='changed'")
    
    include_exts = include_exts or DEFAULT_CONTEXT_EXTS
    include_exts = set(_normalize_ext_list(include_exts))
    exclude_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS

    included_files = []
    total_bytes = 0
    snapshot_parts = []

    # Get file list based on mode
    file_list = _build_file_list(mode, root_dir, changed_paths, include_exts, exclude_dirs)

    # Process files
    for rel_path in file_list:
        if len(included_files) >= max_files or total_bytes >= max_total_bytes:
            break

        path = Path(root_dir) / rel_path
        
        # Check exclusions for snapshot mode
        if mode == "snapshot":
            parts = Path(rel_path).parts
            if any(_should_exclude_dir(p, exclude_dirs) for p in parts):
                continue
        
        ext = path.suffix.lower()
        if ext not in include_exts:
            continue
        
        if not path.exists() or not path.is_file():
            continue
        
        try:
            size = path.stat().st_size
        except OSError:
            continue

        read_limit = min(max_file_bytes, max_total_bytes - total_bytes)
        try:
            with open(path, "rb") as f:
                data = f.read(read_limit)
            content = data.decode("utf-8", errors="replace")
        except OSError:
            continue

        header = f"\n--- {rel_path} ---\n"
        snapshot_parts.append(header)
        snapshot_parts.append(content)
        if size > read_limit:
            snapshot_parts.append("\n[TRUNCATED]\n")
        included_files.append(rel_path)
        total_bytes += len(header.encode("utf-8")) + len(data)

    meta = {
        "included_files": included_files,
        "total_bytes": total_bytes,
        "max_total_bytes": max_total_bytes,
        "max_file_bytes": max_file_bytes,
        "max_files": max_files,
        "truncated": total_bytes >= max_total_bytes or len(included_files) >= max_files,
    }
    return "".join(snapshot_parts), meta


# Backward compatibility wrappers
def build_codebase_snapshot(
    root_dir=".",
    include_exts=None,
    exclude_dirs=None,
    max_total_bytes=DEFAULT_CONTEXT_MAX_BYTES,
    max_file_bytes=DEFAULT_CONTEXT_MAX_FILE_BYTES,
    max_files=DEFAULT_CONTEXT_MAX_FILES,
):
    """Build a snapshot of the entire codebase. (Wrapper for build_context)"""
    return build_context(
        mode="snapshot",
        root_dir=root_dir,
        include_exts=include_exts,
        exclude_dirs=exclude_dirs,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
        max_files=max_files,
    )


def build_changed_files_snapshot(
    changed_paths,
    root_dir=".",
    include_exts=None,
    max_total_bytes=DEFAULT_CONTEXT_MAX_BYTES,
    max_file_bytes=DEFAULT_CONTEXT_MAX_FILE_BYTES,
    max_files=DEFAULT_CONTEXT_MAX_FILES,
):
    """Build a snapshot of changed files. (Wrapper for build_context)"""
    return build_context(
        mode="changed",
        changed_paths=changed_paths,
        root_dir=root_dir,
        include_exts=include_exts,
        max_total_bytes=max_total_bytes,
        max_file_bytes=max_file_bytes,
        max_files=max_files,
    )


def apply_file_ops(file_ops):
    """
    Apply basic filesystem operations (move/delete/mkdir).

    Expected schema:
      {"op": "move", "from": "a", "to": "b"}
      {"op": "delete", "path": "a"}
      {"op": "mkdir", "path": "a"}
      
    Returns:
        (applied_operations, errors): Lists of successful ops and error messages
    """
    if not file_ops:
        return [], []

    applied = []
    errors = []

    for op in file_ops:
        if not isinstance(op, dict):
            errors.append(f"Invalid file_op entry (not object): {op!r}")
            continue
        op_type = (op.get("op") or "").strip().lower()

        try:
            if op_type == "move":
                src = op.get("from")
                dst = op.get("to")
                if not src or not dst:
                    raise ValueError("move requires 'from' and 'to'")
                _ensure_writable(src)
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                shutil.move(src, dst)
                applied.append(f"move:{src}->{dst}")
            elif op_type == "delete":
                target = op.get("path")
                if not target:
                    raise ValueError("delete requires 'path'")
                if os.path.isdir(target):
                    _rmtree_force(target)
                elif os.path.exists(target):
                    _ensure_writable(target)
                    os.remove(target)
                applied.append(f"delete:{target}")
            elif op_type == "mkdir":
                target = op.get("path")
                if not target:
                    raise ValueError("mkdir requires 'path'")
                os.makedirs(target, exist_ok=True)
                applied.append(f"mkdir:{target}")
            else:
                errors.append(f"Unknown op '{op_type}'")
        except Exception as e:
            errors.append(f"{op_type} failed: {e}")

    return applied, errors
