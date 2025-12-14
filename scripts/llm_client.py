"""
LLM client utilities for calling GitHub Copilot CLI.

This module provides:
- get_llm_response: Call Copilot with system/user prompts and observability
- extract_json: Parse JSON from LLM responses with resilient error handling
- Helper functions for token management and response parsing
"""

import os
import sys
import time
import json
import re
import subprocess
import tempfile
from pathlib import Path

SUBPROCESS_TEXT_ENCODING = "utf-8"


def get_github_token():
    """Retrieve GitHub token from gh CLI."""
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
            # Calculate context metrics
            prompt_size_chars = len(input_text)
            prompt_size_kb = prompt_size_chars / 1024
            
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
                details={
                    "prompt_size_chars": prompt_size_chars,
                    "prompt_size_kb": round(prompt_size_kb, 2),
                    "token_efficiency": round(output_tokens_est / max(1, input_tokens_est), 3) if input_tokens_est > 0 else 0,
                }
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
    """Remove markdown code fence markers from text."""
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
    """
    Extract and parse JSON from LLM response text.
    
    Attempts multiple strategies:
    1. Fenced JSON blocks (```json...```)
    2. First {...} object found
    3. Fixing common issues (trailing commas, JS comments)
    
    Returns parsed JSON dict or None if parsing fails.
    """
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
    
    # Detect if response appears truncated (ends mid-sentence or mid-word)
    is_truncated = False
    if text:
        last_chars = text[-50:].strip()
        # Check for incomplete JSON structures or mid-sentence cutoffs
        if last_chars and not last_chars.endswith(("}", "]", '"', ".", "!", "?", ")", ";", ",")):
            is_truncated = True
        # Check if last brace/bracket is unclosed
        open_count = text.count("{") - text.count("}")
        if open_count > 0:
            is_truncated = True
    
    if is_truncated:
        error_msg += " (response appears truncated - LLM may have hit token limit)"
    
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
                "appears_truncated": is_truncated,
            }
        )
    return None
