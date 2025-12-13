---
name: dialectical-loop
description: Run a bounded adversarial cooperation coding loop (Architect -> Player <-> Coach). Use when implementing features from REQUIREMENTS.md, generating SPECIFICATION.md plans, and iterating with strict review until approved.
---

# Dialectical Loop

Run a **bounded, adversarial coding workflow** rather than single-turn “vibe coding”.

## Core idea

- **Architect** turns high-level intent (`REQUIREMENTS.md`) into an actionable contract (`SPECIFICATION.md`).
- **Player** implements the specification and runs verification commands.
- **Coach** adversarially evaluates compliance and blocks until it’s correct.

This keeps attention bounded, forces explicit plans, and adds a strict review gate.

## Inputs / outputs

- Input: `REQUIREMENTS.md` (recommended)
- Generated/optional input: `SPECIFICATION.md`
- Output: code edits + command outputs per turn

## Agent prompts (no manual install)

This skill ships its role prompts inside the skill folder:

- `agents/architect.md`
- `agents/player.md`
- `agents/coach.md`

The orchestrator loads these files automatically at runtime. You do not need to “install agents” separately or configure them in OpenSkills.

## Model providers and authentication

By default, the orchestrator calls the **GitHub Copilot CLI** (`copilot`) and authenticates via GitHub:

- You must be signed in with `gh auth login` (or have `GITHUB_TOKEN` set).
- The `--*-model` flags are passed through to the Copilot CLI as model identifiers.

If you want to use other providers (OpenAI, Anthropic, Gemini, Azure OpenAI, AWS Bedrock, local models), the skill does not require them out-of-the-box, but you will need to adapt the orchestrator:

- Replace the backend in `scripts/dialectical_loop.py` (the `get_llm_response(...)` function) to call your provider.
- In that case, you will typically need provider-specific credentials (for example `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`/ADC, `AZURE_OPENAI_API_KEY` + endpoint, Bedrock IAM, etc.).
- Local runtimes (e.g., Ollama/LM Studio) may not require API keys, but do require a running local server.

## How to run

Follow this skill as an execution playbook.

Run the installed loop script from the project directory you want to modify:

- Windows:
  - `python %USERPROFILE%/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10`
- macOS/Linux:
  - `python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10`

## Script options

- `--max-turns N` (required to bound the loop)
- `--requirements-file PATH`
- `--spec-file PATH`
- `--skip-architect` (only if you already have a good spec)
- `--architect-model MODEL`
- `--player-model MODEL`
- `--coach-model MODEL`

## Examples

Generate a plan (if missing) and iterate up to 10 turns:

- `python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10`

Use custom files:

- `python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --requirements-file REQUIREMENTS.md --spec-file SPECIFICATION.md --max-turns 15`

Skip Architect (only when an explicit plan already exists):

- `python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --skip-architect --max-turns 10`

## Implementation snippet (portable launcher)

```python
import os
import sys
import subprocess
from pathlib import Path

skill_script = Path.home() / ".claude" / "skills" / "dialectical-loop" / "scripts" / "dialectical_loop.py"

args = ["--max-turns", "10"]

python_cmd = sys.executable
venv_python = Path.cwd() / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
if venv_python.exists():
    python_cmd = str(venv_python)

subprocess.run([python_cmd, str(skill_script)] + args, check=False)
```
