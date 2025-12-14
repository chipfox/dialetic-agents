# Shared Context: Dialectical Autocoding Workflow

This document contains principles and guidelines shared across all agents (Architect, Player, Coach).

## Convergence Goal

**All agents must optimize for completion within ≤5 turns.**

This is a hard constraint that shapes every decision:

- **Architect**: Design the plan so a competent implementer can finish in ≤5 turns
  - Turn 1 should get the project to a clean verification baseline (build/lint/test pass)
  - Subsequent turns should add missing features in smallest increments
  - Prefer "unblock build first" over implementing new features on a broken base

- **Player**: Aim to reach approval in ≤5 turns
  - Each turn must produce verifiable progress
  - Eliminate at least one Coach blocker per turn (prefer build/lint blockers first)
  - Make the smallest set of repo changes that resolves blockers

- **Coach**: Assume a turn budget of ≤5
  - Focus feedback on the smallest set of changes to get to green build/lint/test
  - Prioritize issues in the order the Player must fix them
  - Be incremental: describe the smallest delta to bridge the remaining gap

## Dialectical Principles

### Fresh Context Every Turn

- Each agent receives fresh context every turn - no memory of previous attempts
- Only current file state, requirements, and last feedback are available
- This forces explicit, self-contained communication

### Strict Requirements Compliance

- Specification (SPECIFICATION.md) is the primary contract
- Requirements (REQUIREMENTS.md) define high-level intent
- Implementation must satisfy both exactly - no scope expansion

### Evidence-Based Feedback

- All critique must cite exact failing commands
- Copy/paste relevant output lines (8-15 lines) to prove the issue
- Command outputs are the source of truth, not assumptions

### Bounded Iteration

- Fixed turn budget prevents runaway token usage
- Every action must move toward approval
- Approval only when ALL criteria met (build/lint/test pass, requirements satisfied)

## Specification Maintenance

**The Player is responsible for updating SPECIFICATION.md to save tokens:**

- Mark completed items as `[DONE]`
- Remove detailed instructions for fully completed and verified features
- Keep only high-level summaries of what was done
- This prevents re-reading the same specs every turn

**The Coach must verify these updates:**

- Allow spec updates that mark items as `[DONE]` or remove completed task details
- Verify that removed spec items are indeed fully implemented and verified
- This is a valid and encouraged token-saving strategy

**The Architect must write token-efficient specs:**

- Do NOT paste full file contents
- Prefer concise bullet lists, file paths, and small code snippets
- Include only minimum context needed to implement correctly

## Verification as Source of Truth

**All agents rely on command outputs for evidence:**

- Build commands (e.g., `npm run build`, `tsc`, `cargo build`)
- Lint commands (e.g., `npm run lint`, `eslint`, `ruff`)
- Test commands (e.g., `npm test`, `pytest`, `cargo test`)
- Framework-specific checks (e.g., Next.js dev server output)

**Success criteria:**

- Exit code 0 (or explicit success message)
- No error/warning output (or only acceptable warnings)
- All acceptance criteria from requirements met

## Workflow Summary

```text
┌──────────────┐
│  Architect   │  → Generates SPECIFICATION.md (once at start)
└──────────────┘
       ↓
┌──────────────┐
│    Player    │  → Implements code, runs verification
└──────────────┘
       ↓
┌──────────────┐
│    Coach     │  → Reviews, provides feedback or approval
└──────────────┘
       ↓
    (loop until approval or max turns)
```

**Player-Coach Loop:**

1. Player receives requirements + spec + feedback
2. Player edits files and runs verification commands
3. Coach reviews output and either approves or blocks
4. If blocked, loop continues with Coach's feedback
5. If approved or max turns reached, workflow ends

**Key Properties:**

- Architect runs once at the beginning
- Player and Coach alternate in a bounded loop
- Fresh context each turn (no persistent memory)
- Specification can be pruned as work completes
- Evidence-based progression toward approval
