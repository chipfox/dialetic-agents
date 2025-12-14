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
- `--verbose` — enable detailed output (prompts, responses, state)
- `--quiet` — suppress all terminal output except final summary and log path

### Token-saving / context controls

- `--lean-mode`: **Recommended**. Activates all token-saving features (`--fast-fail`, `--coach-focus-recent`, `--auto-fix`, `--context-mode auto`).
- `--context-mode {auto,snapshot,git-changed}`
  - `auto` (default): full snapshot on turn 1, then only git-changed files
  - `snapshot`: snapshot up to limits every turn
  - `git-changed`: only include files reported by `git status --porcelain`
- `--context-max-bytes N`, `--context-max-file-bytes N`, `--context-max-files N`
- `--coach-focus-recent`: Restrict Coach context to only files edited in the current turn (saves tokens).
- `--fast-fail`: Skip Coach review if verification commands fail (saves tokens/time).
- `--auto-fix`: Automatically run `npm run lint -- --fix` (or similar) if available after Player edits.

### Dynamic Spec Pruning

The Player agent is instructed to automatically remove completed sections from `SPECIFICATION.md` (or mark them `[DONE]`) as it progresses. This keeps the context window small and prevents re-reading completed instructions.

### Verification controls

- `--verify-cmd "<command>"` (repeatable)
- `--no-auto-verify` (disables auto `npm run lint` + `npm run build` when `package.json` exists)
- **Automatic LSP**: The script auto-detects `npm run build`, `npm run typecheck`, or `tsc` to provide type errors.

## Recommended Copilot Models

The skill works with any Copilot CLI-available model. Based on cost-to-capability analysis:

### Tier 1: Recommended (Best Balance)

- **Architect**: `claude-sonnet-4.5` (128K context, 1x cost)
- **Player**: `gemini-3-pro-preview` (109K context, 1x cost) or `claude-sonnet-4.5` (more stable)
- **Coach**: `claude-sonnet-4.5` (128K context, 1x cost)

Cost per 5-turn loop: ~38 cost units. Stable, proven, good reasoning across all roles.

```bash
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 \
  --architect-model claude-sonnet-4.5 \
  --player-model gemini-3-pro-preview \
  --coach-model claude-sonnet-4.5
```

### Tier 2: Budget (Cost-Optimized)

- **Architect**: `gemini-3-pro-preview` (109K context, 1x cost)
- **Player**: `claude-haiku-4.5` (128K context, 0.33x cost) — fast & cheap, test on your codebase first
- **Coach**: `claude-sonnet-4.5` (need strong critic)

Cost per 5-turn loop: ~26 cost units (32% cheaper). Good for rapid iteration or simple code tasks.

```bash
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 \
  --architect-model gemini-3-pro-preview \
  --player-model claude-haiku-4.5 \
  --coach-model claude-sonnet-4.5
```

### Tier 3: Premium (Quality-Optimized)

- **Architect**: `claude-opus-4.5` (128K context, 3x cost) — absolute best reasoning
- **Player**: `claude-sonnet-4.5` (solid code gen)
- **Coach**: `claude-opus-4.5` (most credible critic)

Cost per 5-turn loop: ~79 cost units (2.1x Tier 1). Use for mission-critical or highly complex systems.

**Note**: Tier 3 uses Preview models; test before production.

### Avoid

- Claude Opus 4.1 (10x cost, worse than 4.5 Preview)
- Claude Haiku for Architect/Coach (too weak for planning/judgment)
- Models with unclear pricing ("0x" cost)

## Observability & Monitoring

The loop emits **structured observability** to help you monitor loop health and token usage:

- **Output control** (set via `--verbose` and `--quiet`):
  - Default: Real-time per-turn updates to stderr (one line per agent action) + final summary.
  - `--quiet`: Only final summary (turn count, success/fail, errors) printed to stderr.
  - `--verbose`: All the above + detailed snippets of prompts, responses, and intermediate state (best for debugging).

- **Observability log file**: A JSON file is automatically written to the project root with the name `dialectical-loop-TIMESTAMP.json`. This contains:
  - Per-turn events (agent, model, action, tokens used, duration, outcome).
  - Final summary (total tokens, agent call counts, approval/rejection counts).
  - Errors and warnings (if any).

**Token estimates**: The orchestrator uses a simple heuristic (1 token ≈ 4 chars) to estimate token usage per turn. This helps you avoid runaway loops that burn tokens on trivial edits.

### Example: quiet mode (minimal output)

```bash
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 --quiet
```

Outputs only the final summary + log file path to stderr.

### Example: verbose mode (detailed debugging)

```bash
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 --verbose
```

Outputs detailed per-turn logs + final summary + log file path to stderr.

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
