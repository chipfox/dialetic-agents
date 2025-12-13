import os
import json
import subprocess
import sys
import time
import argparse
import tempfile
from pathlib import Path

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

def get_llm_response(system_prompt, user_prompt, model="claude-sonnet-4.5"):
    token = os.environ.get("GITHUB_TOKEN") or get_github_token()
    if not token:
        print("Error: Could not find GITHUB_TOKEN. Please login with 'gh auth login'.")
        sys.exit(1)
    
    # Set token for subprocess
    env = os.environ.copy()
    env["GITHUB_TOKEN"] = token
    env["GH_TOKEN"] = token

    # Combine system and user prompt into a temp file.
    # Avoid writing into the user's project directory.
    full_prompt = f"{system_prompt}\n\n{user_prompt}"
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
    
    print(f"Calling Copilot ({model})...")
    try:
        # Prefer shell=False for predictable argv handling; fallback to shell=True if needed.
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, shell=False)
        except FileNotFoundError:
            cmd_str = subprocess.list2cmdline(cmd)
            result = subprocess.run(cmd_str, env=env, capture_output=True, text=True, shell=True)
        
        if result.returncode != 0:
            print(f"Copilot CLI Error ({result.returncode}):\n{result.stderr}")
            return None
            
        return result.stdout
    except Exception as e:
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

def extract_json(text):
    try:
        # Find JSON block
        start = text.find("```json")
        if start != -1:
            start += 7
            end = text.find("```", start)
            if end != -1:
                json_str = text[start:end].strip()
                return json.loads(json_str)
        # Try parsing raw text if no block
        return json.loads(text)
    except json.JSONDecodeError:
        print("Failed to parse JSON from response.")
        return None

def run_architect_phase(requirements, current_files, requirements_file, spec_file, architect_model):
    print("Architect is analyzing requirements...")
    architect_prompt = load_file(str(AGENT_DIR / "architect.md"))
    if not architect_prompt.strip():
        print(f"Error: Missing architect prompt at {AGENT_DIR / 'architect.md'}")
        return None
    
    architect_input = f"REQUIREMENTS FILE: {requirements_file}\nREQUIREMENTS:\n{requirements}\n\nCURRENT CODEBASE:\n{current_files}\n\n"
    architect_input += f"TASK: Create a detailed technical specification ({spec_file}) for the implementation. "
    architect_input += "Include file paths, data structures, function signatures, and step-by-step implementation plan. "
    architect_input += "Output ONLY the markdown content of the specification file. Do not wrap it in JSON."

    response = get_llm_response(architect_prompt, architect_input, model=architect_model)
    
    if response:
        response = strip_fenced_block(response)
        if not response.strip():
            print("Architect returned empty specification.")
            return None
        save_file(spec_file, response)
        print(f"Generated {spec_file}")
        return response
    else:
        print("Architect failed to generate specification.")
        return None

def main():
    parser = argparse.ArgumentParser(description="Run the Dialectical Autocoding Loop.")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS, help="Maximum number of turns to run.")
    parser.add_argument("--requirements-file", default=REQUIREMENTS_FILE, help="Path to requirements markdown file.")
    parser.add_argument("--spec-file", default=SPECIFICATION_FILE, help="Path to specification markdown file.")
    parser.add_argument("--skip-architect", action="store_true", help="Skip architect phase even if specification is missing.")
    parser.add_argument("--coach-model", default=DEFAULT_COACH_MODEL, help="Model to use for Coach reviews.")
    parser.add_argument("--player-model", default=DEFAULT_PLAYER_MODEL, help="Model to use for Player implementation.")
    parser.add_argument("--architect-model", default=DEFAULT_ARCHITECT_MODEL, help="Model to use for Architect planning.")
    args = parser.parse_args()
    
    max_turns = args.max_turns
    if max_turns < 1:
        print("Error: --max-turns must be >= 1")
        return

    print("Starting Dialectical Autocoding Loop...")

    requirements_file = args.requirements_file
    spec_file = args.spec_file

    requirements = load_file(requirements_file)
    specification = load_file(spec_file)

    if not requirements.strip() and not specification.strip():
        print(f"Error: Neither {requirements_file} nor {spec_file} found. Create one of them to proceed.")
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
            print(f"Error: {spec_file} is missing and --skip-architect was provided.")
            return
        if not requirements.strip():
            print(f"Error: {spec_file} is missing and requirements are empty; cannot generate specification.")
            return

        specification = run_architect_phase(
            requirements,
            current_files,
            requirements_file,
            spec_file,
            architect_model=args.architect_model,
        )
        if not specification:
            print("Aborting due to missing specification.")
            return
    else:
        print(f"Using existing {spec_file}")

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
        print(f"\n=== Turn {turn}/{max_turns} ===")
        
        # --- Player Turn ---
        print("Player is working...")
        player_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\nFEEDBACK FROM PREVIOUS TURN:\n{feedback}"
        
        # Add current file context (simplified: read all .py files in src/tests)
        # In a real system, we'd be smarter about this.
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
        player_response = get_llm_response(player_prompt, player_input, model=args.player_model)
        
        if not player_response:
             print("Player returned no response.")
             continue

        player_data = extract_json(player_response)
        
        if not player_data:
            print("Player failed to produce valid JSON. Retrying or skipping...")
            feedback = "Your last response was not valid JSON. Please follow the format strictly."
            continue
            
        print(f"Player Thought: {player_data.get('thought_process', 'No thought provided')}")
        
        # Apply Edits
        files_changed = []
        if "files" in player_data:
            for path, content in player_data["files"].items():
                print(f"Updating {path}...")
                save_file(path, content)
                files_changed.append(path)
        
        # Run Commands
        command_outputs = ""
        if "commands_to_run" in player_data:
            for cmd in player_data["commands_to_run"]:
                print(f"Running: {cmd}")
                output = run_command(cmd)
                command_outputs += output + "\n"
                print(output)

        # --- Coach Turn ---
        print("Coach is reviewing...")
        coach_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\nPLAYER OUTPUT:\n{json.dumps(player_data, indent=2)}\n\nCOMMAND OUTPUTS:\n{command_outputs}"
        
        # Add file context for Coach too
        coach_input += f"\n\nCURRENT CODEBASE:\n{current_files}" # Note: current_files is technically "old" before edits, but we just updated files.
        # Let's refresh context
        current_files_new = ""
        for root, _, files in os.walk("."):
            for file in files:
                if file.endswith(".py") and "venv" not in root:
                    path = os.path.join(root, file)
                    content = load_file(path)
                    current_files_new += f"\n--- {path} ---\n{content}\n"
        coach_input += f"\n\nUPDATED CODEBASE:\n{current_files_new}"

        # Coach
        coach_response = get_llm_response(coach_prompt, coach_input, model=args.coach_model)
        
        if not coach_response:
             print("Coach returned no response.")
             continue

        coach_data = extract_json(coach_response)
        
        if not coach_data:
            print("Coach failed to produce valid JSON.")
            feedback = "Coach failed to review. Proceeding with caution."
            continue
            
        print(f"Coach Status: {coach_data.get('status')}")
        print(f"Coach Feedback: {coach_data.get('feedback')}")
        
        if coach_data.get("status") == "APPROVED":
            print("\n=== SUCCESS! Coach approved the implementation. ===")
            break
            
        feedback = coach_data.get("feedback", "No feedback provided.")
        
        if turn == max_turns:
            print("\n=== Max turns reached. Process terminated. ===")

if __name__ == "__main__":
    main()
