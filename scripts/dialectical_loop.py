import os
import re
import json
import ast
import shutil
import stat
import subprocess
import sys
import time
import argparse
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import getpass

# Observability module
from observability import RunLog, log_print

# LLM client module
from llm_client import get_llm_response, extract_json, strip_fenced_block

# Context builder module
from context_builder import (
    build_codebase_snapshot,
    build_changed_files_snapshot,
    apply_file_ops,
    get_git_changed_paths,
    _gather_write_diagnostics,
    _should_exclude_dir,
    DEFAULT_CONTEXT_EXTS,
    DEFAULT_EXCLUDE_DIRS,
    DEFAULT_CONTEXT_MAX_BYTES,
    DEFAULT_CONTEXT_MAX_FILE_BYTES,
    DEFAULT_CONTEXT_MAX_FILES,
)

# Optional TypeScript/JavaScript analyzer module
try:
    from ts_analyzer import (
        extract_relevant_paths_from_output as _extract_relevant_paths_from_output,
        parse_ts_missing_property_error as _parse_ts_missing_property_error,
        resolve_ts_module_to_file as _resolve_ts_module_to_file,
        extract_ts_type_definition_snippet as _extract_ts_type_definition_snippet,
        find_import_for_symbol as _find_import_for_symbol,
        extract_local_import_module_specs as _extract_local_import_module_specs,
        expand_paths_with_direct_imports as _expand_paths_with_direct_imports,
        module_specifiers_for_file as _module_specifiers_for_file,
        is_new_file_referenced as _is_new_file_referenced,
    )
    TS_ANALYZER_AVAILABLE = True
except ImportError:
    TS_ANALYZER_AVAILABLE = False
    # Provide no-op implementations
    def _extract_relevant_paths_from_output(output: str, root_dir: str = ".") -> list[str]:
        return []
    def _parse_ts_missing_property_error(text: str) -> dict:
        return {}
    def _resolve_ts_module_to_file(from_file: str, module_spec: str) -> str:
        return ""
    def _extract_ts_type_definition_snippet(type_file: str, type_name: str, max_lines: int = 120) -> str:
        return ""
    def _find_import_for_symbol(file_head: str, symbol_name: str) -> str:
        return ""
    def _extract_local_import_module_specs(file_path: str, max_lines: int = 120) -> list[str]:
        return []
    def _expand_paths_with_direct_imports(paths: list[str], max_total: int = 12) -> list[str]:
        return paths[:max_total] if paths else []
    def _module_specifiers_for_file(rel_path: str) -> list[str]:
        return []
    def _is_new_file_referenced(new_file: str, edited_file_contents: dict[str, str]) -> bool:
        # Conservative: allow creation if analyzer not available
        return True


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
        hint = f"Unable to write to project directory {project_dir}: {e}"
        # Detect common protected folders (OneDrive, Desktop, Documents)
        try:
            pstr = str(project_dir).lower()
            if "onedrive" in pstr or "desktop" in pstr or "documents" in pstr:
                hint += (
                    "\nNote: The repo path appears to be under a user-synced or protected folder (OneDrive/Desktop/Documents). "
                    "These locations sometimes block programmatic writes; consider moving the repo to a non-synced path like C:\\dev\\YourRepo."
                )
        except Exception:
            pass
        return False, hint

# Configuration
MAX_TURNS = 10
REQUIREMENTS_FILE = "REQUIREMENTS.md"
SPECIFICATION_FILE = "SPECIFICATION.md"
DEFAULT_COACH_MODEL = "claude-sonnet-4.5"
DEFAULT_PLAYER_MODEL = "gemini-3-pro-preview"
DEFAULT_ARCHITECT_MODEL = "claude-sonnet-4.5"

DEFAULT_CONTEXT_MODE = "auto"

# Skill layout (repo root contains agents/ and scripts/)
SKILL_ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = SKILL_ROOT / "agents"

# ============================================================================
# ContextCache: Orchestrator-Level Caching for Token Optimization
# ============================================================================

class ContextCache:
    """
    Orchestrator-level cache to reduce redundant context in LLM calls.
    
    Since GitHub Copilot CLI doesn't expose Anthropic's cache_control parameter,
    we implement caching at the orchestrator level by tracking what content
    has been sent in previous turns and avoiding re-sending unchanged data.
    
    Expected savings: 30-50% token reduction on repeated requirements/specs.
    """
    
    def __init__(self):
        import hashlib
        self.hashlib = hashlib
        self._content_cache = {}  # hash -> (content, first_turn)
        self._turn_fingerprints = {}  # turn -> set of hashes sent
        self._cache_hits = 0
        self._cache_misses = 0
    
    def get_hash(self, content: str) -> str:
        """Generate short hash for content."""
        return self.hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]
    
    def track_content(self, key: str, content: str, turn_number: int) -> tuple[bool, str]:
        """
        Track content for a given key (e.g., 'requirements', 'specification').
        
        Returns:
            (is_cached, content_hash): is_cached=True if this exact content 
                                       was already sent in a previous turn
        """
        content_hash = self.get_hash(content)
        
        # Record that this turn includes this content
        if turn_number not in self._turn_fingerprints:
            self._turn_fingerprints[turn_number] = set()
        self._turn_fingerprints[turn_number].add(content_hash)
        
        # Check if we've seen this exact content before
        if content_hash in self._content_cache:
            self._cache_hits += 1
            return True, content_hash
        else:
            self._cache_misses += 1
            self._content_cache[content_hash] = (content, turn_number)
            return False, content_hash
    
    def get_cache_stats(self) -> dict:
        """Get cache performance metrics for observability."""
        total_requests = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total_requests if total_requests > 0 else 0.0
        
        return {
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "hit_rate": round(hit_rate, 3),
            "unique_contents": len(self._content_cache),
            "turns_tracked": len(self._turn_fingerprints)
        }
    
    def estimate_savings(self, content_length: int) -> int:
        """
        Estimate token savings from caching this content.
        
        Returns: Estimated tokens saved (0 if cache miss, ~content_length/4 if hit)
        """
        # Rough estimate: 4 chars per token
        return content_length // 4


def load_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _spec_progress(spec_text: str) -> dict:
    """Compute spec completion state.

    Supported completion mechanisms:
    - Markdown checklists: '- [ ] item' and '- [x] item'
      Complete iff there are zero unchecked items.
    - Explicit marker: a line like 'Status: COMPLETE' (case-insensitive)

    Returns a dict with keys:
      mode: 'checklist' | 'marker' | 'unknown'
      complete: bool
      total_items: int
      remaining_items: list[str]
      hint: str (optional guidance when mode is unknown)
    """
    text = spec_text or ""

    checklist_re = re.compile(r"(?m)^\s*[-*]\s*\[(?P<state>[ xX])\]\s*(?P<body>.+?)\s*$")
    matches = list(checklist_re.finditer(text))
    if matches:
        remaining = []
        for m in matches:
            state = (m.group("state") or " ").strip().lower()
            body = (m.group("body") or "").strip()
            if state != "x":
                remaining.append(body)
        return {
            "mode": "checklist",
            "complete": len(remaining) == 0,
            "total_items": len(matches),
            "remaining_items": remaining,
        }

    marker_re = re.compile(r"(?mi)^\s*status\s*:\s*complete\b")
    if marker_re.search(text):
        return {
            "mode": "marker",
            "complete": True,
            "total_items": 0,
            "remaining_items": [],
        }

    return {
        "mode": "unknown",
        "complete": False,
        "total_items": 0,
        "remaining_items": [],
        "hint": (
            "Cannot determine completion from SPECIFICATION.md. "
            "Add markdown checkboxes (- [ ] / - [x]) or add a line 'Status: COMPLETE' when finished."
        ),
    }


def _format_open_spec_items(spec_prog: dict, max_items: int = 40) -> str:
    remaining = list(spec_prog.get("remaining_items") or [])
    if not remaining:
        return "(none)"
    shown = remaining[:max_items]
    lines = [f"- [ ] {item}" for item in shown]
    if len(remaining) > max_items:
        lines.append(f"- ... ({len(remaining) - max_items} more)")
    return "\n".join(lines)


def _spec_for_model(spec_text: str, spec_prog: dict, turn: int, max_chars: int = 12000) -> str:
    """Return a token-sparing spec view for prompts.

    - Turn 1: keep full spec (truncated).
    - Later turns: if checklist-based, include only open items + a small header.
    """
    text = spec_text or ""
    if turn <= 1:
        return truncate_output(text, max_chars=max_chars)

    if spec_prog.get("mode") == "checklist":
        header = truncate_output(text, max_chars=min(2500, max_chars))
        open_items = _format_open_spec_items(spec_prog)
        return (
            header
            + "\n\nOPEN SPEC ITEMS (unchecked):\n"
            + open_items
            + "\n\n(Full specification omitted on later turns to save tokens.)"
        )

    # Unknown/marker mode: keep truncated full spec so the model can decide how to mark completion.
    return truncate_output(text, max_chars=max_chars)

def save_file(path, content):
    dirname = os.path.dirname(path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)
    _ensure_writable(path)
    # Use atomic write: write to a temp file in the same dir then replace
    fd, tmp_path = tempfile.mkstemp(dir=dirname or None)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        # Ensure the target (if existing) is writable before replacing
        _ensure_writable(path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def safe_save_file(path, content):
    """Write a file and return (ok, error_message)."""
    try:
        save_file(path, content)
        return True, ""
    except Exception as e:
        # Gather additional diagnostics to help pinpoint permission problems
        try:
            diag = _gather_write_diagnostics(path, e)
        except Exception:
            diag = f"Underlying error: {e}"
        return False, f"save_file failed for '{path}': {e}\n{diag}"


def _basic_balance_check_js_ts(text: str):
    """Heuristic balance check for {}, (), [] in JS/TS/TSX.

    Skips strings and comments; not a full parser, but catches common truncation.
    Returns (ok, error_message).
    """
    if text is None:
        return False, "content is None"

    i = 0
    n = len(text)
    brace = paren = bracket = 0

    in_squote = False
    in_dquote = False
    in_btick = False
    in_line_comment = False
    in_block_comment = False
    escaped = False

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue

        if in_squote or in_dquote or in_btick:
            if escaped:
                escaped = False
                i += 1
                continue
            if ch == "\\":
                escaped = True
                i += 1
                continue
            if in_squote and ch == "'":
                in_squote = False
            elif in_dquote and ch == '"':
                in_dquote = False
            elif in_btick and ch == "`":
                in_btick = False
            i += 1
            continue

        # Not in string/comment
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch == "'":
            in_squote = True
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            i += 1
            continue
        if ch == "`":
            in_btick = True
            i += 1
            continue

        if ch == "{":
            brace += 1
        elif ch == "}":
            brace -= 1
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren -= 1
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket -= 1

        if brace < 0 or paren < 0 or bracket < 0:
            return False, "unbalanced delimiters (extra closing bracket/brace/paren)"

        i += 1

    if in_squote or in_dquote or in_btick:
        return False, "unterminated string literal"
    if in_block_comment:
        return False, "unterminated block comment"
    if brace != 0 or paren != 0 or bracket != 0:
        return False, f"unbalanced delimiters: {{}}={brace}, ()={paren}, []={bracket}"
    return True, ""


def validate_source_text(path: str, content: str):
    """Best-effort validation to avoid destructive truncation writes."""
    try:
        suffix = (Path(path).suffix or "").lower()
    except Exception:
        suffix = ""

    if suffix == ".py":
        try:
            ast.parse(content or "")
        except Exception as e:
            return False, f"Python parse failed: {e}"
        return True, ""

    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        text = content or ""
        ok, err = _basic_balance_check_js_ts(text)
        if not ok:
            return ok, err
        ok_code, err_code = _looks_like_code_js_ts(text, suffix)
        if not ok_code:
            return False, err_code
        return True, ""

    return True, ""


def _looks_like_code_js_ts(text: str, suffix: str) -> tuple[bool, str]:
    # Heuristic guardrail: prevent overwriting source files with plain-English prose.
    # This intentionally stays loose (avoid false positives) but catches the common
    # failure mode: "Fixing the file requires viewing it first..." written into .ts/.tsx.
    stripped = (text or "").strip()
    if not stripped:
        return False, "empty content"

    # Quick wins: presence of typical JS/TS syntax characters.
    if any(tok in stripped for tok in (";", "=>", "export ", "import ", "function ", "class ", "interface ", "type ", "return ")):
        return True, ""

    # JSX/TSX often contains tags.
    if suffix in {".jsx", ".tsx"}:
        if re.search(r"<\s*[A-Za-z][A-Za-z0-9]*\b", stripped):
            return True, ""

    # If it contains braces/parens/brackets at all, it's likely code-like.
    if re.search(r"[\{\}\(\)\[\]]", stripped):
        return True, ""

    # If the first non-empty line looks like a sentence (multiple spaces, ends with a period)
    # and there's no other code signal, treat as likely prose.
    first_line = stripped.splitlines()[0].strip()
    if len(first_line) > 20 and " " in first_line and re.search(r"[a-zA-Z]", first_line) and not re.search(r"[=<>:\-_/\\]", first_line):
        return False, "content does not look like JS/TS source (likely prose)"

    # Otherwise accept (could be e.g. a minimal identifier file).
    return True, ""

def _looks_like_unix_command(command: str) -> bool:
    cmd = (command or "").lstrip()
    if not cmd:
        return False
    first = cmd.split()[0]
    unix_first = {
        "ls",
        "cat",
        "grep",
        "sed",
        "awk",
        "find",
        "chmod",
        "chown",
        "cp",
        "mv",
        "rm",
        "pwd",
        "touch",
        "head",
        "tail",
        "xargs",
        "which",
    }
    if first in unix_first:
        return True
    # Common POSIX-only syntax hints
    if "$(" in cmd or "`" in cmd or cmd.startswith("./"):
        return True
    return False


# _extract_relevant_paths_from_output is now imported from ts_analyzer module


def _read_file_head(rel_path: str, max_lines: int = 40, max_chars: int = 4000) -> str:
    """Read the first N lines of a file (repo-relative) with lightweight line numbers."""
    try:
        p = Path(rel_path)
        if p.is_absolute():
            abs_path = p
        else:
            abs_path = Path(".") / p
        if not abs_path.exists() or not abs_path.is_file():
            return ""

        lines = []
        total = 0
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i > max_lines:
                    break
                chunk = f"{i:>3} | {line.rstrip()}"
                total += len(chunk) + 1
                if total > max_chars:
                    break
                lines.append(chunk)
        return "\n".join(lines).rstrip()
    except Exception:
        return ""


def _extract_command_output_section(command_outputs: str, needle: str) -> str:
    """Extract the Output section for the first command whose line contains `needle`."""
    text = command_outputs or ""
    if not text:
        return ""
    sections = text.split("Command: ")
    for sec in sections:
        if not sec.strip():
            continue
        first_line_end = sec.find("\n")
        cmd_line = sec[:first_line_end] if first_line_end != -1 else sec
        if needle in cmd_line:
            # Find Output:\n marker
            out_marker = "Output:\n"
            idx = sec.find(out_marker)
            if idx == -1:
                return sec.strip()
            return sec[idx + len(out_marker) :].strip()
    return ""


def _extract_first_ts_error_block(text: str, max_chars: int = 1200) -> str:
    """Best-effort extraction of the first TS/Next.js error block from build output."""
    s = (text or "").strip()
    if not s:
        return ""

    # Prefer Next.js style blocks anchored by "Failed to compile." or "Type error:".
    anchor_idx = s.find("Failed to compile.")
    if anchor_idx == -1:
        anchor_idx = s.find("Type error:")
    if anchor_idx == -1:
        # Fallback: first occurrence of "error" line
        m = re.search(r"(?im)^.*\berror\b.*$", s)
        anchor_idx = m.start(0) if m else 0

    chunk = s[anchor_idx:]
    # Stop at a repeated "Command:" marker if present (defensive)
    stop = chunk.find("Command:")
    if stop != -1:
        chunk = chunk[:stop]

    return truncate_output(chunk.strip(), max_chars=max_chars) or ""


# TS analyzer functions now imported from ts_analyzer module:
# - _parse_ts_missing_property_error
# - _resolve_ts_module_to_file  
# - _extract_ts_type_definition_snippet
# - _find_import_for_symbol


def summarize_command_outputs(command_outputs: str) -> str:
    """Create a small, high-signal summary of command outputs to reduce tokens."""
    if not command_outputs:
        return ""

    build_out = _extract_command_output_section(command_outputs, "npm run build")
    lint_out = _extract_command_output_section(command_outputs, "npm run lint")

    primary_err = _extract_first_ts_error_block(build_out) if build_out else ""
    if not primary_err:
        primary_err = _extract_first_ts_error_block(command_outputs)

    paths = _extract_relevant_paths_from_output(command_outputs, root_dir=".")
    paths = paths[:10]

    expected_got_lines = []
    for line in (build_out or command_outputs).splitlines():
        if re.search(r"\bExpected\b|\bReceived\b|\bbut required\b|\bassignable\b|\bdoes not exist on type\b", line):
            expected_got_lines.append(line.strip())
        if len(expected_got_lines) >= 12:
            break

    parts = []
    if primary_err:
        parts.append("PRIMARY ERROR (first block):\n" + primary_err)
    if paths:
        parts.append("FILES MENTIONED:\n" + "\n".join(f"- {p}" for p in paths))
    if expected_got_lines:
        parts.append("KEY LINES:\n" + "\n".join(f"- {l}" for l in expected_got_lines))
    if lint_out and not build_out:
        parts.append("LINT (excerpt):\n" + truncate_output(lint_out, max_chars=800))
    return "\n\n".join(parts).strip()


# More TS analyzer functions now imported from ts_analyzer module:
# - _extract_local_import_module_specs
# - _expand_paths_with_direct_imports
# - _module_specifiers_for_file
# - _is_new_file_referenced


def _run_shell_command(command: str, shell_kind: str):
    shell_kind = (shell_kind or "auto").lower()

    if os.name != "nt":
        return subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        )

    if shell_kind == "cmd":
        args = ["cmd", "/d", "/s", "/c", command]
        return subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        )

    if shell_kind == "powershell":
        args = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ]
        return subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        )

    if shell_kind == "wsl":
        args = ["wsl", "bash", "-lc", command]
        return subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            encoding=SUBPROCESS_TEXT_ENCODING,
            errors="replace",
        )

    # auto
    if _looks_like_unix_command(command):
        try:
            return _run_shell_command(command, "wsl")
        except Exception:
            pass
    try:
        return _run_shell_command(command, "powershell")
    except Exception:
        return _run_shell_command(command, "cmd")


def run_command(command, shell_kind="auto"):
    try:
        result = _run_shell_command(command, shell_kind)
        return (
            f"Command: {command}\nExit Code: {result.returncode}\n"
            f"Output:\n{result.stdout}\nError:\n{result.stderr}"
        ), result.returncode
    except Exception as e:
        return f"Error running command {command}: {e}", 1


def _split_csv_arg(value):
    """Split comma-separated argument string."""
    if not value:
        return []
    items = [v.strip() for v in value.split(",")]
    return [v for v in items if v]


def run_architect_phase(
    requirements,
    current_files,
    requirements_file,
    spec_file,
    architect_model,
    run_log=None,
    verbose=False,
    quiet=False,
    feedback=None
):
    if feedback:
        log_print(f"Architect ({architect_model}) is replanning based on Coach feedback...", verbose=verbose, quiet=quiet)
    else:
        log_print(f"Architect ({architect_model}) is analyzing requirements...", verbose=verbose, quiet=quiet)
    
    architect_prompt = load_file(str(AGENT_DIR / "architect.md"))
    if not architect_prompt.strip():
        print(f"Error: Missing architect prompt at {AGENT_DIR / 'architect.md'}")
        return None
    
    architect_input = (
        f"REQUIREMENTS FILE: {requirements_file}\nREQUIREMENTS:\n{requirements}\n\n"
        f"CURRENT CODEBASE:\n{current_files}\n\n"
    )
    
    if feedback:
        architect_input += (
            f"FEEDBACK_FROM_COACH:\n{feedback}\n\n"
            f"TASK: The Coach has identified a fundamental design flaw. Review the feedback and UPDATE the existing specification ({spec_file}) to address the concerns. "
            "Preserve any working implementations while fixing the architectural issue. "
        )
    else:
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
        ok, err = safe_save_file(spec_file, response)
        if not ok:
            log_print(f"[Architect] {err}", verbose=verbose, quiet=quiet)
            if run_log:
                run_log.log_event(
                    turn_number=0,
                    phase="architect",
                    agent="architect",
                    model=model,
                    action="write_spec",
                    result="error",
                    error=err,
                )
            return None
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

def extract_file_mentions(text: str) -> list:
    """Extract file paths mentioned in text (e.g., Coach feedback).
    
    Returns list of unique file paths found in the text.
    """
    if not text:
        return []
    pattern = r'[\w/.-]+\.(?:ts|tsx|js|jsx|py|md|json|yaml|yml)'
    return list(set(re.findall(pattern, text)))

def extract_error_fingerprints(verification_output: str) -> set:
    """Extract unique error signatures from TypeScript/lint verification.
    
    Format: 'file:line:error_code' for deterministic tracking across turns.
    
    Examples:
    - TypeScript: 'app/api/route.ts:45:TS2304'
    - ESLint: 'components/Widget.tsx:120:react-hooks/exhaustive-deps'
    """
    if not verification_output:
        return set()
    
    errors = set()
    
    # TypeScript: "path/file.ts(line,col): error TSxxxx:"
    ts_pattern = r'([\w/.-]+\.tsx?)\((\d+),\d+\): error (TS\d+):'
    for file, line, code in re.findall(ts_pattern, verification_output):
        errors.add(f"{file}:{line}:{code}")
    
    # ESLint: "path/file.ts:line:col: message [rule-name]"
    eslint_pattern = r'([\w/.-]+\.tsx?):(\d+):\d+:.+?\[([^\]]+)\]'
    for file, line, rule in re.findall(eslint_pattern, verification_output):
        errors.add(f"{file}:{line}:{rule}")
    
    return errors

def calculate_feedback_coverage(mentioned_files: list, edited_files: list) -> float:
    """Calculate % of Coach-mentioned files that Player actually edited.
    
    Returns:
        1.0 if no files mentioned (perfect coverage by default)
        0.0 to 1.0 representing coverage percentage
    """
    if not mentioned_files:
        return 1.0  # No files mentioned = perfect coverage
    mentioned_set = set(mentioned_files)
    edited_set = set(edited_files)
    addressed = mentioned_set & edited_set
    return len(addressed) / len(mentioned_set)

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
        "--command-shell",
        default="auto",
        choices=["auto", "powershell", "cmd", "wsl"],
        help=(
            "Shell for running commands. On Windows, auto prefers PowerShell and may use WSL for Unix-like commands."
        ),
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
        "--check-writes",
        action="store_true",
        help="Perform a pre-flight write check (attempt to write probe file) and exit with diagnostics.",
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
        "--max-fast-fail-retries",
        type=int,
        default=2,
        help=(
            "Maximum number of fast-fail retries allowed per turn before forcing a Coach review. "
            "Helps avoid spending many Player calls without feedback."
        ),
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
    
    # Initialize context caching for token optimization
    context_cache = ContextCache()
    
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

    if args.check_writes:
        diag = _gather_write_diagnostics(str(Path.cwd()), PermissionError("write-check"))
        log_print("Write diagnostics:\n" + diag, verbose=True, quiet=False)
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

        # A "turn" is defined as a full cycle that includes a Coach model call.
        # If Coach is skipped (e.g., invalid Player JSON, write failures, fast-fail),
        # we do NOT consume a turn; the next iteration reuses the same turn number.
        turn = 1
        skipped_without_coach = 0
        max_skips_without_coach = max(50, max_turns * 10)

        last_skip_reason = ""
        last_fast_fail_outputs = ""
        last_fast_fail_errors: list[str] = []
        fast_fail_retries_this_turn = 0
        zero_edits_streak = 0  # Track consecutive turns with 0 edits to detect stuck loops
        
        # Inter-agent communication tracking
        mentioned_files = []  # Files Coach mentions in feedback (for next turn's Player response tracking)
        previous_error_fingerprints = set()  # Error fingerprints from previous turn (for persistence detection)

        while turn <= max_turns:
            log_print(f"Turn {turn}/{max_turns}", verbose=args.verbose, quiet=args.quiet)
            
            # Reload specs/requirements to capture any updates (e.g. Player marking items DONE)
            requirements = load_file(requirements_file)
            specification = load_file(spec_file)

            spec_prog = _spec_progress(specification)
            spec_for_prompt = _spec_for_model(specification, spec_prog, turn)
            
            # Track context for caching analysis
            req_cached, req_hash = context_cache.track_content('requirements', requirements, turn)
            spec_cached, spec_hash = context_cache.track_content('specification', specification, turn)
            
            if not specification.strip():
                log_print("[WARN] SPECIFICATION.md is empty. Player may have pruned it completely.", verbose=True, quiet=args.quiet)

            # --- Player Turn ---
            log_print(f"[Player] ({args.player_model}) Implementing...", verbose=args.verbose, quiet=args.quiet)

            feedback_for_player = (feedback or "").strip()
            if not feedback_for_player:
                if turn == 1:
                    feedback_for_player = (
                        "NONE (Turn 1 baseline). You MUST take action: implement Turn 1 items from SPECIFICATION."
                    )
                else:
                    feedback_for_player = "NONE"

            baseline_verify_cmds = []
            if auto_verify:
                try:
                    baseline_verify_cmds = detect_verification_commands(".")
                except Exception:
                    baseline_verify_cmds = []

            min_context_fast_fail_retry = (last_skip_reason == "fast-fail")

            player_input = (
                f"REQUIREMENTS:\n{requirements}\n\n"
                f"SPECIFICATION:\n{spec_for_prompt}\n\n"
                f"SPEC PROGRESS:\n"
                f"- mode: {spec_prog.get('mode')}\n"
                f"- complete: {spec_prog.get('complete')}\n"
                f"- total_items: {spec_prog.get('total_items')}\n"
                f"- remaining_items: {len(spec_prog.get('remaining_items') or [])}\n"
                + (f"- hint: {spec_prog.get('hint')}\n" if spec_prog.get('hint') else "")
                + "\n"
                f"FEEDBACK FROM PREVIOUS TURN:\n{feedback_for_player}"
            )

            # Hard rule: success is only allowed when the specification is explicitly marked complete.
            player_input += (
                "\n\nSUCCESS CRITERIA (MANDATORY):\n"
                "- Do NOT claim the task is complete unless SPECIFICATION.md is marked complete.\n"
                "- If items are done, mark them as completed in SPECIFICATION.md (checkboxes - [x]) or add 'Status: COMPLETE'.\n"
                "- If you run verification only (0 edits), you MUST still update SPECIFICATION.md when appropriate to avoid wasted tokens."
            )

            if baseline_verify_cmds:
                player_input += (
                    "\n\nVERIFICATION COMMANDS AVAILABLE (pick at least one):\n"
                    + "\n".join(f"- {c}" for c in baseline_verify_cmds)
                )
            
            # Context for Player: full snapshot on normal turns, minimal snapshot on fast-fail retries.
            if min_context_fast_fail_retry:
                relevant_paths = _extract_relevant_paths_from_output(last_fast_fail_outputs, root_dir=".")
                relevant_paths = relevant_paths[: max(1, min(8, args.context_max_files))]
                expanded_paths = _expand_paths_with_direct_imports(
                    relevant_paths, max_total=max(4, min(12, args.context_max_files))
                )

                player_input += "\n\nFAST-FAIL RETRY (MINIMAL CONTEXT):\n"
                if last_fast_fail_errors:
                    player_input += "Failing checks:\n" + "\n".join(f"- {e}" for e in last_fast_fail_errors) + "\n"
                if last_fast_fail_outputs:
                    summary = summarize_command_outputs(last_fast_fail_outputs)
                    if summary:
                        player_input += "\nCOMMAND OUTPUT SUMMARY:\n" + summary + "\n"
                    else:
                        player_input += "\nFailing command output (truncated):\n" + truncate_output(last_fast_fail_outputs) + "\n"

                if relevant_paths:
                    # Show the header of the primary failing file to reveal imports/source-of-truth types.
                    head = _read_file_head(relevant_paths[0], max_lines=40, max_chars=3500)
                    if head:
                        player_input += (
                            f"\nFAILING FILE HEADER (first ~40 lines): {relevant_paths[0]}\n"
                            + head
                            + "\n"
                        )

                        # If this is a TS missing-property error, include the type source-of-truth snippet.
                        err_info = _parse_ts_missing_property_error(last_fast_fail_outputs)
                        if err_info.get("type") and err_info.get("property"):
                            mod = _find_import_for_symbol(head, err_info["type"])
                            if mod:
                                resolved = _resolve_ts_module_to_file(relevant_paths[0], mod)
                                if resolved:
                                    snippet = _extract_ts_type_definition_snippet(resolved, err_info["type"])
                                    if snippet:
                                        player_input += (
                                            f"\nTYPE SOURCE OF TRUTH: {err_info['type']} (from {resolved})\n"
                                            + snippet
                                            + "\n"
                                        )

                    rel_files, rel_meta = build_changed_files_snapshot(
                        expanded_paths,
                        root_dir=".",
                        include_exts=include_exts,
                        max_total_bytes=min(args.context_max_bytes, 30_000),
                        max_file_bytes=min(args.context_max_file_bytes, 10_000),
                        max_files=min(args.context_max_files, len(expanded_paths)),
                    )
                    trunc_note = " (TRUNCATED)" if rel_meta.get("truncated") else ""
                    meta_files = len(rel_meta["included_files"])
                    meta_bytes = rel_meta["total_bytes"]
                    player_input += (
                        "\nRELEVANT FILES"
                        f"{trunc_note} [files={meta_files}, bytes={meta_bytes}]:\n{rel_files}"
                    )
                else:
                    player_input += "\n(No file paths could be extracted from failing output.)\n"
            else:
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
                # Reset fast-fail state to prevent stale context on next iteration
                last_skip_reason = ""
                last_fast_fail_outputs = ""
                last_fast_fail_errors = []
                fast_fail_retries_this_turn = 0
                skipped_without_coach += 1
                if skipped_without_coach > max_skips_without_coach:
                    error_msg = (
                        "Aborting: too many iterations without a Coach review. "
                        "Player is repeatedly failing before Coach can run."
                    )
                    log_print(error_msg, verbose=True, quiet=args.quiet)
                    run_log.report(status="failed", message=error_msg)
                    break
                continue

            player_data = extract_json(
                player_response, run_log=run_log, turn_number=turn, agent="player"
            )
            
            if not player_data:
                # Check if response appears truncated
                is_truncated = False
                if player_response:
                    last_chars = player_response[-50:].strip()
                    if last_chars and not last_chars.endswith(("}", "]", '"', ".", "!", "?", ")", ";", ",")):
                        is_truncated = True
                    open_count = player_response.count("{") - player_response.count("}")
                    if open_count > 0:
                        is_truncated = True
                
                truncation_hint = ""
                if is_truncated:
                    log_print(f"[Player] Invalid JSON output (response appears truncated).", verbose=args.verbose, quiet=args.quiet)
                    truncation_hint = (
                        "Your previous response appears to have been CUT OFF mid-response. "
                        "This may indicate you hit a token limit. "
                        "Please provide a COMPLETE, SHORTER response with valid JSON.\n"
                    )
                else:
                    log_print(f"[Player] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)

                # Attempt a single in-turn repair to avoid burning a full turn.
                repair_input = (
                    "Your previous response was NOT valid JSON.\n"
                    + truncation_hint +
                    "Return ONLY one fenced ```json code block with a single JSON object matching the required schema.\n"
                    "No prose, no markdown outside the code fence, no commentary.\n\n"
                    "PREVIOUS RESPONSE (for repair):\n"
                    + truncate_output(player_response, max_chars=4000)
                )
                repair_response = get_llm_response(
                    player_prompt,
                    repair_input,
                    model=args.player_model,
                    run_log=run_log,
                    turn_number=turn,
                    agent="player",
                )
                if repair_response:
                    player_data = extract_json(
                        repair_response, run_log=run_log, turn_number=turn, agent="player"
                    )

                if not player_data:
                    feedback = (
                        "Your last response was not valid JSON. Response must be a valid JSON object. "
                        "Return ONLY one fenced ```json block containing a single JSON object."
                    )
                    # Reset fast-fail state to prevent stale context on next iteration
                    last_skip_reason = ""
                    last_fast_fail_outputs = ""
                    last_fast_fail_errors = []
                    fast_fail_retries_this_turn = 0
                    skipped_without_coach += 1
                    if skipped_without_coach > max_skips_without_coach:
                        error_msg = (
                            "Aborting: too many iterations without a Coach review due to invalid Player JSON."
                        )
                        log_print(error_msg, verbose=True, quiet=args.quiet)
                        run_log.report(status="failed", message=error_msg)
                        break
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
            file_write_errors = []
            new_files_created = []
            if "files" in player_data:
                # Determine which files are new before writing
                for path in player_data["files"].keys():
                    try:
                        if not Path(path).exists():
                            new_files_created.append(path)
                    except Exception:
                        pass

                # Guardrail: if Player creates a new file, ensure it is referenced.
                # This prevents "invented" helper components that are never imported.
                for nf in list(new_files_created):
                    if not _is_new_file_referenced(nf, player_data.get("files") or {}):
                        # Determine file pattern for better diagnostics
                        normalized = nf.replace("\\", "/")
                        file_pattern = "unknown"
                        if "/api/" in normalized and normalized.endswith("route.ts"):
                            file_pattern = "Next.js API route"
                        elif "/app/" in normalized and "page." in normalized:
                            file_pattern = "Next.js page route"
                        else:
                            file_pattern = "code file"
                        
                        error_msg = (
                            f"Refusing to create new file '{nf}': no import/reference found for it. "
                            "If you create a new file, you must also add/adjust an import that references it."
                        )
                        file_write_errors.append(error_msg)
                        
                        # Log guardrail telemetry
                        run_log.log_event(
                            turn_number=turn,
                            phase="loop",
                            agent="system",
                            model="guardrail",
                            action="block_file_creation",
                            result="rejected",
                            details={
                                "blocked_file": nf,
                                "file_pattern": file_pattern,
                                "guardrail_rule": "require_import_reference",
                                "suggestion": "Create import/reference first, or file may be framework route"
                            },
                            error=error_msg
                        )
                        
                        # Remove from write set to avoid creating it.
                        try:
                            del player_data["files"][nf]
                        except Exception:
                            pass
                for path, content in player_data["files"].items():
                    ok_syntax, err_syntax = validate_source_text(path, content)
                    if not ok_syntax:
                        file_write_errors.append(
                            f"Refusing to write '{path}': content failed validation ({err_syntax}). "
                            "This usually means the model output is truncated; resend FULL file content."
                        )
                        continue
                    ok, err = safe_save_file(path, content)
                    if ok:
                        files_changed.append(path)
                    else:
                        file_write_errors.append(err)
                log_print(
                    f"[Player] Applied {len(files_changed)} edits.",
                    verbose=args.verbose,
                    quiet=args.quiet
                )
            
            # Track Player response to Coach feedback from previous turn
            if turn > 1 and mentioned_files:  # Skip Turn 1 (no prior feedback)
                feedback_coverage = calculate_feedback_coverage(mentioned_files, files_changed)
                unexpected_edits = [f for f in files_changed if f not in mentioned_files]
                
                player_response_metrics = {
                    "edited_files": files_changed,
                    "edited_files_count": len(files_changed),
                    "feedback_coverage": round(feedback_coverage, 2),
                    "unexpected_edits": unexpected_edits,
                    "unexpected_edits_count": len(unexpected_edits)
                }
                
                # Log Player response to Coach feedback
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="player",
                    model=args.player_model,
                    action="respond_to_feedback",
                    result="success",
                    details=player_response_metrics
                )
                
                # WARNING: Low feedback coverage
                if feedback_coverage < 0.5:
                    warning_msg = (
                        f"⚠️  WARNING: Player addressed only {feedback_coverage*100:.0f}% "
                        f"of Coach's mentioned files ({len(files_changed)}/{len(mentioned_files)})"
                    )
                    log_print(warning_msg, verbose=True, quiet=args.quiet)
                    run_log.log_event(
                        turn_number=turn,
                        phase="loop",
                        agent="system",
                        model="monitor",
                        action="warning",
                        result="detected",
                        details={
                            "warning_type": "low_feedback_coverage",
                            "coverage": round(feedback_coverage, 2),
                            "mentioned_count": len(mentioned_files),
                            "edited_count": len(files_changed),
                            "severity": "medium"
                        }
                    )

            if file_write_errors:
                log_print(
                    f"[Player] File write errors: {len(file_write_errors)}",
                    verbose=args.verbose,
                    quiet=args.quiet,
                )
                feedback = (
                    "CRITICAL ERROR: The orchestrator failed to write one or more files. "
                    "This is an environment/permissions issue, not a code issue.\n\n"
                    "WRITE ERRORS:\n" + "\n".join(f"- {e}" for e in file_write_errors)
                )
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="system",
                    model="orchestrator",
                    action="write_files",
                    result="error",
                    error="\n".join(file_write_errors)[:1000],
                )
                # Reset fast-fail state to prevent stale context on next iteration
                last_skip_reason = ""
                last_fast_fail_outputs = ""
                last_fast_fail_errors = []
                fast_fail_retries_this_turn = 0
                # Skip Coach review: cannot proceed without successful writes.
                skipped_without_coach += 1
                if skipped_without_coach > max_skips_without_coach:
                    error_msg = (
                        "Aborting: too many iterations without a Coach review due to persistent write failures."
                    )
                    log_print(error_msg, verbose=True, quiet=args.quiet)
                    run_log.report(status="failed", message=error_msg)
                    break
                continue
            
            # Auto-Fix (if enabled)
            if args.auto_fix and files_changed:
                fix_cmds = detect_auto_fix_commands(".")
                if fix_cmds:
                    log_print(f"[Auto-Fix] Running {len(fix_cmds)} fixers...", verbose=args.verbose, quiet=args.quiet)
                    for cmd in fix_cmds:
                        out, code = run_command(cmd, args.command_shell)
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

            # Check for "Lazy Player" or zero-edit patterns
            made_changes = bool(files_changed or file_ops_applied)
            lazy_turn = (not made_changes and not player_data.get("commands_to_run"))
            zero_edit_with_verification = (not made_changes and player_data.get("commands_to_run"))
            
            # Track zero-edit streak
            if not made_changes:
                zero_edits_streak += 1
            else:
                zero_edits_streak = 0  # Reset on successful edit
            
            # Detect stuck loops: 3+ consecutive zero-edit attempts
            if zero_edits_streak >= 3:
                error_msg = (
                    f"Aborting: Player has made 0 edits for {zero_edits_streak} consecutive attempts. "
                    "This indicates the Player is stuck and unable to make progress. "
                    "Possible causes: guardrail blocking necessary changes, unclear feedback, or context overload."
                )
                log_print(error_msg, verbose=True, quiet=args.quiet)
                run_log.report(status="failed", message=error_msg)
                break
            
            # Inject escalating feedback after 2 consecutive zero-edit attempts
            if zero_edits_streak == 2:
                feedback = (
                    "CRITICAL: You have made 0 edits for 2 consecutive attempts. You are stuck. "
                    "You MUST make code changes to address the feedback. "
                    "If a guardrail is blocking you, find an alternative approach. "
                    "If you cannot make progress, explain WHY in detail."
                )
                log_print("[Warning] Zero-edit streak detected, injecting escalation feedback.", verbose=args.verbose, quiet=args.quiet)
                # Reset fast-fail state and continue to let Player try again with stronger feedback
                last_skip_reason = ""
                last_fast_fail_outputs = ""
                last_fast_fail_errors = []
                fast_fail_retries_this_turn = 0
                skipped_without_coach += 1
                if skipped_without_coach > max_skips_without_coach:
                    error_msg = "Aborting: too many iterations without a Coach review."
                    log_print(error_msg, verbose=True, quiet=args.quiet)
                    run_log.report(status="failed", message=error_msg)
                    break
                continue
            
            if lazy_turn:
                log_print("[Player] No actions taken (lazy turn).", verbose=args.verbose, quiet=args.quiet)
                # Do not burn a whole turn. If we can auto-verify, do it anyway and let Fast-Fail/Coach decide.
                if not auto_verify and not args.verify_cmd:
                    feedback = (
                        "CRITICAL ERROR: You did not perform any actions (no files edited, no file_ops, no commands). "
                        "You MUST modify the codebase to address the feedback. "
                        "If you think no changes are needed, you must explain why in 'thought_process' and run a verification command."
                    )
                    # Reset fast-fail state to prevent leakage to next iteration
                    last_skip_reason = ""
                    last_fast_fail_outputs = ""
                    last_fast_fail_errors = []
                    fast_fail_retries_this_turn = 0
                    # Skip Coach review to save tokens/time since nothing changed
                    skipped_without_coach += 1
                    if skipped_without_coach > max_skips_without_coach:
                        error_msg = (
                            "Aborting: too many iterations without a Coach review due to repeated no-op Player turns."
                        )
                        log_print(error_msg, verbose=True, quiet=args.quiet)
                        run_log.report(status="failed", message=error_msg)
                        break
                    continue
            
            # Warn about wasteful zero-edit + verification pattern
            if zero_edit_with_verification:
                log_print(
                    "[Warning] Player made 0 edits but ran verification commands - this is often wasteful.",
                    verbose=args.verbose, quiet=args.quiet
                )
            
            # Run Commands
            command_outputs = ""
            executed_commands = []
            verification_errors = []
            
            player_commands = list(player_data.get("commands_to_run", []))
            for cmd in player_commands:
                output, _code = run_command(cmd, args.command_shell)
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

            verification_start = time.time()
            for cmd in verify_commands:
                cmd_start = time.time()
                output, _code = run_command(cmd, args.command_shell)
                cmd_duration = time.time() - cmd_start
                
                executed_commands.append(cmd)
                # Truncate output to save tokens
                trunc_out = truncate_output(output)
                command_outputs += f"Command: {cmd}\nExit Code: {_code}\nOutput:\n{trunc_out}\n\n"
                if _code != 0:
                    verification_errors.append(f"Command '{cmd}' failed with exit code {_code}")
                
                # WARNING: Slow verification command
                if cmd_duration > 60:
                    log_print(
                        f"[WARNING] Slow verification: '{cmd}' took {cmd_duration:.1f}s",
                        verbose=True, quiet=args.quiet
                    )
                    run_log.log_event(
                        turn_number=turn,
                        phase="loop",
                        agent="system",
                        model="monitor",
                        action="warning",
                        result="detected",
                        details={
                            "warning_type": "slow_verification",
                            "command": cmd,
                            "duration_s": round(cmd_duration, 2),
                            "severity": "medium"
                        }
                    )
            
            total_verification_time = time.time() - verification_start

            if executed_commands:
                log_print(
                    f"[Player] Executed {len(executed_commands)} commands.",
                    verbose=args.verbose,
                    quiet=args.quiet,
                )
            
            # Track error persistence across turns
            if verification_errors:
                current_error_fingerprints = extract_error_fingerprints(command_outputs)
                
                # Compare with previous turn (if exists)
                persisting = set()
                resolved = set()
                new_errors = current_error_fingerprints
                
                if previous_error_fingerprints:
                    persisting = current_error_fingerprints & previous_error_fingerprints
                    resolved = previous_error_fingerprints - current_error_fingerprints
                    new_errors = current_error_fingerprints - previous_error_fingerprints
                
                persistence_rate = (
                    len(persisting) / len(previous_error_fingerprints) 
                    if previous_error_fingerprints else 0.0
                )
                
                error_persistence_metrics = {
                    "current_errors": sorted(list(current_error_fingerprints))[:10],  # Limit to 10 for log size
                    "current_error_count": len(current_error_fingerprints),
                    "persisting_errors": sorted(list(persisting))[:5],  # Show top 5
                    "persisting_count": len(persisting),
                    "resolved_errors": sorted(list(resolved))[:5],
                    "resolved_count": len(resolved),
                    "new_errors": sorted(list(new_errors))[:5],
                    "new_error_count": len(new_errors),
                    "persistence_rate": round(persistence_rate, 2)
                }
                
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="system",
                    model="monitor",
                    action="track_error_persistence",
                    result="success",
                    details=error_persistence_metrics
                )
                
                # WARNING: High error persistence (same errors stuck across turns)
                if turn > 2 and persistence_rate > 0.7 and len(persisting) > 0:
                    warning_msg = (
                        f"⚠️  WARNING: {len(persisting)} errors persisting at {persistence_rate*100:.0f}% rate. "
                        f"Stuck on: {sorted(list(persisting))[:2]}"
                    )
                    log_print(warning_msg, verbose=True, quiet=args.quiet)
                    run_log.log_event(
                        turn_number=turn,
                        phase="loop",
                        agent="system",
                        model="monitor",
                        action="warning",
                        result="detected",
                        details={
                            "warning_type": "high_error_persistence",
                            "persistence_rate": round(persistence_rate, 2),
                            "persisting_count": len(persisting),
                            "persisting_errors": sorted(list(persisting))[:3],
                            "severity": "high"
                        }
                    )
                
                # Store for next turn comparison
                previous_error_fingerprints = current_error_fingerprints
            else:
                # No errors: clear previous fingerprints
                previous_error_fingerprints = set()

            # Log Player action with comprehensive metrics
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
                    "verification_time_s": round(total_verification_time, 2),
                    # Loop health metrics
                    "zero_edits_streak": zero_edits_streak,
                    "fast_fail_retries_this_turn": fast_fail_retries_this_turn,
                    "skipped_without_coach": skipped_without_coach,
                    "made_changes": made_changes,
                    # Player behavior analysis
                    "attempted_files": len(player_data.get("files", {})),
                    "attempted_file_ops": len(player_data.get("file_ops", [])),
                    "attempted_commands": len(player_data.get("commands_to_run", [])),
                    "has_thought_process": bool(player_data.get("thought_process")),
                    # Action success rate
                    "file_success_rate": round(len(files_changed) / max(1, len(player_data.get("files", {}))) if player_data.get("files") else 1.0, 2),
                }
            )

            # Check for Fast Fail (Verification Failure)
            if args.fast_fail and verification_errors:
                fast_fail_retries_this_turn += 1
                bypass_fast_fail = fast_fail_retries_this_turn > int(getattr(args, "max_fast_fail_retries", 2))
                
                # WARNING: Fast-fail spiral detection
                if fast_fail_retries_this_turn >= 3:
                    log_print(
                        f"[WARNING] Fast-fail spiral detected: {fast_fail_retries_this_turn} retries in this turn",
                        verbose=True, quiet=args.quiet
                    )
                    run_log.log_event(
                        turn_number=turn,
                        phase="loop",
                        agent="system",
                        model="monitor",
                        action="warning",
                        result="detected",
                        details={
                            "warning_type": "fast_fail_spiral",
                            "retry_count": fast_fail_retries_this_turn,
                            "severity": "high"
                        }
                    )

                if bypass_fast_fail:
                    log_print(
                        f"[Fast Fail] Verification failed; retry cap reached ({fast_fail_retries_this_turn}). Forcing Coach.",
                        verbose=args.verbose,
                        quiet=args.quiet,
                    )
                    run_log.log_event(
                        turn_number=turn,
                        phase="loop",
                        agent="system",
                        model="orchestrator",
                        action="fast_fail_cap",
                        result="success",
                        details={"retries": fast_fail_retries_this_turn, "errors": verification_errors},
                    )
                    # Keep feedback, but proceed to Coach instead of skipping.
                    feedback = (
                        "VERIFICATION FAILED (Coach forced due to retry cap).\n" +
                        "Errors:\n" + "\n".join(f"- {e}" for e in verification_errors)
                    )
                else:
                    log_print(
                        f"[Fast Fail] Verification failed ({len(verification_errors)} errors). Skipping Coach.",
                        verbose=args.verbose,
                        quiet=args.quiet,
                    )
                feedback = (
                    "AUTOMATIC REJECTION (Fast Fail):\n"
                    "The following verification commands failed:\n" + 
                    "\n".join(f"- {e}" for e in verification_errors) + 
                    "\n\nCOMMAND OUTPUT SUMMARY:\n" + (summarize_command_outputs(command_outputs) or "(none)") +
                    "\n\nCOMMAND OUTPUT (TRUNCATED):\n" + truncate_output(command_outputs, max_chars=2500) + 
                    "\n\nYou must fix these errors before the Coach will review your code."
                )
                
                # Log the fast-fail as a system event (Coach was not called).
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="system",
                    model="orchestrator",
                    action="fast_fail",
                    result="success",
                    details={
                        "decision": "rejected",
                        "reason": "verification_failed",
                        "errors": verification_errors
                    }
                )
                last_skip_reason = "fast-fail"
                # Truncate command outputs to prevent context bloat on repeated fast-fail retries
                # Keep only the most recent failure details; earlier attempts are already in the conversation
                last_fast_fail_outputs = truncate_output(command_outputs, max_chars=8000)
                last_fast_fail_errors = list(verification_errors)
                if not bypass_fast_fail:
                    skipped_without_coach += 1
                    if skipped_without_coach > max_skips_without_coach:
                        error_msg = (
                            "Aborting: too many iterations without a Coach review due to repeated fast-fail verification."
                        )
                        log_print(error_msg, verbose=True, quiet=args.quiet)
                        run_log.report(status="failed", message=error_msg)
                        break
                    continue

            # --- Coach Turn ---
            log_print(f"[Coach] ({args.coach_model}) Reviewing...", verbose=args.verbose, quiet=args.quiet)
            last_skip_reason = ""
            last_fast_fail_outputs = ""
            last_fast_fail_errors = []
            fast_fail_retries_this_turn = 0
            
            # Include a names-only repo file list ONLY when Coach is focusing on recent edits.
            # This prevents token bloat when the Coach already receives a broad codebase snapshot.
            repo_file_tree = ""
            if args.coach_focus_recent:
                repo_file_tree = get_repo_file_tree(".", exclude_dirs, include_exts=include_exts)
            
            coach_input = (
                f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{spec_for_prompt}\n\n"
                f"SPEC PROGRESS (from orchestrator):\n"
                f"- mode: {spec_prog.get('mode')}\n"
                f"- complete: {spec_prog.get('complete')}\n"
                f"- total_items: {spec_prog.get('total_items')}\n"
                f"- remaining_items: {len(spec_prog.get('remaining_items') or [])}\n\n"
                "COACH APPROVAL RULE (MANDATORY):\n"
                "- Only set status=APPROVED if SPECIFICATION.md is explicitly marked complete (all checklist items checked or Status: COMPLETE).\n"
                "- If work is complete but not marked, require updating SPECIFICATION.md (do NOT approve).\n\n"
                f"ORCHESTRATOR SUMMARY:\n"
                f"- edits_applied: {len(files_changed)}\n"
                f"- edited_files: {json.dumps(files_changed, ensure_ascii=False)}\n"
                f"- file_write_errors: {len(file_write_errors) if 'file_write_errors' in locals() else 0}\n\n"
                f"PLAYER OUTPUT:\n{json.dumps(player_data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"COMMAND OUTPUT SUMMARY:\n{summarize_command_outputs(command_outputs) or '(none)'}\n\n"
                f"COMMAND OUTPUTS (TRUNCATED):\n{truncate_output(command_outputs, max_chars=3000) or ''}\n\n"
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
                feedback = "Coach produced no response. Proceeding with caution."
                skipped_without_coach = 0
                if turn == max_turns:
                    log_print("Max turns reached.", verbose=args.verbose, quiet=args.quiet)
                    run_log.report(
                        status="partial",
                        message=f"Max turns ({max_turns}) reached without full approval."
                    )
                    break
                turn += 1
                continue

            coach_data = extract_json(
                coach_response, run_log=run_log, turn_number=turn, agent="coach"
            )
            
            if not coach_data:
                log_print(f"[Coach] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)
                # Attempt a single in-turn repair to avoid wasting a Coach call.
                repair_input = (
                    "Your previous response was NOT valid JSON.\n"
                    "Return ONLY one fenced ```json code block with a single JSON object matching the required schema.\n"
                    "No prose, no markdown outside the code fence, no commentary.\n\n"
                    "PREVIOUS RESPONSE (for repair):\n"
                    + truncate_output(coach_response, max_chars=4000)
                )
                repair_response = get_llm_response(
                    coach_prompt,
                    repair_input,
                    model=args.coach_model,
                    run_log=run_log,
                    turn_number=turn,
                    agent="coach",
                )
                if repair_response:
                    coach_data = extract_json(
                        repair_response, run_log=run_log, turn_number=turn, agent="coach"
                    )

                if not coach_data:
                    feedback = "Coach failed to review. Proceeding with caution."
                skipped_without_coach = 0
                if turn == max_turns:
                    log_print("Max turns reached.", verbose=args.verbose, quiet=args.quiet)
                    run_log.report(
                        status="partial",
                        message=f"Max turns ({max_turns}) reached without full approval."
                    )
                    break
                turn += 1
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
            
            # Parse Coach feedback for inter-agent communication metrics
            mentioned_files = extract_file_mentions(coach_feedback)  # Update for next turn's Player tracking
            action_items = len(re.findall(r'^\d+\.', coach_feedback, re.MULTILINE))
            
            feedback_metrics = {
                "mentioned_files": mentioned_files,
                "mentioned_files_count": len(mentioned_files),
                "action_items_count": action_items,
                "feedback_length_chars": len(coach_feedback),
                "feedback_type": coach_status
            }

            # Log Coach decision with full feedback and inter-agent metrics
            coach_decision = "approved" if coach_status == "APPROVED" else (
                "replan" if coach_status == "REPLAN_NEEDED" else "rejected"
            )
            run_log.log_event(
                turn_number=turn,
                phase="loop",
                agent="coach",
                model=args.coach_model,
                action="review",
                result="success",
                details={
                    "decision": coach_decision,
                    "status": coach_status,
                    "reason_length": len(coach_feedback),
                    "feedback_text": coach_feedback,
                    "feedback_metrics": feedback_metrics
                }
            )
            
            if coach_status == "APPROVED":
                # Success is ONLY allowed when the specification is explicitly marked complete.
                spec_now = load_file(spec_file)
                spec_now_prog = _spec_progress(spec_now)
                if not spec_now_prog.get("complete"):
                    remaining = len(spec_now_prog.get("remaining_items") or [])
                    reason = (
                        f"Spec incomplete (mode={spec_now_prog.get('mode')}, remaining_items={remaining}). "
                        "Do not declare success until SPECIFICATION.md is marked complete."
                    )
                    log_print(
                        "[INCOMPLETE] Coach approved, but SPECIFICATION.md is not marked complete. " + reason,
                        verbose=True,
                        quiet=args.quiet,
                    )

                    # Force continued work (or fail if out of turns) with explicit instruction to mark spec.
                    feedback = (
                        "BLOCKER: You may not finish the run yet because SPECIFICATION.md is not marked complete. "
                        "Update SPECIFICATION.md by checking off completed items (- [x]) or adding 'Status: COMPLETE'. "
                        "If the work is complete, your task is to mark it complete in the spec so we stop wasting tokens."
                    )
                    if turn == max_turns:
                        run_log.report(status="failed", message=reason)
                        break
                    turn += 1
                    continue

                # Spec is complete; now we may declare success.
                if not made_changes:
                    log_print(
                        "SUCCESS! Coach approved and SPECIFICATION is marked complete (no edits applied this turn).",
                        verbose=args.verbose,
                        quiet=args.quiet,
                    )
                    run_log.report(status="success", message="Coach approved; specification marked complete.")
                else:
                    log_print(
                        "SUCCESS! Coach approved the implementation and SPECIFICATION is marked complete.",
                        verbose=args.verbose,
                        quiet=args.quiet,
                    )
                    run_log.report(status="success", message="Coach approved implementation; specification marked complete.")
                break
            
            # Handle replan request from Coach
            if coach_status == "REPLAN_NEEDED":
                log_print(
                    "[Replan] Coach detected fundamental design flaw. Re-invoking Architect...",
                    verbose=args.verbose,
                    quiet=args.quiet
                )
                
                # Log replan event
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="system",
                    model="orchestrator",
                    action="replan_triggered",
                    result="success",
                    details={
                        "reason": "Coach requested replan",
                        "feedback_preview": coach_feedback[:200] if coach_feedback else ""
                    }
                )
                
                # Re-invoke Architect with feedback
                revised_spec = run_architect_phase(
                    requirements,
                    current_files,
                    requirements_file,
                    spec_file,
                    architect_model=args.architect_model,
                    run_log=run_log,
                    verbose=args.verbose,
                    quiet=args.quiet,
                    feedback=coach_feedback
                )
                
                if not revised_spec:
                    error_msg = "Architect failed to generate revised specification during replan."
                    log_print(f"[Replan] {error_msg}", verbose=args.verbose, quiet=args.quiet)
                    run_log.report(status="failed", message=error_msg)
                    break
                
                log_print(
                    f"[Replan] Architect updated {spec_file}. Continuing loop with revised specification.",
                    verbose=args.verbose,
                    quiet=args.quiet
                )
                
                # Log successful replan
                run_log.log_event(
                    turn_number=turn,
                    phase="loop",
                    agent="architect",
                    model=args.architect_model,
                    action="replan_completed",
                    result="success",
                    details={"output_file": spec_file}
                )
                
                # Set feedback for next Player turn explaining the replan
                feedback = (
                    f"SPECIFICATION REVISED BY ARCHITECT (Replan):\n\n"
                    f"The Coach identified a fundamental design issue:\n{coach_feedback}\n\n"
                    f"The Architect has updated {spec_file} to address this. "
                    f"Review the revised specification and implement the corrected approach. "
                    f"Focus on the changes in the REVISION HISTORY section if present."
                )
                
                # Continue to next turn WITHOUT incrementing turn counter
                # Replan does not consume a turn since Coach already reviewed
                skipped_without_coach = 0
                continue
                
            feedback = coach_feedback

            skipped_without_coach = 0

            if turn == max_turns:
                log_print("Max turns reached.", verbose=args.verbose, quiet=args.quiet)
                run_log.report(
                    status="partial",
                    message=f"Max turns ({max_turns}) reached without full approval."
                )
                break

            # Log context cache stats for observability
            cache_stats = context_cache.get_cache_stats()
            if cache_stats["cache_hits"] > 0:
                log_print(
                    f"[Cache] Hit rate: {cache_stats['hit_rate']:.1%} "
                    f"({cache_stats['cache_hits']} hits, {cache_stats['cache_misses']} misses)",
                    verbose=True,
                    quiet=args.quiet
                )

            turn += 1

    except KeyboardInterrupt:
        log_print("Loop interrupted by user.", verbose=args.verbose, quiet=False)
        run_log.report(status="interrupted", message="User interrupted the loop.")
    except Exception as e:
        log_print(f"Unexpected error: {e}", verbose=args.verbose, quiet=False)
        run_log.report(status="error", message=str(e))
    finally:
        # Log final context cache performance
        if 'context_cache' in locals():
            cache_stats = context_cache.get_cache_stats()
            log_print(
                f"Context Cache Performance: {cache_stats['hit_rate']:.1%} hit rate "
                f"({cache_stats['cache_hits']} hits / {cache_stats['cache_hits'] + cache_stats['cache_misses']} requests), "
                f"{cache_stats['unique_contents']} unique contents cached",
                verbose=True,
                quiet=args.quiet
            )
        
        # Final flush (already done incrementally, but ensure it's written)
        run_log._flush_log_to_file()
        log_path = str(run_log.tailable_log_path())
        log_print(f"Observability log saved: {log_path}", verbose=args.verbose, quiet=args.quiet)

if __name__ == "__main__":
    main()
