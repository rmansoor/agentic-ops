"""Test generation: find untested Python functions, have the LLM write pytest tests, keep only passing ones."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .llm import LLMClient, ToolSpec, structured_call
from .models import GeneratedTests

SYSTEM = """You write focused, deterministic pytest tests for existing Python code.
- Import the module under test using the import path you are given.
- Cover normal cases, edge cases and error paths for the listed functions only.
- No network, no sleeping, no reliance on wall-clock time; use tmp_path and monkeypatch for I/O.
- Do not modify or re-implement the code under test. Tests must pass against the current code:
  if behaviour looks like a bug, assert the current behaviour and note it in `notes`.
Return the full test module via the `submit_tests` tool."""

TESTS_TOOL = ToolSpec(
    "submit_tests",
    "Submit a complete pytest module.",
    {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "Complete Python test module."},
            "notes": {"type": "string", "description": "Suspected bugs or untestable parts."},
        },
        "required": ["code"],
    },
)


@dataclass
class FuncInfo:
    name: str
    start: int
    end: int


@dataclass
class TestGenResult:
    source: str
    test_file: str | None
    functions: list[str]
    passed: bool
    attempts: int
    notes: str = ""
    output: str = field(default="", repr=False)


def module_import_path(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def public_functions(source: str) -> list[FuncInfo]:
    tree = ast.parse(source)
    out: list[FuncInfo] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_"):
            out.append(FuncInfo(node.name, node.lineno, node.end_lineno or node.lineno))
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    not sub.name.startswith("_") or sub.name == "__init__"
                ):
                    out.append(FuncInfo(f"{node.name}.{sub.name}", sub.lineno, sub.end_lineno or sub.lineno))
    return out


def untested(funcs: list[FuncInfo], rel: str, coverage_json: str | None) -> list[FuncInfo]:
    """With a coverage.py JSON report, keep functions that have missing lines; otherwise keep all."""
    if not coverage_json or not Path(coverage_json).is_file():
        return funcs
    files = json.loads(Path(coverage_json).read_text()).get("files", {})
    entry = files.get(rel) or next((v for k, v in files.items() if k.endswith(rel)), None)
    if entry is None:
        return funcs  # file never imported by the test suite: everything is untested
    missing = set(entry.get("missing_lines", []))
    return [f for f in funcs if missing & set(range(f.start, f.end + 1))]


def run_pytest(test_file: Path, cwd: Path) -> tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(test_file)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-6000:]


def generate_tests(
    llm: LLMClient,
    repo_dir: str | Path,
    source_file: str,
    out_dir: str = "tests/generated",
    coverage_json: str | None = None,
    max_repairs: int = 1,
    run: bool = True,
) -> TestGenResult:
    root = Path(repo_dir).resolve()
    src_path = (root / source_file).resolve()
    rel = src_path.relative_to(root)
    source = src_path.read_text()
    targets = untested(public_functions(source), rel.as_posix(), coverage_json)
    if not targets:
        return TestGenResult(rel.as_posix(), None, [], True, 0, "Nothing untested.")
    names = [f.name for f in targets]
    prompt = (
        f"Module import path: `{module_import_path(rel)}`\nFile: `{rel.as_posix()}`\n"
        f"Write tests for: {', '.join(names)}\n\n```python\n{source[:40_000]}\n```"
    )
    out = root / out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "__init__.py").touch(exist_ok=True)
    test_file = out / f"test_{rel.stem}_generated.py"

    generated = structured_call(llm, SYSTEM, prompt, TESTS_TOOL, GeneratedTests)
    attempts = 1
    test_file.write_text(generated.code)
    if not run:
        return TestGenResult(rel.as_posix(), str(test_file.relative_to(root)), names, False, attempts, generated.notes)

    passed, output = run_pytest(test_file, root)
    while not passed and attempts <= max_repairs:
        repair_prompt = (
            f"{prompt}\n\nYour previous test module:\n```python\n{generated.code}\n```\n"
            f"pytest output:\n```\n{output}\n```\nFix the TESTS (not the code under test) so they pass. "
            "Drop any test you cannot make pass and explain why in `notes`."
        )
        generated = structured_call(llm, SYSTEM, repair_prompt, TESTS_TOOL, GeneratedTests)
        attempts += 1
        test_file.write_text(generated.code)
        passed, output = run_pytest(test_file, root)

    if not passed:
        test_file.unlink(missing_ok=True)
        return TestGenResult(rel.as_posix(), None, names, False, attempts, generated.notes, output)
    return TestGenResult(
        rel.as_posix(), str(test_file.relative_to(root)), names, True, attempts, generated.notes, output
    )
