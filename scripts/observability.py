"""
Observability utilities for the dialectical loop.

This module provides:
- RunLog: Event tracking for each run with token/turn metrics
- log_print: Timestamped logging to stderr with verbose/quiet support
"""

import sys
import json
from pathlib import Path
from datetime import datetime, timezone


def utc_now_iso():
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


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
                "replan": len([t for t in coach_calls if t.get("decision") == "replan"]),
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
        coach_replan = summary["coach_calls"]["replan"]
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
            coach_summary = f"Coach: {coach_approved} approved, {coach_rejected} rejected"
            if coach_replan > 0:
                coach_summary += f", {coach_replan} replan"
            coach_summary += f", {coach_errors} errors"
            print(coach_summary, file=sys.stderr)
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
