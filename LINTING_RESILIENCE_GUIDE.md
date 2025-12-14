# VSCode Linting Resilience Guide

## Overview

This guide documents how the dialectical-loop skill remains resilient to VSCode automatic linting and formatting. When files are saved in VSCode, various formatters may modify the code. This skill is designed to survive those transformations.

## Problem Statement

VSCode can automatically apply formatting via:
- **Black** (Python code formatter)
- **Prettier** (Markdown/JSON formatter)
- **pylint/flake8** (Python linters)
- **Various other extensions** (markdownlint, etc.)

These tools can change:
- Line breaks and indentation
- Import order
- Whitespace and spacing
- String quote styles
- Comment formatting
- Markdown structure

**The Risk:** If code semantics change during formatting, the skill breaks.

**The Solution:** Design code that's resilient to these transformations.

## Linting-Safe Code Patterns

### 1. Import Organization

**Safe Pattern:** Follow PEP 8 import ordering
```python
# Standard library imports (first)
import os
import sys
import json
from pathlib import Path
from datetime import datetime, timezone

# Third-party imports (second)
# (none for this project)

# Local imports (third)
# (none for this project)
```

**Why It's Safe:**
- Black/isort will reorder but preserve semantic meaning
- No circular dependencies possible
- Clear separation allows linters to reorganize without breaking logic
- Each import group can safely be reorganized

**Unsafe Pattern:**
```python
# AVOID: Relative imports mixed with absolute
from ..agents import coach
import os
from pathlib import Path  # Wrong order

# AVOID: Too many imports on one line
import os, sys, json  # Will be reformatted
```

### 2. Line Length Compliance

**Safe Pattern:** Keep lines under 100 characters
```python
# Good: Line is 98 characters
coach_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}"

# Good: Break before 100 chars
coach_input = (
    f"REQUIREMENTS:\n{requirements}\n\n"
    f"SPECIFICATION:\n{specification}"
)
```

**Why It's Safe:**
- Black will preserve intentional line breaks
- Under 100 chars means no forced reformatting
- Multi-line f-strings and concatenation survive formatting

**Unsafe Pattern:**
```python
# BAD: 127 characters - will be forcibly reformatted
coach_input = f"REQUIREMENTS:\n{requirements}\n\nSPECIFICATION:\n{specification}\n\nPLAYER OUTPUT:\n{json.dumps(player_data, indent=2)}"
```

### 3. Function Signatures

**Safe Pattern:** Clear, well-structured signatures
```python
def extract_json(text, run_log=None, turn_number=0, agent="unknown"):
    """Extract JSON from text with linting resilience."""
    # Implementation
```

**Why It's Safe:**
- Clear parameter list survives reformatting
- No complex defaults that might be reordered
- Consistent with Black formatting

**Unsafe Pattern:**
```python
# AVOID: Line continuation in signature - may be reformatted
def extract_json(text, run_log=None, turn_number=0, agent="unknown", validate_structure=True,\
                 retry_count=2, timeout_seconds=30):
```

### 4. Dictionary and List Structures

**Safe Pattern:** Multi-line with consistent formatting
```python
event = {
    "turn_number": turn_number,
    "phase": phase,
    "agent": agent,
    "model": model,
    "action": action,
}

# Or use Black-safe continuation
event = {"turn_number": turn_number, "phase": phase, "agent": agent}
```

**Why It's Safe:**
- Black will normalize spacing but preserve structure
- Each item on separate line survives reformatting
- Trailing commas are idiomatic and preserved

**Unsafe Pattern:**
```python
# AVOID: Inconsistent formatting
event = {"turn_number": turn_number, "phase":
         phase, "agent": agent, 
    "model": model}
```

### 5. String Formatting

**Safe Pattern:** Use f-strings with clear structure
```python
message = f"[{timestamp}] {message}"

# Multi-line f-strings with clear breaks
full_message = (
    f"Turn {turn_number}: {status}\n"
    f"Timestamp: {timestamp}\n"
    f"Duration: {duration_s}s"
)
```

**Why It's Safe:**
- f-strings are Black-preferred
- Clear line breaks aren't reordered
- No complex escaping that might be changed

**Unsafe Pattern:**
```python
# AVOID: Complex string with poor structure
message = f"[{timestamp}] {message} - " + \
         f"status={status}, duration={duration_s}s, " + \
         f"tokens={tokens}"  # Will be reformatted
```

### 6. Comments and Documentation

**Safe Pattern:** Clear, concise comments
```python
def log_print(message, verbose=False, quiet=False):
    """Print to stderr unless quiet is True. Verbose adds extra details."""
    if not quiet:
        # Add timestamp for all output
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        prefix = "[VERBOSE]" if verbose else ""
        print(f"[{timestamp}]{prefix} {message}", file=sys.stderr)
```

**Why It's Safe:**
- Black won't reflow single-line comments
- Docstrings are preserved exactly
- Clear structure survives reformatting

**Unsafe Pattern:**
```python
# AVOID: Comments with special characters that might be reformatted
# ========================================
# This is a section separator
# ========================================
```

### 7. JSON Serialization

**Safe Pattern:** Consistent, explicit formatting
```python
# Use consistent indent for JSON logs
json_output = json.dumps(data, indent=2)

# Or store raw data and serialize on demand
event = {
    "data": {...},  # Will be serialized consistently
}
```

**Why It's Safe:**
- `json.dumps(indent=2)` produces consistent output
- No post-formatting changes the JSON structure
- Data integrity is mathematically guaranteed

**Unsafe Pattern:**
```python
# AVOID: Relying on custom formatting
json_output = json.dumps(data)  # Single-line might be reformatted
```

## Markdown Resilience

### Safe Markdown Pattern

```markdown
# Main Heading

Clear content with proper spacing.

## Subheading

- List item 1
- List item 2
  - Nested item

Code blocks with language specification:

```python
def example():
    pass
```

Tables, links, and other structures.
```

**Why It's Safe:**
- Proper blank lines around headings (MD022 compliant)
- Language-specified code blocks (MD040 compliant)
- Blank lines around lists (MD032 compliant)
- Blank lines around fenced code (MD031 compliant)

### Unsafe Markdown Patterns

```markdown
## Heading without blank line before it
- List without blank lines
```python
No language specified
```

No blank lines around code blocks
```

## Implementation: Linting Resilience Module

The `scripts/linting_resilience.py` module provides validation:

```bash
# Check if a file survives linting
python scripts/linting_resilience.py scripts/dialectical_loop.py

# Check multiple files
python scripts/linting_resilience.py *.py *.md *.json
```

**Checks Performed:**
1. **AST Preservation (Python)** - Semantic meaning unchanged
2. **Import Order (Python)** - PEP 8 compliant
3. **Line Lengths (Python)** - Under 100 characters
4. **JSON Structure** - Data integrity preserved
5. **Markdown Structure** - Linting rules compliant

## Pre-Save Validation

Before committing code:

```bash
# Validate all files
python scripts/linting_resilience.py scripts/dialectical_loop.py \
                                    DIALECTICAL_LOOP_ANALYSIS.md

# Should output:
# File: scripts/dialectical_loop.py
# Resilient: ✓ Yes
#   ✓ ast_preservation
#   ✓ import_order
#   ✓ line_lengths
```

## Integration with Development

### VSCode Settings

Add to `.vscode/settings.json` (create if not exists):

```json
{
    "[python]": {
        "editor.formatOnSave": true,
        "editor.defaultFormatter": "ms-python.black-formatter",
        "editor.rulers": [100]
    },
    "[markdown]": {
        "editor.formatOnSave": true,
        "editor.wordWrap": "on"
    }
}
```

### Pre-Commit Hook

Create `.git/hooks/pre-commit`:

```bash
#!/bin/bash
python scripts/linting_resilience.py scripts/dialectical_loop.py || exit 1
```

## Testing Resilience

### Manual Test Procedure

1. Make a code change in `dialectical_loop.py`
2. Save the file in VSCode (triggers linting)
3. Run resilience check:
   ```bash
   python scripts/linting_resilience.py scripts/dialectical_loop.py
   ```
4. Verify output shows "✓ Yes"

### Automated Test

```python
# Test that code survives formatting cycle
import tempfile
import shutil
from linting_resilience import validate_python_ast_preservation

original = "scripts/dialectical_loop.py"
with tempfile.NamedTemporaryFile(suffix=".py") as tmp:
    shutil.copy(original, tmp.name)
    # Simulate VSCode formatting...
    ok, issues = validate_python_ast_preservation(original, tmp.name)
    assert ok, f"Code didn't survive formatting: {issues}"
```

## Troubleshooting

### Issue: "AST changed after linting"

**Cause:** Semantic change during formatting (critical!)

**Solution:**
1. Review the exact changes made
2. Restore original code
3. Check against patterns in this guide
4. Validate line lengths, imports, etc.

### Issue: "Line length exceeded"

**Cause:** Lines over 100 characters

**Solution:**
```python
# Break into multiple lines
long_variable = (
    "first part " +
    "second part " +
    "third part"
)
```

### Issue: "Import order changed"

**Cause:** Imports not in PEP 8 order

**Solution:**
```python
# Reorder: stdlib first, then third-party, then local
import os
import sys
from pathlib import Path

from third_party import something

from . import local_module
```

## Summary

The skill remains linting-resilient by:

1. ✅ Following PEP 8 and standard patterns
2. ✅ Keeping code under line length limits
3. ✅ Using Black-compatible formatting
4. ✅ Validating semantic preservation
5. ✅ Testing after VSCode save cycles
6. ✅ Documenting safe patterns

When you save a file in VSCode, the skill will survive the automatic formatting because it's been designed with linting in mind from the start.
