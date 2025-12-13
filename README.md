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
- `--player-model gemini-3-pro-preview`
- `--coach-model claude-sonnet-4.5`
- `--architect-model claude-sonnet-4.5`

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
