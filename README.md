# dialectical-loop (OpenSkills)

An **adversarial cooperation** coding loop (Architect → Player ↔ Coach) inspired by the *Dialectical Autocoding* workflow. This repo is structured as an installable OpenSkills skill.

## What you get

- **Architect** generates/refreshes a detailed `SPECIFICATION.md` (when missing)
- **Player** implements changes and runs commands/tests
- **Coach** reviews for strict compliance and rejects until complete
- **Bounded turns** via `--max-turns`

## Prerequisites

- Python 3.10+ (3.12 works)

### Default provider: GitHub Copilot CLI

This repo’s orchestrator is implemented against the GitHub Copilot CLI:

- GitHub CLI authenticated: `gh auth login` (or `GITHUB_TOKEN` set)
- GitHub Copilot CLI available as `copilot` (your environment must be able to run it)

### Agent prompts (built-in)

You do not install “agents” manually. The role prompts are included in this repo and are loaded automatically by the script:

- `agents/architect.md`
- `agents/player.md`
- `agents/coach.md`

## Install (OpenSkills)

From your own terminal after you publish this repo:

- `openskills install <your-github-url>`
- Confirm: `openskills list`
- Read into your agent: `openskills read dialectical-loop`

> Note: `openskills` primarily *prints* skill instructions for an agent to follow; it does not provide a universal `openskills run` command.

## Run the loop

In the project directory you want to modify:

1. Create `REQUIREMENTS.md` (or provide an existing `SPECIFICATION.md`).
2. Run the installed script:

- macOS/Linux example:
  - `python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10`
- Windows example:
  - `python %USERPROFILE%/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10`

### Useful flags

- `--requirements-file REQUIREMENTS.md`
- `--spec-file SPECIFICATION.md`
- `--skip-architect`
- `--quiet` (minimal output)
- `--verbose` (debug output)

### Token-saving flags

- `--context-mode {auto,snapshot,git-changed}`
  - `auto` (default): snapshot on turn 1, then only git-changed files
- `--context-max-bytes N`, `--context-max-file-bytes N`, `--context-max-files N`

### Verification flags

- `--verify-cmd "<command>"` (repeatable)
- `--no-auto-verify`

### Model selection (Architect, Player, Coach)

This skill works with any Copilot CLI model. Choose a tier based on your budget and quality needs:

#### Tier 1: Recommended (Balanced)

```bash
--architect-model claude-sonnet-4.5 \
--player-model gemini-3-pro-preview \
--coach-model claude-sonnet-4.5
```

Cost: ~38 units per 5-turn loop. Stable, proven, good reasoning.

#### Tier 2: Budget (Cost-optimized)

```bash
--architect-model gemini-3-pro-preview \
--player-model claude-haiku-4.5 \
--coach-model claude-sonnet-4.5
```

Cost: ~26 units (32% cheaper). Good for rapid iteration; test Haiku on your codebase first.

#### Tier 3: Premium (Quality-optimized)

```bash
--architect-model claude-opus-4.5 \
--player-model claude-sonnet-4.5 \
--coach-model claude-opus-4.5
```

Cost: ~79 units (2.1x Tier 1). Use for mission-critical systems; Opus 4.5 is Preview (test first).

#### Why these choices?

- **Architect** needs strong reasoning and large context (spec generation is cognitively demanding)
- **Player** focuses on code generation; Haiku is fast & cheap for most code tasks
- **Coach** must be a credible critic; keep it strong (reason parity with Architect)

## Observability: Know What Your Loop Is Doing

This skill includes **built-in observability** to prevent token waste and give you confidence the loop is functioning correctly.

### How observability works

Every time you run the script, it automatically writes a **JSON observability log** to your project directory (filename like `dialectical-loop-20251213-143115.json`). This log captures:

- **Per-turn breakdown**: agent (Architect/Player/Coach), model used, action, tokens (estimated), outcome, duration.
- **Summary stats**: total turns, total tokens, approval/rejection counts, any errors.
- **Alerts**: if a loop gets stuck rejecting, or tokens are unexpectedly high.

### Output modes

```bash
# Quiet: minimal output (only summary + log file path)
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 --quiet

# Normal (default): one-line-per-turn feedback + summary
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10

# Verbose: detailed debugging output (includes snippets)
python ~/.claude/skills/dialectical-loop/scripts/dialectical_loop.py --max-turns 10 --verbose
```

### Interpreting logs

Open the generated JSON file to diagnose loop health:

```json
{
  "summary": {
    "total_turns_executed": 3,
    "total_tokens_estimated": 12500,
    "coach_calls": {
      "approved": 1,
      "rejected": 1
    }
  }
}
```

**What to watch for:**

- **High token count**: May indicate spec is too verbose; try refining REQUIREMENTS.md.
- **Many Coach rejections**: Player is misunderstanding the spec; check SPECIFICATION.md clarity.
- **Errors in logs**: Review stderr output + the error field in the JSON.

## Using other LLM providers (OpenAI, Anthropic, Gemini, Azure, Bedrock, local)

This skill does not require any non-GitHub API keys by default, because it does not call Claude/OpenAI/Gemini APIs directly.

If you want to run the same dialectical loop against another provider, you’ll need to modify the backend used to obtain LLM responses in `scripts/dialectical_loop.py` (the `get_llm_response(...)` function). Once you do that, credentials become provider-specific:

- **OpenAI / OpenRouter / compatible**: typically `OPENAI_API_KEY` (and possibly a custom base URL).
- **Anthropic (Claude API)**: typically `ANTHROPIC_API_KEY`.
- **Google Gemini**: commonly `GOOGLE_API_KEY` (or Application Default Credentials depending on your setup).
- **Azure OpenAI**: typically `AZURE_OPENAI_API_KEY` plus an endpoint and deployment name.
- **AWS Bedrock**: AWS credentials/IAM (environment variables or configured profiles).
- **Local models** (Ollama/LM Studio): usually no API key, but a local server must be running.

The `--player-model` / `--coach-model` / `--architect-model` flags are currently passed to the Copilot CLI as-is; if you switch providers, you can reinterpret those flags in your backend implementation.

## Repo layout

- `SKILL.md` — OpenSkills entrypoint
- `scripts/dialectical_loop.py` — orchestrator
- `agents/*.md` — prompts for Architect/Player/Coach (and other roles, if you expand later)
- `REQUIREMENTS.example.md` — example task file (not used by the loop unless you copy it)
