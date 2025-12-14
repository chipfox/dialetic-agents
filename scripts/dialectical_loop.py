import os
import re
import json
import shutil
import subprocess
import sys
import time
import argparse
import tempfile
from pathlib import Path
from datetime import datetime, timezone


SUBPROCESS_TEXT_ENCODING = "utf-8"


def configure_stdio_utf8():
    """Best-effort: make console I/O resilient to Unicode on Windows."""
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding=SUBPROCESS_TEXT_ENCODING, errors="replace")
            except Exception:
                pass


def check_project_write_access(project_dir: Path):
    """Fail fast if the current project directory is not writable.

    This often happens on Windows when the repo lives under OneDrive/Documents
    and Controlled Folder Access blocks python/node from writing.
    """
    test_path = project_dir / ".dialectical-loop-write-test.tmp"
    try:
        test_path.write_bytes(b"ok")
        test_path.unlink(missing_ok=True)
        return True, ""
    except PermissionError as e:
        hint = (
            f"Permission denied writing to project directory: {project_dir}\n"
            "Common causes on Windows:\n"
            "- Windows Security > Ransomware protection (Controlled folder access) blocking python/node\n"
            "- Repo located under OneDrive/Documents/Desktop with special protection or sync locks\n\n"
            "Fix options:\n"
            "1) Move the repo to a non-protected path (e.g., C:\\dev\\YourRepo) and rerun\n"
            "2) Allowlist your Python and Node executables in Controlled Folder Access\n"
            "3) Ensure files/folders are not read-only and you own the directory\n\n"
            f"Underlying error: {e}"
        )
        return False, hint
    except OSError as e:
        return False, f"Unable to write to project directory {project_dir}: {e}"

# Configuration
MAX_TURNS = 10
REQUIREMENTS_FILE = "REQUIREMENTS.md"
SPECIFICATION_FILE = "SPECIFICATION.md"
DEFAULT_COACH_MODEL = "claude-sonnet-4.5"
DEFAULT_PLAYER_MODEL = "gemini-3-pro-preview"
DEFAULT_ARCHITECT_MODEL = "claude-sonnet-4.5"

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

DEFAULT_CONTEXT_MODE = "auto"

# Skill layout (repo root contains agents/ and scripts/)
SKILL_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = SKILL_ROOT / "agents"

# ============================================================================
# RunLog: Structured Observability
# ============================================================================


def utc_now_iso():
    """Timezone-aware UTC timestamps with trailing Z."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

class RunLog:
    """Captures observability events for a dialectical loop run."""

    def __init__(self, verbose=False, quiet=False):
        self.verbose = verbose
        self.quiet = quiet
        self.run_id = f"dialectical-loop-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        self.timestamp_start = utc_now_iso()
        self.turns = []
        self.architect_invoked = False
        self.errors = []

    def log_event(self, turn_number, phase, agent, model, action, result, 
                  input_tokens_est=0, output_tokens_est=0, duration_s=0, 
                  details=None, error=None):
        """Log a single LLM or action event."""
        event = {
            "turn_number": turn_number,
            "phase": phase,
            "agent": agent,
            "model": model,
            "action": action,
            "input_tokens_est": input_tokens_est,
            "output_tokens_est": output_tokens_est,
            "total_tokens_est": input_tokens_est + output_tokens_est,
            "outcome": "error" if error else "success",
            "duration_s": round(duration_s, 2),
            "timestamp": utc_now_iso(),
        }
        if details:
            event.update(details)
        if error:
            event["error"] = str(error)
            self.errors.append(error)
        self.turns.append(event)
        # Incrementally flush to file so watchers see live updates
        self._flush_log_to_file()

    def estimate_tokens(self, text):
        """Simple token estimate: ~4 chars ≈ 1 token."""
        return max(1, len(text) // 4)

    def total_tokens_estimate(self):
        """Sum of all tokens across all turns."""
        return sum(t.get("total_tokens_est", 0) for t in self.turns)

    def get_summary(self):
        """Build a summary of the run."""
        architect_calls = [t for t in self.turns if t.get("agent") == "architect"]
        player_calls = [t for t in self.turns if t.get("agent") == "player"]
        coach_calls = [t for t in self.turns if t.get("agent") == "coach"]
        
        return {
            "total_turns_executed": len(set(
                t.get("turn_number") for t in self.turns if t.get("phase") == "loop"
            )),
            "total_tokens_estimated": self.total_tokens_estimate(),
            "architect_calls": {
                "successful": len([t for t in architect_calls if t["outcome"] == "success"]),
                "failed": len([t for t in architect_calls if t["outcome"] == "error"]),
            },
            "player_calls": {
                "successful": len([t for t in player_calls if t["outcome"] == "success"]),
                "failed": len([t for t in player_calls if t["outcome"] == "error"]),
            },
            "coach_calls": {
                "approved": len([t for t in coach_calls if t.get("decision") == "approved"]),
                "rejected": len([t for t in coach_calls if t.get("decision") == "rejected"]),
                "errors": len([t for t in coach_calls if t["outcome"] == "error"]),
            },
            "errors": self.errors,
        }

    def to_json(self):
        """Serialize the log to JSON."""
        return {
            "run_id": self.run_id,
            "timestamp_start": self.timestamp_start,
            "timestamp_end": utc_now_iso(),
            "verbose": self.verbose,
            "quiet": self.quiet,
            "turns": self.turns,
            "summary": self.get_summary(),
        }

    def write_log_file(self, directory="."):
        """Write JSON log to a timestamped file in the given directory."""
        log_path = Path(directory) / f"{self.run_id}.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, indent=2)
        return str(log_path)

    def tailable_log_path(self, directory="."):
        """Return the log path (string) so callers can show how to watch it."""
        return str(Path(directory) / f"{self.run_id}.json")

    def create_log_file(self, directory="."):
        """Create the log file immediately with a stub so watchers can follow it."""
        self.log_file_path = Path(directory) / f"{self.run_id}.json"
        # Write initial stub so the file exists and can be followed
        self._flush_log_to_file()

    def _flush_log_to_file(self):
        """Write the current state to the log file (called after each event)."""
        if not hasattr(self, 'log_file_path'):
            return
        try:
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                json.dump(self.to_json(), f, indent=2)
        except OSError:
            pass  # Silent fail if we can't write (e.g., permission issue)

    def report(self, status="unknown", message=""):
        """Print a human-readable summary report."""
        summary = self.get_summary()
        total_turns = summary["total_turns_executed"]
        total_tokens = summary["total_tokens_estimated"]
        arch_ok = summary["architect_calls"]["successful"]
        arch_fail = summary["architect_calls"]["failed"]
        player_ok = summary["player_calls"]["successful"]
        player_fail = summary["player_calls"]["failed"]
        coach_approved = summary["coach_calls"]["approved"]
        coach_rejected = summary["coach_calls"]["rejected"]
        coach_errors = summary["coach_calls"]["errors"]

        if not self.quiet:
            print("", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("DIALECTICAL LOOP SUMMARY", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print(f"Status: {status.upper()}", file=sys.stderr)
            print(f"Total turns: {total_turns}", file=sys.stderr)
            print(f"Total tokens (estimated): {total_tokens:,}", file=sys.stderr)
            print(f"Architect: {arch_ok} successful, {arch_fail} failed", file=sys.stderr)
            print(f"Player: {player_ok} successful, {player_fail} failed", file=sys.stderr)
            print(
                f"Coach: {coach_approved} approved, {coach_rejected} rejected, "
                f"{coach_errors} errors",
                file=sys.stderr
            )
            if message:
                print(f"Message: {message}", file=sys.stderr)
            print("=" * 70, file=sys.stderr)
            print("", file=sys.stderr)


def log_print(message, verbose=False, quiet=False):
    """Print to stderr unless quiet is True. Verbose adds extra details."""
    if not quiet:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        prefix = "[VERBOSE]" if verbose else ""
        prefix_str = f" {prefix}" if prefix else ""
        print(f"[{timestamp}]{prefix_str} {message}", file=sys.stderr)

def load_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def save_file(path, content):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

def run_command(command):
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        )
        return (
            f"Command: {command}\nExit Code: {result.returncode}\n"
            f"Output:\n{result.stdout}\nError:\n{result.stderr}"
        ), result.returncode
    except Exception as e:
        return f"Error running command {command}: {e}", 1


def _split_csv_arg(value):
    if not value:
        return []
    items = [v.strip() for v in value.split(",")]
    return [v for v in items if v]


def _normalize_ext_list(exts):
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
    return dirname in exclude_dirs


def build_codebase_snapshot(
    root_dir=".",
    include_exts=None,
    exclude_dirs=None,
    max_total_bytes=DEFAULT_CONTEXT_MAX_BYTES,
    max_file_bytes=DEFAULT_CONTEXT_MAX_FILE_BYTES,
    max_files=DEFAULT_CONTEXT_MAX_FILES,
):
    include_exts = include_exts or DEFAULT_CONTEXT_EXTS
    include_exts = set(_normalize_ext_list(include_exts))
    exclude_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS

    included_files = []
    total_bytes = 0
    snapshot_parts = []

    # Try to use git ls-files first to respect .gitignore
    git_files = []
    try:
        code, out, _ = _run_capture(["git", "ls-files"], cwd=root_dir)
        if code == 0:
            git_files = [f.strip() for f in out.splitlines() if f.strip()]
    except Exception:
        pass

    if git_files:
        # Use git file list
        for rel_path in git_files:
            path = Path(root_dir) / rel_path
            
            # Check exclusions manually just in case
            parts = Path(rel_path).parts
            if any(_should_exclude_dir(p, exclude_dirs) for p in parts):
                continue
                
            ext = path.suffix.lower()
            if ext not in include_exts:
                continue
            
            if len(included_files) >= max_files:
                break
            
            try:
                if not path.exists(): continue
                size = path.stat().st_size
            except OSError:
                continue

            if total_bytes >= max_total_bytes:
                break

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
            
    else:
        # Fallback to os.walk
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in sorted(dirs) if not _should_exclude_dir(d, exclude_dirs)]
            for filename in sorted(files):
                path = Path(root) / filename
                rel_path = os.path.relpath(path, root_dir)
                ext = path.suffix.lower()
                if ext not in include_exts:
                    continue

                if len(included_files) >= max_files:
                    break
                try:
                    size = path.stat().st_size
                except OSError:
                    continue

                if total_bytes >= max_total_bytes:
                    break

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

            if len(included_files) >= max_files or total_bytes >= max_total_bytes:
                break

    meta = {
        "included_files": included_files,
        "total_bytes": total_bytes,
        "max_total_bytes": max_total_bytes,
        "max_file_bytes": max_file_bytes,
        "max_files": max_files,
        "truncated": total_bytes >= max_total_bytes or len(included_files) >= max_files,
    }
    return "".join(snapshot_parts), meta


def _run_capture(argv, cwd="."):
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


def build_changed_files_snapshot(
    changed_paths,
    root_dir=".",
    include_exts=None,
    max_total_bytes=DEFAULT_CONTEXT_MAX_BYTES,
    max_file_bytes=DEFAULT_CONTEXT_MAX_FILE_BYTES,
    max_files=DEFAULT_CONTEXT_MAX_FILES,
):
    include_exts = include_exts or DEFAULT_CONTEXT_EXTS
    include_exts = set(_normalize_ext_list(include_exts))

    included_files = []
    total_bytes = 0
    snapshot_parts = []

    for rel_path in changed_paths:
        if len(included_files) >= max_files or total_bytes >= max_total_bytes:
            break

        path = Path(root_dir) / rel_path
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


def apply_file_ops(file_ops):
    """Apply basic filesystem operations (move/delete/mkdir).

    Expected schema:
      {"op": "move", "from": "a", "to": "b"}
      {"op": "delete", "path": "a"}
      {"op": "mkdir", "path": "a"}
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
                os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
                shutil.move(src, dst)
                applied.append(f"move:{src}->{dst}")
            elif op_type == "delete":
                target = op.get("path")
                if not target:
                    raise ValueError("delete requires 'path'")
                if os.path.isdir(target):
                    shutil.rmtree(target)
                elif os.path.exists(target):
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

def get_github_token():
    try:
        token = subprocess.check_output(
            ["gh", "auth", "token"],
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        ).strip()
        return token
    except Exception as e:
        print(f"Error getting GitHub token: {e}")
        return None

def get_llm_response(
    system_prompt,
    user_prompt,
    model="claude-sonnet-4.5",
    run_log=None,
    turn_number=0,
    agent="unknown"
):
    """Call Copilot with observability logging."""
    token = os.environ.get("GITHUB_TOKEN") or get_github_token()
    if not token:
        print("Error: Could not find GITHUB_TOKEN. Please login with 'gh auth login'.")
        sys.exit(1)
    
    # Set token for subprocess
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token

    # Estimate input tokens
    input_text = f"{system_prompt}\n\n{user_prompt}"
    input_tokens_est = run_log.estimate_tokens(input_text) if run_log else 0

    # Combine system and user prompt into a temp file.
    # Avoid writing into the user's project directory.
    full_prompt = input_text
    temp_dir = Path(tempfile.gettempdir()) / "dialectical-loop"
    temp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        suffix=".txt",
        prefix="context_",
        delete=False,
        dir=str(temp_dir),
    ) as temp_file:
        temp_file.write(full_prompt)
        abs_path = temp_file.name
    cli_prompt = (
        f"Read the file '{abs_path}'. It contains your instructions and input data. "
        "Follow the instructions in that file exactly. Output only your final answer."
    )

    cmd = [
        "copilot",
        "--model",
        model,
        "--allow-all-paths",
        "--silent",
        "-p",
        cli_prompt,
    ]
    
    start_time = time.time()
    try:
        # Prefer shell=False for predictable argv handling; fallback to shell=True if needed.
        try:
            result = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                encoding=SUBPROCESS_TEXT_ENCODING,
                errors="replace",
                shell=False,
            )
        except FileNotFoundError:
            cmd_str = subprocess.list2cmdline(cmd)
            result = subprocess.run(
                cmd_str,
                env=env,
                capture_output=True,
                text=True,
                encoding=SUBPROCESS_TEXT_ENCODING,
                errors="replace",
                shell=True,
            )
        
        duration_s = time.time() - start_time
        output_tokens_est = run_log.estimate_tokens(result.stdout) if run_log else 0
        
        if result.returncode != 0:
            if run_log:
                run_log.log_event(
                    turn_number=turn_number,
                    phase="loop" if turn_number > 0 else "architect",
                    agent=agent,
                    model=model,
                    action="llm_call",
                    result="failed",
                    input_tokens_est=input_tokens_est,
                    output_tokens_est=0,
                    duration_s=duration_s,
                    error=f"Copilot CLI Error ({result.returncode})"
                )
            print(f"Copilot CLI Error ({result.returncode}):\n{result.stderr}")
            return None
        
        if run_log:
            run_log.log_event(
                turn_number=turn_number,
                phase="loop" if turn_number > 0 else "architect",
                agent=agent,
                model=model,
                action="llm_call",
                result="success",
                input_tokens_est=input_tokens_est,
                output_tokens_est=output_tokens_est,
                duration_s=duration_s,
            )
            
        return result.stdout
    except Exception as e:
        if run_log:
            run_log.log_event(
                turn_number=turn_number,
                phase="loop" if turn_number > 0 else "architect",
                agent=agent,
                model=model,
                action="llm_call",
                result="failed",
                input_tokens_est=input_tokens_est,
                output_tokens_est=0,
                duration_s=time.time() - start_time,
                error=str(e)
            )
        print(f"Error calling Copilot: {e}")
        return None
    finally:
        try:
            os.remove(abs_path)
        except Exception:
            pass

def strip_fenced_block(text):
    if not text:
        return text
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
        return stripped.strip()
    return text

def extract_json(text, run_log=None, turn_number=0, agent="unknown"):
    if not text:
        return None

    candidates = []

    def add_candidate(candidate_text):
        if candidate_text and candidate_text.strip():
            candidates.append(candidate_text.strip())

    def normalize_candidate(candidate_text):
        if not candidate_text:
            return candidate_text
        stripped = candidate_text.strip()
        if stripped.lower().startswith("json\n"):
            stripped = stripped.split("\n", 1)[1].lstrip()
        return stripped

    # Prefer fenced JSON blocks first
    for fence in ["```json", "```JSON", "```"]:
        start = text.find(fence)
        while start != -1:
            block_start = start + len(fence)
            end = text.find("```", block_start)
            if end == -1:
                break
            add_candidate(normalize_candidate(text[block_start:end]))
            start = text.find(fence, end + 3)

    # Add full text as a fallback
    add_candidate(normalize_candidate(text))

    # Add the first detected JSON object slice
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        add_candidate(normalize_candidate(text[first_brace : last_brace + 1]))

    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Attempt 1: Fix trailing commas
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
            
            # Attempt 2: Strip JS-style comments (// ...) which LLMs love to add
            # Be careful not to strip http:// links inside strings
            # Regex: Match // outside of quotes (simplified approximation)
            lines = candidate.splitlines()
            cleaned_lines = []
            for line in lines:
                # Simple strip of // comment at end of line if it looks like a comment
                # This is heuristic but handles the common case: "key": "value", // comment
                if "//" in line:
                    # Check if // is inside quotes? Hard to do perfectly with regex.
                    # Safe bet: if // is after the last quote, it's a comment.
                    # Or if the line has no quotes.
                    quote_count = line.count('"')
                    if quote_count % 2 == 0:
                        # Balanced quotes, check if // is after the last one
                        last_quote = line.rfind('"')
                        comment_start = line.find("//")
                        if comment_start > last_quote:
                            line = line[:comment_start]
                cleaned_lines.append(line)
            
            fixed_comments = "\n".join(cleaned_lines)
            # Re-apply trailing comma fix on top of comment fix
            fixed_comments = re.sub(r",\s*([}\]])", r"\1", fixed_comments)
            
            try:
                return json.loads(fixed_comments)
            except json.JSONDecodeError:
                continue

    # Log parse failure with raw response preview
    error_msg = f"Failed to parse JSON from {agent} response (turn {turn_number})"
    if run_log:
        run_log.log_event(
            turn_number=turn_number,
            phase="loop" if turn_number > 0 else "architect",
            agent=agent,
            model="unknown",
            action="json_parse",
            result="failed",
            error=error_msg,
            details={
                "response_preview": text[:500] if len(text) > 500 else text,
                "response_length": len(text),
                "contains_brace": "{" in text,
                "contains_bracket": "[" in text,
            }
        )
    return None

def run_architect_phase(
    requirements,
    current_files,
    requirements_file,
    spec_file,
    architect_model,
    run_log=None,
    verbose=False,
    quiet=False
):
    log_print(f"Architect ({architect_model}) is analyzing requirements...", verbose=verbose, quiet=quiet)
    architect_prompt = load_file(str(AGENT_DIR / "architect.md"))
    if not architect_prompt.strip():
        print(f"Error: Missing architect prompt at {AGENT_DIR / 'architect.md'}")
        return None
    
    architect_input = (
        f"REQUIREMENTS FILE: {requirements_file}\nREQUIREMENTS:\n{requirements}\n\n"
        f"CURRENT CODEBASE:\n{current_files}\n\n"
    )
    architect_input += (
        f"TASK: Create a detailed technical specification ({spec_file}) for the implementation. "
    )
    architect_input += (
        "Include file paths, data structures, function signatures, "
        "and step-by-step implementation plan. "
    )
    architect_input += (
        "Output ONLY the markdown content of the specification file. "
        "Do not wrap it in JSON."
    )

    response = get_llm_response(
        architect_prompt,
        architect_input,
        model=architect_model,
        run_log=run_log,
        turn_number=0,
        agent="architect"
    )
    
    if response:
        response = strip_fenced_block(response)
        if not response.strip():
            log_print("Architect returned empty specification.", verbose=verbose, quiet=quiet)
            if run_log:
                run_log.log_event(
                    turn_number=0, phase="architect", agent="architect", model=architect_model,
                    action="spec_generation", result="failed", error="Empty specification"
                )
            return None
        save_file(spec_file, response)
        log_print(f"Generated {spec_file}", verbose=verbose, quiet=quiet)
        if run_log:
            run_log.log_event(
                turn_number=0, phase="architect", agent="architect", model=architect_model,
                action="spec_generation", result="success",
                details={"output_file": spec_file}
            )
        return response
    else:
        log_print("Architect failed to generate specification.", verbose=verbose, quiet=quiet)
        if run_log:
            run_log.log_event(
                turn_number=0, phase="architect", agent="architect", model=architect_model,
                action="spec_generation", result="failed", error="No response from LLM"
            )
        return None

def detect_verification_commands(root_dir="."):
    """Detect project type and return relevant verification commands (LSP-like checks)."""
    commands = []
    root = Path(root_dir)
    
    # Node/JS/TS
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                scripts = data.get("scripts", {})
                
                # 1. Build / Typecheck (LSP equivalent)
                if "build" in scripts:
                    commands.append("npm run build")
                elif "typecheck" in scripts:
                    commands.append("npm run typecheck")
                elif (root / "tsconfig.json").exists():
                    # Fallback: try to run tsc directly if installed locally
                    tsc_path = root / "node_modules" / ".bin" / "tsc"
                    if sys.platform == "win32":
                        tsc_path = tsc_path.with_suffix(".cmd")
                    
                    if tsc_path.exists():
                        commands.append(f"{tsc_path} --noEmit")
                
                # 2. Lint
                if "lint" in scripts:
                    commands.append("npm run lint")
                    
        except Exception:
            pass
    elif (root / "tsconfig.json").exists():
        # No package.json but tsconfig exists? Try global tsc or just hope
        # Actually, without package.json, we can't easily guess where tsc is unless global.
        # We'll skip for now to avoid 'command not found' spam.
        pass
            
    return commands

def detect_auto_fix_commands(root_dir="."):
    """Detect available auto-fix/formatting commands."""
    commands = []
    root = Path(root_dir)
    
    pkg_json = root / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                scripts = data.get("scripts", {})
                
                # Prefer explicit fix scripts
                if "lint:fix" in scripts:
                    commands.append("npm run lint:fix")
                elif "format" in scripts:
                    commands.append("npm run format")
                elif "lint" in scripts:
                    # Try appending --fix to standard lint
                    commands.append("npm run lint -- --fix")
        except Exception:
            pass
            
    return commands

def truncate_output(output, max_chars=2000):
    """Smart truncation of command output to save tokens."""
    if not output or len(output) <= max_chars:
        return output
    
    # Keep head and tail
    head_size = max_chars // 3
    tail_size = max_chars - head_size
    
    head = output[:head_size]
    tail = output[-tail_size:]
    
    return f"{head}\n... [OUTPUT TRUNCATED {len(output) - max_chars} CHARS] ...\n{tail}"

def get_repo_file_tree(root_dir=".", exclude_dirs=None, include_exts=None):
    """Get a simple list of file paths to help Coach see missing/misreferenced files.

    Note: This is intentionally names-only (no file contents) to keep token usage low.
    """
    exclude_dirs = exclude_dirs or DEFAULT_EXCLUDE_DIRS
    include_exts = include_exts or DEFAULT_CONTEXT_EXTS
    include_exts = {e.lower() for e in include_exts}
    file_list = []
    
    # Try git ls-files first
    try:
        code, out, _ = _run_capture(["git", "ls-files"], cwd=root_dir)
        if code == 0:
            files = [f.strip() for f in out.splitlines() if f.strip()]
            # Filter exclusions just in case
            for f in files:
                parts = Path(f).parts
                if not any(_should_exclude_dir(p, exclude_dirs) for p in parts):
                    suffix = Path(f).suffix.lower()
                    if suffix in include_exts or suffix == "":
                        file_list.append(f)
            return "\n".join(sorted(file_list))
    except Exception:
        pass
        
    # Fallback to os.walk
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in sorted(dirs) if not _should_exclude_dir(d, exclude_dirs)]
        for filename in sorted(files):
            path = Path(root) / filename
            rel_path = os.path.relpath(path, root_dir)
            suffix = Path(filename).suffix.lower()
            if suffix in include_exts or suffix == "":
                file_list.append(rel_path)
            
    return "\n".join(sorted(file_list))

def main():
    configure_stdio_utf8()
    parser = argparse.ArgumentParser(
        description="Run the Dialectical Autocoding Loop with built-in observability."
    )
    parser.add_argument(
        "--max-turns", type=int, default=MAX_TURNS, help="Maximum number of turns to run."
    )
    parser.add_argument(
        "--requirements-file", default=REQUIREMENTS_FILE, help="Path to requirements markdown file."
    )
    parser.add_argument(
        "--spec-file", default=SPECIFICATION_FILE, help="Path to specification markdown file."
    )
    parser.add_argument(
        "--skip-architect",
        action="store_true",
        help="Skip architect phase even if specification is missing."
    )
    parser.add_argument(
        "--coach-model", default=DEFAULT_COACH_MODEL, help="Model to use for Coach reviews."
    )
    parser.add_argument(
        "--player-model",
        default=DEFAULT_PLAYER_MODEL,
        help="Model to use for Player implementation."
    )
    parser.add_argument(
        "--architect-model",
        default=DEFAULT_ARCHITECT_MODEL,
        help="Model to use for Architect planning."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose output (details on prompts, responses, state)."
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress all terminal output except final summary and log path."
    )
    parser.add_argument(
        "--context-exts",
        default=",".join(DEFAULT_CONTEXT_EXTS),
        help="Comma-separated file extensions to include in context snapshots."
    )
    parser.add_argument(
        "--context-exclude-dirs",
        default=",".join(sorted(DEFAULT_EXCLUDE_DIRS)),
        help="Comma-separated directory names to exclude from snapshots."
    )
    parser.add_argument(
        "--context-max-bytes",
        type=int,
        default=DEFAULT_CONTEXT_MAX_BYTES,
        help="Max total bytes to include in context snapshots."
    )
    parser.add_argument(
        "--context-max-file-bytes",
        type=int,
        default=DEFAULT_CONTEXT_MAX_FILE_BYTES,
        help="Max bytes per file included in context snapshots."
    )
    parser.add_argument(
        "--context-max-files",
        type=int,
        default=DEFAULT_CONTEXT_MAX_FILES,
        help="Max number of files included in context snapshots."
    )
    parser.add_argument(
        "--context-mode",
        choices=["auto", "snapshot", "git-changed"],
        default=DEFAULT_CONTEXT_MODE,
        help="Context strategy: auto uses snapshot then git-changed when possible."
    )
    parser.add_argument(
        "--verify-cmd",
        action="append",
        default=[],
        help="Extra verification command to run after Player edits (repeatable)."
    )
    parser.add_argument(
        "--no-auto-verify",
        action="store_true",
        help="Disable automatic verification (e.g., npm build/lint when package.json exists)."
    )
    parser.add_argument(
        "--coach-focus-recent",
        action="store_true",
        help="Restrict Coach context to only files edited in the current turn (ignoring previous turns)."
    )
    parser.add_argument(
        "--fast-fail",
        action="store_true",
        help="Skip Coach review if verification commands fail (exit code != 0)."
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        help="Attempt to run auto-fixers (e.g. 'npm run lint -- --fix') after Player edits."
    )
    parser.add_argument(
        "--lean-mode",
        action="store_true",
        help="Enable all token-saving features: --fast-fail, --coach-focus-recent, --auto-fix, and --context-mode auto."
    )
    args = parser.parse_args()

    # Apply lean-mode overrides
    if args.lean_mode:
        args.fast_fail = True
        args.coach_focus_recent = True
        args.auto_fix = True
        # Only override context-mode if it's the default, to allow user override
        if args.context_mode == DEFAULT_CONTEXT_MODE:
            args.context_mode = "auto"

    
    max_turns = args.max_turns
    if max_turns < 1:
        print("Error: --max-turns must be >= 1")
        return

    # Initialize observability
    run_log = RunLog(verbose=args.verbose, quiet=args.quiet)
    run_log.create_log_file()  # Create file immediately so it can be watched
    log_print(
        f"Starting Dialectical Autocoding Loop (max_turns={max_turns}, "
        f"verbose={args.verbose}, quiet={args.quiet})", 
        verbose=args.verbose,
        quiet=args.quiet
    )

    # Show user where the observability log will be written and how to tail it
    log_path_preview = run_log.tailable_log_path()
    log_print(
        f"Observability log will be written to: {log_path_preview}",
        verbose=True,
        quiet=args.quiet,
    )
    if not args.quiet:
        log_print(
            "To watch updates as the run proceeds: powershell -Command \"Get-Content -Path '" + log_path_preview + "' -Wait\"",
            verbose=False,
            quiet=args.quiet,
        )

    requirements_file = args.requirements_file
    spec_file = args.spec_file

    ok, reason = check_project_write_access(Path.cwd())
    if not ok:
        log_print(reason, verbose=True, quiet=args.quiet)
        run_log.report(status="failed", message=reason)
        log_path = run_log.write_log_file()
        log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
        return

    try:
        requirements = load_file(requirements_file)
        specification = load_file(spec_file)

        if not requirements.strip() and not specification.strip():
            error_msg = (
                f"Error: Neither {requirements_file} nor {spec_file} found. "
                "Create one of them to proceed."
            )
            print(error_msg)
            log_print(error_msg, verbose=args.verbose, quiet=args.quiet)
            run_log.report(status="failed", message=error_msg)
            log_path = run_log.write_log_file()
            log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
            return

        include_exts = _split_csv_arg(args.context_exts)
        exclude_dirs = set(_split_csv_arg(args.context_exclude_dirs))

        if args.context_mode in {"snapshot", "auto"}:
            current_files, _meta = build_codebase_snapshot(
                root_dir=".",
                include_exts=include_exts,
                exclude_dirs=exclude_dirs,
                max_total_bytes=args.context_max_bytes,
                max_file_bytes=args.context_max_file_bytes,
                max_files=args.context_max_files,
            )
        else:
            changed_paths = get_git_changed_paths(repo_dir=".") or []
            current_files, _meta = build_changed_files_snapshot(
                changed_paths,
                root_dir=".",
                include_exts=include_exts,
                max_total_bytes=args.context_max_bytes,
                max_file_bytes=args.context_max_file_bytes,
                max_files=args.context_max_files,
            )

        if not specification.strip():
            if args.skip_architect:
                error_msg = f"Error: {spec_file} is missing and --skip-architect was provided."
                print(error_msg)
                log_print(error_msg, verbose=args.verbose, quiet=args.quiet)
                run_log.report(status="failed", message=error_msg)
                log_path = run_log.write_log_file()
                log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
                return
            if not requirements.strip():
                error_msg = (
                    f"Error: {spec_file} is missing and requirements are empty; "
                    "cannot generate specification."
                )
                print(error_msg)
                log_print(error_msg, verbose=args.verbose, quiet=args.quiet)
                run_log.report(status="failed", message=error_msg)
                log_path = run_log.write_log_file()
                log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
                return

            specification = run_architect_phase(
                requirements,
                current_files,
                requirements_file,
                spec_file,
                architect_model=args.architect_model,
                run_log=run_log,
                verbose=args.verbose,
                quiet=args.quiet,
            )
            if not specification:
                error_msg = "Aborting due to missing specification."
                print(error_msg)
                log_print(error_msg, verbose=args.verbose, quiet=args.quiet)
                run_log.report(status="failed", message=error_msg)
                log_path = run_log.write_log_file()
                log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
                return
        else:
            log_print(f"Using existing {spec_file}", verbose=args.verbose, quiet=args.quiet)

        coach_prompt = load_file(str(AGENT_DIR / "coach.md"))
        player_prompt = load_file(str(AGENT_DIR / "player.md"))

        if not coach_prompt.strip():
            print(f"Error: Missing coach prompt at {AGENT_DIR / 'coach.md'}")
            return
        if not player_prompt.strip():
            print(f"Error: Missing player prompt at {AGENT_DIR / 'player.md'}")
            return
        
        feedback = "No feedback yet. This is the first turn."

        auto_verify = not args.no_auto_verify
        
        for turn in range(1, max_turns + 1):
            log_print(f"Turn {turn}/{max_turns}", verbose=args.verbose, quiet=args.quiet)
            
            # Reload specs/requirements to capture any updates (e.g. Player marking items DONE)
            requirements = load_file(requirements_file)
            specification = load_file(spec_file)
            
            if not specification.strip():
                log_print("[WARN] SPECIFICATION.md is empty. Player may have pruned it completely.", verbose=True, quiet=args.quiet)

            # --- Player Turn ---
            log_print(f"[Player] ({args.player_model}) Implementing...", verbose=args.verbose, quiet=args.quiet)
            player_input = (
                f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\n"
                f"FEEDBACK FROM PREVIOUS TURN:\n{feedback}"
            )
            
            context_mode = args.context_mode
            if context_mode == "auto" and turn > 1:
                context_mode = "git-changed"

            if context_mode == "git-changed":
                changed_paths = get_git_changed_paths(repo_dir=".") or []
                current_files, meta = build_changed_files_snapshot(
                    changed_paths,
                    root_dir=".",
                    include_exts=include_exts,
                    max_total_bytes=args.context_max_bytes,
                    max_file_bytes=args.context_max_file_bytes,
                    max_files=args.context_max_files,
                )
                if not current_files:
                    current_files, meta = build_codebase_snapshot(
                        root_dir=".",
                        include_exts=include_exts,
                        exclude_dirs=exclude_dirs,
                        max_total_bytes=args.context_max_bytes,
                        max_file_bytes=args.context_max_file_bytes,
                        max_files=args.context_max_files,
                    )
            else:
                current_files, meta = build_codebase_snapshot(
                    root_dir=".",
                    include_exts=include_exts,
                    exclude_dirs=exclude_dirs,
                    max_total_bytes=args.context_max_bytes,
                    max_file_bytes=args.context_max_file_bytes,
                    max_files=args.context_max_files,
                )

            if current_files:
                trunc_note = " (TRUNCATED)" if meta.get("truncated") else ""
                meta_files = len(meta["included_files"])
                meta_bytes = meta["total_bytes"]
                player_input += (
                    "\n\nCURRENT CODEBASE"
                    f"{trunc_note} [files={meta_files}, bytes={meta_bytes}]:"
                    f"\n{current_files}"
                )

            # Player
            player_response = get_llm_response(
                player_prompt,
                player_input,
                model=args.player_model, 
                run_log=run_log,
                turn_number=turn,
                agent="player"
            )
            
            if not player_response:
                log_print(f"[Player] No response.", verbose=args.verbose, quiet=args.quiet)
                continue

            player_data = extract_json(
                player_response, run_log=run_log, turn_number=turn, agent="player"
            )
            
            if not player_data:
                log_print(f"[Player] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)
                feedback = (
                    "Your last response was not valid JSON. Response must be a valid JSON object. "
                    "Please follow the format strictly and wrap output in {...} braces."
                )
                continue
            
            if args.verbose:
                thought = (
                    player_data.get("thought_process")
                    or player_data.get("summary")
                    or "N/A"
                )
                log_print(
                    f"[Player] Thought: {thought}"[:120],
                    verbose=True,
                    quiet=args.quiet
                )
            
            # Apply File Ops (move/delete/mkdir)
            file_ops_applied, file_ops_errors = apply_file_ops(player_data.get("file_ops"))
            if file_ops_applied:
                log_print(
                    f"[Player] Applied {len(file_ops_applied)} file ops.",
                    verbose=args.verbose,
                    quiet=args.quiet,
                )
            if file_ops_errors:
                log_print(
                    f"[Player] File ops errors: {len(file_ops_errors)}",
                    verbose=args.verbose,
                    quiet=args.quiet,
                )

            # Apply Edits
            files_changed = []
            if "files" in player_data:
                for path, content in player_data["files"].items():
                    save_file(path, content)
                    files_changed.append(path)
                log_print(
                    f"[Player] Applied {len(files_changed)} edits.",
                    verbose=args.verbose,
                    quiet=args.quiet
                )
            
            # Auto-Fix (if enabled)
            if args.auto_fix and files_changed:
                fix_cmds = detect_auto_fix_commands(".")
                if fix_cmds:
                    log_print(f"[Auto-Fix] Running {len(fix_cmds)} fixers...", verbose=args.verbose, quiet=args.quiet)
                    for cmd in fix_cmds:
                        out, code = run_command(cmd)
                        if code == 0:
                            log_print(f"[Auto-Fix] '{cmd}' success.", verbose=args.verbose, quiet=args.quiet)
                            run_log.log_event(
                                turn_number=turn,
                                phase="loop",
                                agent="system",
                                model="auto-fix",
                                action="run_fixer",
                                result="success",
                                details={"command": cmd}
                            )
                        else:
                            log_print(f"[Auto-Fix] '{cmd}' failed (ignored).", verbose=args.verbose, quiet=args.quiet)
                            run_log.log_event(
                                turn_number=turn,
                                phase="loop",
                                agent="system",
                                model="auto-fix",
                                action="run_fixer",
                                result="failed",
                                details={"command": cmd, "exit_code": code},
                                error=out[:500]
                            )

            # Check for "Lazy Player" (claims success but no edits)
            if not files_changed and not file_ops_applied and not player_data.get("commands_to_run"):
                log_print("[Player] No actions taken (lazy turn).", verbose=args.verbose, quiet=args.quiet)
                feedback = (
                    "CRITICAL ERROR: You did not perform any actions (no files edited, no file_ops, no commands). "
                    "You MUST modify the codebase to address the feedback. "
                    "If you think no changes are needed, you must explain why in 'thought_process' and run a verification command."
                )
                # Skip Coach review to save tokens/time since nothing changed
                continue
            
            # Run Commands
            command_outputs = ""
            executed_commands = []
            verification_errors = []
            
            player_commands = list(player_data.get("commands_to_run", []))
            for cmd in player_commands:
                output, _code = run_command(cmd)
                executed_commands.append(cmd)
                # Truncate output to save tokens
                trunc_out = truncate_output(output)
                command_outputs += f"Command: {cmd}\nExit Code: {_code}\nOutput:\n{trunc_out}\n\n"

            verify_commands = list(args.verify_cmd)
            if auto_verify:
                detected_cmds = detect_verification_commands(".")
                for cmd in detected_cmds:
                    if cmd not in player_commands and cmd not in verify_commands:
                        verify_commands.append(cmd)

            for cmd in verify_commands:
                output, _code = run_command(cmd)
                executed_commands.append(cmd)
                # Truncate output to save tokens
                trunc_out = truncate_output(output)
                command_outputs += f"Command: {cmd}\nExit Code: {_code}\nOutput:\n{trunc_out}\n\n"
                if _code != 0:
                    verification_errors.append(f"Command '{cmd}' failed with exit code {_code}")

            if executed_commands:
                log_print(
                    f"[Player] Executed {len(executed_commands)} commands.",
                    verbose=args.verbose,
                    quiet=args.quiet,
                )

            # Log Player action
            run_log.log_event(
                turn_number=turn,
                phase="loop",
                agent="player",
                model=args.player_model,
                action="implementation",
                result="success",
                details={
                    "edits_applied": len(files_changed),
                    "file_ops_applied": len(file_ops_applied),
                    "file_ops_errors": len(file_ops_errors),
                    "commands_executed": len(executed_commands),
                    "verification_errors": len(verification_errors),
                }
            )

            # Check for Fast Fail (Verification Failure)
            if args.fast_fail and verification_errors:
                log_print(f"[Fast Fail] Verification failed ({len(verification_errors)} errors). Skipping Coach.", verbose=args.verbose, quiet=args.quiet)
                feedback = (
                    "AUTOMATIC REJECTION (Fast Fail):\n"
                    "The following verification commands failed:\n" + 
                    "\n".join(f"- {e}" for e in verification_errors) + 
                    "\n\nFULL COMMAND OUTPUT:\n" + command_outputs + 
                    "\n\nYou must fix these errors before the Coach will review your code."
                )
                
                # Log the skipped coach turn
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="coach",
                    model="system",
                    action="fast_fail",
                    result="success",
                    details={
                        "decision": "rejected",
                        "reason": "verification_failed",
                        "errors": verification_errors
                    }
                )
                continue

            # --- Coach Turn ---
            log_print(f"[Coach] ({args.coach_model}) Reviewing...", verbose=args.verbose, quiet=args.quiet)
            
            # Include a names-only repo file list ONLY when Coach is focusing on recent edits.
            # This prevents token bloat when the Coach already receives a broad codebase snapshot.
            repo_file_tree = ""
            if args.coach_focus_recent:
                repo_file_tree = get_repo_file_tree(".", exclude_dirs, include_exts=include_exts)
            
            coach_input = (
                f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\n"
                f"PLAYER OUTPUT:\n{json.dumps(player_data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"COMMAND OUTPUTS:\n{command_outputs}\n\n"
                + (f"REPO FILE STRUCTURE (Names Only):\n{repo_file_tree}" if repo_file_tree else "")
            )
            
            # Determine Coach context
            coach_context_paths = None
            if args.coach_focus_recent and files_changed:
                coach_context_paths = files_changed
                log_print(f"[Coach] Focusing on {len(files_changed)} recently edited files.", verbose=args.verbose, quiet=args.quiet)

            if coach_context_paths is not None:
                # Use explicit list of files (recent edits)
                current_files_new, meta_new = build_changed_files_snapshot(
                    coach_context_paths,
                    root_dir=".",
                    include_exts=include_exts,
                    max_total_bytes=args.context_max_bytes,
                    max_file_bytes=args.context_max_file_bytes,
                    max_files=args.context_max_files,
                )
            elif context_mode == "git-changed":
                changed_paths = get_git_changed_paths(repo_dir=".") or []
                current_files_new, meta_new = build_changed_files_snapshot(
                    changed_paths,
                    root_dir=".",
                    include_exts=include_exts,
                    max_total_bytes=args.context_max_bytes,
                    max_file_bytes=args.context_max_file_bytes,
                    max_files=args.context_max_files,
                )
                if not current_files_new:
                    current_files_new, meta_new = build_codebase_snapshot(
                        root_dir=".",
                        include_exts=include_exts,
                        exclude_dirs=exclude_dirs,
                        max_total_bytes=args.context_max_bytes,
                        max_file_bytes=args.context_max_file_bytes,
                        max_files=args.context_max_files,
                    )
            else:
                current_files_new, meta_new = build_codebase_snapshot(
                    root_dir=".",
                    include_exts=include_exts,
                    exclude_dirs=exclude_dirs,
                    max_total_bytes=args.context_max_bytes,
                    max_file_bytes=args.context_max_file_bytes,
                    max_files=args.context_max_files,
                )
            trunc_note = " (TRUNCATED)" if meta_new.get("truncated") else ""
            meta_new_files = len(meta_new["included_files"])
            meta_new_bytes = meta_new["total_bytes"]
            coach_input += (
                "\n\nUPDATED CODEBASE"
                f"{trunc_note} [files={meta_new_files}, bytes={meta_new_bytes}]:"
                f"\n{current_files_new}"
            )

            # Coach
            coach_response = get_llm_response(
                coach_prompt,
                coach_input,
                model=args.coach_model,
                run_log=run_log,
                turn_number=turn,
                agent="coach"
            )
            
            if not coach_response:
                log_print(f"[Coach] No response.", verbose=args.verbose, quiet=args.quiet)
                continue

            coach_data = extract_json(
                coach_response, run_log=run_log, turn_number=turn, agent="coach"
            )
            
            if not coach_data:
                log_print(f"[Coach] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)
                feedback = "Coach failed to review. Proceeding with caution."
                continue
            
            coach_status = coach_data.get("status", "UNKNOWN")
            coach_feedback = coach_data.get("feedback", "")
            log_print(f"[Coach] Status: {coach_status}", verbose=args.verbose, quiet=args.quiet)
            if args.verbose:
                # Log first 200 chars of feedback in verbose mode
                fb_preview = (
                    coach_feedback[:200] + "..." if len(coach_feedback) > 200 else coach_feedback
                )
                log_print(f"[Coach] Feedback: {fb_preview}", verbose=True, quiet=args.quiet)

            # Log Coach decision with full feedback for debugging
            run_log.log_event(
                turn_number=turn,
                phase="loop",
                agent="coach",
                model=args.coach_model,
                action="review",
                result="success",
                details={
                    "decision": "approved" if coach_status == "APPROVED" else "rejected",
                    "reason_length": len(coach_feedback),
                    "feedback_text": coach_feedback,
                }
            )
            
            if coach_status == "APPROVED":
                log_print(
                    "SUCCESS! Coach approved the implementation.",
                    verbose=args.verbose,
                    quiet=args.quiet
                )
                run_log.report(status="success", message="Coach approved implementation.")
                break
                
            feedback = coach_feedback
            
            if turn == max_turns:
                log_print("Max turns reached.", verbose=args.verbose, quiet=args.quiet)
                run_log.report(
                    status="partial",
                    message=f"Max turns ({max_turns}) reached without full approval."
                )

    except KeyboardInterrupt:
        log_print("Loop interrupted by user.", verbose=args.verbose, quiet=False)
        run_log.report(status="interrupted", message="User interrupted the loop.")
    except Exception as e:
        log_print(f"Unexpected error: {e}", verbose=args.verbose, quiet=False)
        run_log.report(status="error", message=str(e))
    finally:
        # Final flush (already done incrementally, but ensure it's written)
        run_log._flush_log_to_file()
        log_path = str(run_log.tailable_log_path())
        log_print(f"Observability log saved: {log_path}", verbose=args.verbose, quiet=args.quiet)

if __name__ == "__main__":
    main()
