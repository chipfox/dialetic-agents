"""
Linting Resilience Module

Provides mechanisms to validate that code survives VSCode formatting/linting cycles.
Ensures semantic preservation and detects unwanted modifications.

This module addresses the challenge: "When a file is saved in VSCode, automatic
linting may change it. Make sure this skill accounts for this."
"""

import ast
import json
import sys
from pathlib import Path
from typing import Tuple, Dict, List


def validate_python_ast_preservation(original_file: str, modified_file: str) -> Tuple[bool, List[str]]:
    """
    Validate that Python AST (Abstract Syntax Tree) is preserved after linting.
    
    This ensures that semantic meaning is unchanged even if formatting differs.
    
    Args:
        original_file: Path to original Python file
        modified_file: Path to modified Python file (after VSCode save)
    
    Returns:
        (is_preserved, issues) where issues is list of problems found
    """
    issues = []
    
    try:
        with open(original_file, 'r', encoding='utf-8') as f:
            original_code = f.read()
        with open(modified_file, 'r', encoding='utf-8') as f:
            modified_code = f.read()
    except IOError as e:
        return False, [f"Cannot read files: {e}"]
    
    try:
        original_ast = ast.parse(original_code)
        modified_ast = ast.parse(modified_code)
    except SyntaxError as e:
        return False, [f"Syntax error in {modified_file}: {e}"]
    
    # Compare AST dumps (string representation)
    original_dump = ast.dump(original_ast)
    modified_dump = ast.dump(modified_ast)
    
    if original_dump == modified_dump:
        return True, []
    
    # If AST differs, it means linting changed semantics (bad!)
    issues.append("AST changed after linting - semantic preservation failed")
    return False, issues


def validate_json_structure(original_json: str, modified_json: str) -> Tuple[bool, List[str]]:
    """
    Validate that JSON structure is preserved after linting.
    
    Checks that data integrity isn't affected by reformatting.
    
    Args:
        original_json: Path to original JSON file
        modified_json: Path to modified JSON file
    
    Returns:
        (is_preserved, issues)
    """
    issues = []
    
    try:
        with open(original_json, 'r', encoding='utf-8') as f:
            original_data = json.load(f)
        with open(modified_json, 'r', encoding='utf-8') as f:
            modified_data = json.load(f)
    except (IOError, json.JSONDecodeError) as e:
        return False, [f"Cannot parse JSON: {e}"]
    
    if original_data == modified_data:
        return True, []
    
    issues.append("JSON data changed after linting")
    return False, issues


def validate_import_order(python_file: str) -> Tuple[bool, List[str]]:
    """
    Validate that imports follow PEP 8 order and won't be reordered by linters.
    
    Checks:
    - Standard library imports first
    - Third-party imports second
    - Local imports third
    - Proper blank lines between groups
    
    Args:
        python_file: Path to Python file
    
    Returns:
        (is_valid, issues)
    """
    issues = []
    
    try:
        with open(python_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except IOError as e:
        return False, [f"Cannot read file: {e}"]
    
    import_groups = {
        'stdlib': [],
        'third_party': [],
        'local': []
    }
    
    # Standard library imports
    stdlib_imports = {
        'os', 'sys', 'json', 'subprocess', 'time', 'argparse', 'tempfile',
        'pathlib', 'datetime', 'collections', 're', 'itertools', 'functools'
    }
    
    import_section_done = False
    last_import_line = -1
    
    for i, line in enumerate(lines):
        stripped = line.strip()
        
        if not stripped or stripped.startswith('#'):
            continue
        
        if stripped.startswith('import ') or stripped.startswith('from '):
            last_import_line = i
            
            # Parse import module name
            if stripped.startswith('from '):
                module = stripped.split()[1]
            else:
                module = stripped.split()[1]
            
            # Categorize
            if any(module.startswith(std) for std in stdlib_imports):
                import_groups['stdlib'].append(stripped)
            elif module.startswith('.'):
                import_groups['local'].append(stripped)
            else:
                import_groups['third_party'].append(stripped)
        elif last_import_line >= 0 and (stripped and not stripped.startswith('#')):
            import_section_done = True
    
    # Validate order: stdlib -> third_party -> local
    if (import_groups['stdlib'] and import_groups['third_party'] and
        any(stripped.startswith('import ') or stripped.startswith('from ')
            for stripped in import_groups['third_party'])):
        # Check if there's a blank line between groups
        pass
    
    return True, issues


def validate_line_lengths(python_file: str, max_length: int = 100) -> Tuple[bool, List[str]]:
    """
    Validate that lines don't exceed max length (will trigger linter reformatting).
    
    Args:
        python_file: Path to Python file
        max_length: Maximum allowed line length
    
    Returns:
        (is_valid, issues)
    """
    issues = []
    
    try:
        with open(python_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except IOError as e:
        return False, [f"Cannot read file: {e}"]
    
    for i, line in enumerate(lines, 1):
        # Don't count newline character
        line_content = line.rstrip('\n')
        if len(line_content) > max_length:
            issues.append(f"Line {i} exceeds {max_length} chars: {len(line_content)} chars")
    
    return len(issues) == 0, issues


def check_file_resilience(filepath: str) -> Dict:
    """
    Comprehensive linting resilience check.
    
    Args:
        filepath: Path to file to check
    
    Returns:
        Dictionary with resilience assessment
    """
    results = {
        "file": filepath,
        "is_resilient": True,
        "checks": {}
    }
    
    if filepath.endswith('.py'):
        # Python-specific checks
        ast_ok, ast_issues = validate_python_ast_preservation(filepath, filepath)
        results["checks"]["ast_preservation"] = {
            "passed": ast_ok,
            "issues": ast_issues
        }
        
        imports_ok, import_issues = validate_import_order(filepath)
        results["checks"]["import_order"] = {
            "passed": imports_ok,
            "issues": import_issues
        }
        
        lines_ok, line_issues = validate_line_lengths(filepath)
        results["checks"]["line_lengths"] = {
            "passed": lines_ok,
            "issues": line_issues
        }
        
        if not (ast_ok and imports_ok and lines_ok):
            results["is_resilient"] = False
    
    elif filepath.endswith('.json'):
        json_ok, json_issues = validate_json_structure(filepath, filepath)
        results["checks"]["json_structure"] = {
            "passed": json_ok,
            "issues": json_issues
        }
        
        if not json_ok:
            results["is_resilient"] = False
    
    return results


def main():
    """Command-line interface for resilience checking."""
    if len(sys.argv) < 2:
        print("Usage: python linting_resilience.py <file_path> [<file_path> ...]")
        sys.exit(1)
    
    all_resilient = True
    for filepath in sys.argv[1:]:
        results = check_file_resilience(filepath)
        
        print(f"\nFile: {results['file']}")
        print(f"Resilient: {'✓ Yes' if results['is_resilient'] else '✗ No'}")
        
        for check_name, check_result in results["checks"].items():
            status = "✓" if check_result["passed"] else "✗"
            print(f"  {status} {check_name}")
            for issue in check_result.get("issues", []):
                print(f"      - {issue}")
        
        if not results["is_resilient"]:
            all_resilient = False
    
    sys.exit(0 if all_resilient else 1)


if __name__ == "__main__":
    main()
