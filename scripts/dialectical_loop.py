import os
import json
import subprocess
import sys
import time
import argparse
import tempfile
from pathlib import Path
from datetime import datetime, timezone

# Configuration
MAX_TURNS = 10
REQUIREMENTS_FILE = "REQUIREMENTS.md"
SPECIFICATION_FILE = "SPECIFICATION.md"
DEFAULT_COACH_MODEL = "claude-sonnet-4.5"
DEFAULT_PLAYER_MODEL = "gemini-3-pro-preview"
DEFAULT_ARCHITECT_MODEL = "claude-sonnet-4.5"

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
            "total_turns_executed": len(set(t.get("turn_number") for t in self.turns if t.get("phase") == "loop")),
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
            print(f"Coach: {coach_approved} approved, {coach_rejected} rejected, {coach_errors} errors", file=sys.stderr)
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
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return f"Command: {command}\nExit Code: {result.returncode}\nOutput:\n{result.stdout}\nError:\n{result.stderr}"
    except Exception as e:
        return f"Error running command {command}: {e}"

def get_github_token():
    try:
        token = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
        return token
    except Exception as e:
        print(f"Error getting GitHub token: {e}")
        return None

def get_llm_response(system_prompt, user_prompt, model="claude-sonnet-4.5", run_log=None, turn_number=0, agent="unknown"):
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
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, shell=False)
        except FileNotFoundError:
            cmd_str = subprocess.list2cmdline(cmd)
            result = subprocess.run(cmd_str, env=env, capture_output=True, text=True, shell=True)
        
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

    # Prefer fenced JSON blocks first
    for fence in ["```json", "```"]:
        start = text.find(fence)
        while start != -1:
            block_start = start + len(fence)
            end = text.find("```", block_start)
            if end == -1:
                break
            add_candidate(text[block_start:end])
            start = text.find(fence, end + 3)

    # Add full text as a fallback
    add_candidate(text)

    # Add the first detected JSON object slice
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        add_candidate(text[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            return json.loads(candidate)
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
                "response_preview": text[:200] if len(text) > 200 else text,
                "response_length": len(text),
                "contains_brace": "{" in text,
                "contains_bracket": "[" in text,
            }
        )
    return None

def run_architect_phase(requirements, current_files, requirements_file, spec_file, architect_model, run_log=None, verbose=False, quiet=False):
    log_print(f"Architect is analyzing requirements...", verbose=verbose, quiet=quiet)
    architect_prompt = load_file(str(AGENT_DIR / "architect.md"))
    if not architect_prompt.strip():
        print(f"Error: Missing architect prompt at {AGENT_DIR / 'architect.md'}")
        return None
    
    architect_input = f"REQUIREMENTS FILE: {requirements_file}\nREQUIREMENTS:\n{requirements}\n\nCURRENT CODEBASE:\n{current_files}\n\n"
    architect_input += f"TASK: Create a detailed technical specification ({spec_file}) for the implementation. "
    architect_input += "Include file paths, data structures, function signatures, and step-by-step implementation plan. "
    architect_input += "Output ONLY the markdown content of the specification file. Do not wrap it in JSON."

    response = get_llm_response(architect_prompt, architect_input, model=architect_model, run_log=run_log, turn_number=0, agent="architect")
    
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

def main():
    parser = argparse.ArgumentParser(description="Run the Dialectical Autocoding Loop with built-in observability.")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS, help="Maximum number of turns to run.")
    parser.add_argument("--requirements-file", default=REQUIREMENTS_FILE, help="Path to requirements markdown file.")
    parser.add_argument("--spec-file", default=SPECIFICATION_FILE, help="Path to specification markdown file.")
    parser.add_argument("--skip-architect", action="store_true", help="Skip architect phase even if specification is missing.")
    parser.add_argument("--coach-model", default=DEFAULT_COACH_MODEL, help="Model to use for Coach reviews.")
    parser.add_argument("--player-model", default=DEFAULT_PLAYER_MODEL, help="Model to use for Player implementation.")
    parser.add_argument("--architect-model", default=DEFAULT_ARCHITECT_MODEL, help="Model to use for Architect planning.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output (details on prompts, responses, state).")
    parser.add_argument("--quiet", action="store_true", help="Suppress all terminal output except final summary and log path.")
    args = parser.parse_args()
    
    max_turns = args.max_turns
    if max_turns < 1:
        print("Error: --max-turns must be >= 1")
        return

    # Initialize observability
    run_log = RunLog(verbose=args.verbose, quiet=args.quiet)
    log_print(f"Starting Dialectical Autocoding Loop (max_turns={max_turns}, verbose={args.verbose}, quiet={args.quiet})", 
              verbose=args.verbose, quiet=args.quiet)

    requirements_file = args.requirements_file
    spec_file = args.spec_file

    try:
        requirements = load_file(requirements_file)
        specification = load_file(spec_file)

        if not requirements.strip() and not specification.strip():
            error_msg = f"Error: Neither {requirements_file} nor {spec_file} found. Create one of them to proceed."
            print(error_msg)
            log_print(error_msg, verbose=args.verbose, quiet=args.quiet)
            run_log.report(status="failed", message=error_msg)
            log_path = run_log.write_log_file()
            log_print(f"Observability log: {log_path}", verbose=args.verbose, quiet=args.quiet)
            return

        # Gather context
        current_files = ""
        for root, _, files in os.walk("."):
            for file in files:
                if file.endswith(".py") and "venv" not in root:
                    path = os.path.join(root, file)
                    content = load_file(path)
                    current_files += f"\n--- {path} ---\n{content}\n"

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
                error_msg = f"Error: {spec_file} is missing and requirements are empty; cannot generate specification."
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
        
        for turn in range(1, max_turns + 1):
            log_print(f"Turn {turn}/{max_turns}", verbose=args.verbose, quiet=args.quiet)
            
            # --- Player Turn ---
            log_print(f"[Player] Implementing...", verbose=args.verbose, quiet=args.quiet)
            player_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\nFEEDBACK FROM PREVIOUS TURN:\n{feedback}"
            
            # Add current file context
            current_files = ""
            for root, _, files in os.walk("."):
                for file in files:
                    if file.endswith(".py") and "venv" not in root:
                        path = os.path.join(root, file)
                        content = load_file(path)
                        current_files += f"\n--- {path} ---\n{content}\n"
            
            if current_files:
                player_input += f"\n\nCURRENT CODEBASE:\n{current_files}"

            # Player
            player_response = get_llm_response(player_prompt, player_input, model=args.player_model, 
                                               run_log=run_log, turn_number=turn, agent="player")
            
            if not player_response:
                log_print(f"[Player] No response.", verbose=args.verbose, quiet=args.quiet)
                continue

            player_data = extract_json(player_response, run_log=run_log, turn_number=turn, agent="player")
            
            if not player_data:
                log_print(f"[Player] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)
                feedback = "Your last response was not valid JSON. Response must be a valid JSON object. Please follow the format strictly and wrap output in {...} braces."
                continue
            
            if args.verbose:
                log_print(f"[Player] Thought: {player_data.get('thought_process', 'N/A')[:100]}...", 
                         verbose=True, quiet=args.quiet)
            
            # Apply Edits
            files_changed = []
            if "files" in player_data:
                for path, content in player_data["files"].items():
                    save_file(path, content)
                    files_changed.append(path)
                log_print(f"[Player] Applied {len(files_changed)} edits.", verbose=args.verbose, quiet=args.quiet)
            
            # Run Commands
            command_outputs = ""
            if "commands_to_run" in player_data:
                for cmd in player_data["commands_to_run"]:
                    output = run_command(cmd)
                    command_outputs += output + "\n"
                log_print(f"[Player] Executed {len(player_data['commands_to_run'])} commands.", 
                         verbose=args.verbose, quiet=args.quiet)

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
                    "commands_executed": len(player_data.get("commands_to_run", [])),
                }
            )

            # --- Coach Turn ---
            log_print(f"[Coach] Reviewing...", verbose=args.verbose, quiet=args.quiet)
            coach_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\nPLAYER OUTPUT:\n{json.dumps(player_data, indent=2)}\n\nCOMMAND OUTPUTS:\n{command_outputs}"
            
            # Add file context for Coach
            current_files_new = ""
            for root, _, files in os.walk("."):
                for file in files:
                    if file.endswith(".py") and "venv" not in root:
                        path = os.path.join(root, file)
                        content = load_file(path)
                        current_files_new += f"\n--- {path} ---\n{content}\n"
            coach_input += f"\n\nUPDATED CODEBASE:\n{current_files_new}"

            # Coach
            coach_response = get_llm_response(coach_prompt, coach_input, model=args.coach_model,
                                              run_log=run_log, turn_number=turn, agent="coach")
            
            if not coach_response:
                log_print(f"[Coach] No response.", verbose=args.verbose, quiet=args.quiet)
                continue

            coach_data = extract_json(coach_response, run_log=run_log, turn_number=turn, agent="coach")
            
            if not coach_data:
                log_print(f"[Coach] Invalid JSON output.", verbose=args.verbose, quiet=args.quiet)
                feedback = "Coach failed to review. Proceeding with caution."
                continue
            
            coach_status = coach_data.get("status", "UNKNOWN")
            coach_feedback = coach_data.get("feedback", "")
            log_print(f"[Coach] Status: {coach_status}", verbose=args.verbose, quiet=args.quiet)
            if args.verbose:
                # Log first 200 chars of feedback in verbose mode
                fb_preview = coach_feedback[:200] + "..." if len(coach_feedback) > 200 else coach_feedback
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
                log_print("SUCCESS! Coach approved the implementation.", verbose=args.verbose, quiet=args.quiet)
                run_log.report(status="success", message="Coach approved implementation.")
                break
                
            feedback = coach_feedback
            
            if turn == max_turns:
                log_print("Max turns reached.", verbose=args.verbose, quiet=args.quiet)
                run_log.report(status="partial", message=f"Max turns ({max_turns}) reached without full approval.")

    except KeyboardInterrupt:
        log_print("Loop interrupted by user.", verbose=args.verbose, quiet=False)
        run_log.report(status="interrupted", message="User interrupted the loop.")
    except Exception as e:
        log_print(f"Unexpected error: {e}", verbose=args.verbose, quiet=False)
        run_log.report(status="error", message=str(e))
    finally:
        # Always write log file
        log_path = run_log.write_log_file()
        log_print(f"Observability log saved: {log_path}", verbose=args.verbose, quiet=args.quiet)

if __name__ == "__main__":
    main()
