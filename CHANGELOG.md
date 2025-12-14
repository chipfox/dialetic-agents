# Changelog

All notable changes to dialectical-loop will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.4] - 2025-12-14

### Changed

- **OpenSkills Compliance**: Complete SKILL.md frontmatter with version, tags, author, repository, license
  - Improves AI discoverability from C (40/100) to A (95/100) grade
- **Context Builder Unification**: Refactored duplicate context building functions
  - New `build_context(mode="snapshot"|"changed")` function eliminates ~130 lines of duplication
  - Maintains backward compatibility via wrapper functions
  - Extracted `_build_file_list()` helper for cleaner mode-specific logic

### Fixed

- Addressed CODE_REVIEW.md Priority 1 & 2 items from systematic audit
- Zero breaking changes - all module imports verified

## [1.0.3] - 2025-12-14

### Changed

- **Module Extraction**: Refactored main orchestrator into focused modules
  - `observability.py`: RunLog class and logging utilities (177 lines)
  - `llm_client.py`: GitHub Copilot CLI interaction (320 lines)
  - `context_builder.py`: Codebase snapshot and file operations (432 lines)
  - Main orchestrator reduced from 2,970 → 2,325 lines (22% reduction)
- Improved code maintainability and testability through separation of concerns

### Fixed

- No functional changes - purely organizational refactoring

## [1.0.2] - 2025-12-14

### Added

- **Replan Capability**: Coach can now trigger Architect re-design when fundamental design flaws detected
  - New `REPLAN_NEEDED` status in Coach response schema
  - Architect accepts feedback parameter for specification revision
  - Replan doesn't consume turn budget (optimizes for ≤5 turn completion)
  - Full observability tracking for replan events
- **Context Caching**: Orchestrator-level caching for token optimization
  - `ContextCache` class tracks repeated requirements/specifications
  - Hash-based deduplication with per-turn fingerprinting
  - Expected 30-50% token savings on repeated content
  - Cache performance metrics in observability logs

### Changed

- Coach agent prompt updated with replan guidance and when to use REPLAN_NEEDED
- Architect agent prompt enhanced with replan mode instructions
- RunLog summary now tracks replan decisions separately from approvals/rejections
- Main loop continues after replan without incrementing turn counter

### Fixed

- Coach decision logging now properly categorizes approved/rejected/replan statuses

## [1.0.1] - 2025-01-20

### Added

- TypeScript analyzer module (`scripts/ts_analyzer.py`) with optional import
- Shared agent context documentation (`agents/_shared.md`)
- Examples directory with sample REQUIREMENTS, SPECIFICATION, and run logs
- Comprehensive example README explaining workflow patterns

### Changed

- Extracted 9 TypeScript-specific functions to separate module
- Main orchestrator reduced by ~300 lines
- Improved modularity for non-TypeScript projects

### Fixed

- Optional TS analyzer import allows graceful fallback when module unavailable

## [1.0.0] - 2025-01-20

### Added

- Production observability with JSON logs (`dialectical-loop-TIMESTAMP.json`)

- Per-turn event tracking (agent, model, tokens, duration)
- Loop health metrics (zero-edit streaks, fast-fail spirals)
- Inter-agent communication tracking (feedback coverage, error persistence)
- Real-time warnings for stuck patterns
- Token-optimized lean mode (`--lean-mode`)
- Fast-fail optimization (skip Coach if verification fails)
- Auto-context switching (full snapshot turn 1 → git-changed thereafter)
- Coach focus-recent mode (reviews only changed files)
- Dynamic specification pruning
- Three-tier model recommendations (Balanced/Budget/Premium)
- Cross-platform shell configuration support
- Bounded iteration with `--max-turns`
- Adversarial cooperation workflow (Architect → Player ↔ Coach)

### Changed

- Optimized SKILL.md format following OpenSkills guidelines
- Condensed documentation with progressive disclosure
- Moved detailed documentation to README.md

### Fixed

- Improved token efficiency with multiple optimization strategies
- Enhanced verification command handling

### Removed

- Orphaned utility scripts (extract_pdf_text.py, linting_resilience.py)

## [Unreleased]

Initial development and feature additions leading to v1.0.0.
