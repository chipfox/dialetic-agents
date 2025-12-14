"""TypeScript/JavaScript-specific analysis utilities.

This module contains functions for analyzing TypeScript/JavaScript codebases,
including error parsing, module resolution, type definition extraction, and
import analysis. These functions are optional and only loaded when needed.
"""

import os
import re
from pathlib import Path


def extract_relevant_paths_from_output(output: str, root_dir: str = ".") -> list[str]:
    """Best-effort extraction of repo-relative paths from build/lint output."""
    text = output or ""
    candidates: set[str] = set()

    # Common Unix/Next.js style: ./src/foo.ts:12:34
    for m in re.finditer(r"(?P<p>\./[^\s:]+\.(?:ts|tsx|js|jsx|json|md|css|scss))(?::\d+)*(?:\b|$)", text):
        candidates.add(m.group("p"))

    # Repo-relative: src/foo.ts:12:34
    for m in re.finditer(r"(?P<p>(?:src|scripts|agents|app|pages|components|lib|types)/[^\s:]+\.(?:ts|tsx|js|jsx|json|md|css|scss))(?::\d+)*(?:\b|$)", text):
        candidates.add(m.group("p"))

    # Windows absolute: C:\...\src\foo.ts:12:34
    for m in re.finditer(r"(?P<p>[A-Za-z]:\\[^\r\n:]+\.(?:ts|tsx|js|jsx|json|md|css|scss))(?::\d+)*(?:\b|$)", text):
        candidates.add(m.group("p"))

    rel_paths: list[str] = []
    for p in candidates:
        try:
            p2 = p.rstrip(".,)")
            if p2.startswith("./"):
                p2 = p2[2:]
            if re.match(r"^[A-Za-z]:\\", p2):
                # Convert to repo-relative if possible
                abs_path = os.path.abspath(p2)
                rel = os.path.relpath(abs_path, os.path.abspath(root_dir))
                rel = rel.replace("\\", "/")
                if not rel.startswith(".."):
                    p2 = rel
                else:
                    continue
            rel_paths.append(p2)
        except Exception:
            continue

    # De-dupe while preserving order
    seen: set[str] = set()
    ordered: list[str] = []
    for p in rel_paths:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def parse_ts_missing_property_error(text: str) -> dict:
    """Parse errors like: Property 'x' does not exist on type 'Y'."""
    s = text or ""
    m = re.search(r"Property\s+'(?P<prop>[^']+)'\s+does not exist on type\s+'(?P<typ>[^']+)'", s)
    if not m:
        return {}
    file_m = re.search(r"(?m)^(?:\./)?(?P<file>(?:src|app|pages|components|lib|types)/[^\s:]+\.(?:ts|tsx|js|jsx)):(?P<line>\d+):(?P<col>\d+)", s)
    return {
        "property": m.group("prop"),
        "type": m.group("typ"),
        "file": (file_m.group("file") if file_m else ""),
        "line": (int(file_m.group("line")) if file_m else None),
        "col": (int(file_m.group("col")) if file_m else None),
    }


def resolve_ts_module_to_file(from_file: str, module_spec: str) -> str:
    """Resolve a TS/JS module specifier to a repo-relative file path when possible."""
    spec = (module_spec or "").strip()
    if not spec:
        return ""

    base_dir = Path(from_file).parent if from_file else Path(".")

    if spec.startswith("@/"):
        rel = Path("src") / spec[2:]
    elif spec.startswith("./") or spec.startswith("../"):
        rel = (base_dir / spec).resolve()
        try:
            rel = Path(os.path.relpath(str(rel), str(Path(".").resolve())))
        except Exception:
            return ""
    else:
        return ""

    # Try common TS entrypoint patterns
    candidates = []
    if rel.suffix:
        candidates.append(rel)
    else:
        candidates.extend(
            [
                Path(str(rel) + ".ts"),
                Path(str(rel) + ".tsx"),
                Path(str(rel) + ".d.ts"),
                rel / "index.ts",
                rel / "index.tsx",
            ]
        )

    for c in candidates:
        try:
            abs_c = (Path(".") / c).resolve()
            if abs_c.exists() and abs_c.is_file():
                return str(c).replace("\\", "/")
        except Exception:
            continue
    return ""


def extract_ts_type_definition_snippet(type_file: str, type_name: str, max_lines: int = 120) -> str:
    """Extract an exported interface/type definition block for `type_name` from `type_file`."""
    try:
        abs_path = (Path(".") / type_file).resolve()
        if not abs_path.exists() or not abs_path.is_file():
            return ""
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""

    # Find start
    start_idx = -1
    start_pat = re.compile(rf"^\s*export\s+(interface|type)\s+{re.escape(type_name)}\b")
    for i, line in enumerate(lines):
        if start_pat.search(line):
            start_idx = i
            break
    if start_idx == -1:
        # Fallback: non-exported (still useful)
        alt_pat = re.compile(rf"^\s*(interface|type)\s+{re.escape(type_name)}\b")
        for i, line in enumerate(lines):
            if alt_pat.search(line):
                start_idx = i
                break
    if start_idx == -1:
        return ""

    # Capture block
    out = []
    brace = 0
    started_brace = False
    for j in range(start_idx, min(len(lines), start_idx + max_lines)):
        l = lines[j]
        out.append(f"{j+1:>4} | {l}")
        # naive brace tracking (good enough for interface/type blocks)
        brace += l.count("{")
        brace -= l.count("}")
        if l.count("{"):
            started_brace = True
        if started_brace and brace <= 0 and ("}" in l):
            break
        if not started_brace and l.rstrip().endswith(";"):
            break
    return "\n".join(out).rstrip()


def find_import_for_symbol(file_head: str, symbol_name: str) -> str:
    """Find module specifier that imports `symbol_name` in a file header snippet."""
    if not file_head or not symbol_name:
        return ""
    # Match: import { A, B as C } from 'x'
    for m in re.finditer(r"(?m)^\s*import\s+(?:type\s+)?\{(?P<body>[^}]+)\}\s+from\s+['\"](?P<mod>[^'\"]+)['\"]", file_head):
        body = m.group("body")
        names = [n.strip() for n in body.split(",") if n.strip()]
        for n in names:
            # handle "Foo as Bar"
            parts = [p.strip() for p in n.split(" as ")]
            if parts and parts[0] == symbol_name:
                return m.group("mod")
    return ""


def extract_local_import_module_specs(file_path: str, max_lines: int = 120) -> list[str]:
    """Extract local module specifiers from import/export-from statements (1 hop)."""
    # Read file head inline to avoid circular dependency
    try:
        p = Path(file_path)
        if p.is_absolute():
            abs_path = p
        else:
            abs_path = Path(".") / p
        if not abs_path.exists() or not abs_path.is_file():
            return []
        
        lines = []
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f, start=1):
                if i > max_lines:
                    break
                lines.append(line)
        head = "".join(lines)
    except Exception:
        return []
    
    if not head:
        return []
    specs: list[str] = []
    for m in re.finditer(r"(?m)^\s*(?:import|export)\s+.*?\s+from\s+['\"](?P<mod>[^'\"]+)['\"]", head):
        mod = (m.group("mod") or "").strip()
        if mod.startswith("./") or mod.startswith("../") or mod.startswith("@/"):
            specs.append(mod)
    # CommonJS require
    for m in re.finditer(r"require\(\s*['\"](?P<mod>[^'\"]+)['\"]\s*\)", head):
        mod = (m.group("mod") or "").strip()
        if mod.startswith("./") or mod.startswith("../") or mod.startswith("@/"):
            specs.append(mod)
    # de-dupe
    out = []
    seen = set()
    for s in specs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def expand_paths_with_direct_imports(paths: list[str], max_total: int = 12) -> list[str]:
    """Expand paths with their direct local imports (1 hop), keeping list small."""
    if not paths:
        return []
    expanded: list[str] = []
    seen: set[str] = set()

    def add(p: str):
        if not p:
            return
        p2 = p.replace("\\", "/")
        if p2 not in seen:
            seen.add(p2)
            expanded.append(p2)

    for p in paths:
        add(p)
        if len(expanded) >= max_total:
            return expanded
        for mod in extract_local_import_module_specs(p):
            resolved = resolve_ts_module_to_file(p, mod)
            if resolved:
                add(resolved)
                if len(expanded) >= max_total:
                    return expanded
    return expanded


def module_specifiers_for_file(rel_path: str) -> list[str]:
    """Generate common import specifiers for a repo-relative file."""
    p = (rel_path or "").replace("\\", "/")
    if not p:
        return []
    # Strip extension
    no_ext = re.sub(r"\.(tsx|ts|jsx|js)$", "", p)
    specs = []
    # Support common Next.js directory structures for @/ alias
    for prefix in ("src/", "app/", "pages/", "lib/", "components/"):
        if no_ext.startswith(prefix):
            specs.append("@/" + no_ext[len(prefix):])
            break
    # Also allow importing the full path without ext (rare but possible)
    specs.append(no_ext)
    return list(dict.fromkeys(specs))


def is_new_file_referenced(new_file: str, edited_file_contents: dict[str, str]) -> bool:
    """Guardrail: if we create a new file, ensure it is referenced by imports.

    Accept if:
    - any current edited file contains an import specifier for it, OR
    - git grep finds an existing import/reference in the repo, OR
    - the file matches a framework convention (e.g., Next.js API routes, page routes).
    """
    # Framework convention: Next.js API routes (app/api/**/route.ts) and page routes (app/**/page.tsx)
    # These files are discovered via file-system routing and don't need explicit imports
    normalized = new_file.replace("\\", "/")
    if re.match(r"^(src/)?(app/api/.+/route\.(ts|js)x?)$", normalized):
        return True  # Next.js API route
    if re.match(r"^(src/)?(app/.+/page\.(tsx|jsx))$", normalized):
        return True  # Next.js page route
    if re.match(r"^(src/)?(pages/.+\.(tsx|jsx|ts|js))$", normalized):
        return True  # Next.js pages directory route
    
    specs = module_specifiers_for_file(new_file)
    # If no specs could be generated for a code file, be conservative (deny creation)
    # unless it's a non-code file (config, markdown, etc.)
    if not specs:
        is_code_file = new_file.endswith((".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".cpp", ".c", ".go"))
        return not is_code_file  # Allow non-code files, deny code files without import specs

    for _path, content in (edited_file_contents or {}).items():
        for s in specs:
            if s and s in (content or ""):
                return True

    # Try fast repo search via git grep in common code directories
    import subprocess
    try:
        for s in specs:
            if not s:
                continue
            result = subprocess.run(
                ["git", "grep", "-n", s, "--", "src", "app", "pages", "lib", "components"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
                cwd="."
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
    except Exception:
        pass

    return False

