"""Unified-diff parsing with right-side (new file) line numbers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class DiffLine:
    kind: str  # "+", "-", " "
    new_lineno: int | None
    text: str


@dataclass
class FileDiff:
    path: str
    old_path: str | None = None
    is_deleted: bool = False
    is_binary: bool = False
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added_lines(self) -> set[int]:
        return {ln.new_lineno for ln in self.lines if ln.kind == "+" and ln.new_lineno is not None}

    def annotated(self, max_chars: int = 20_000) -> str:
        """Render the patch with new-file line numbers so the model can cite exact lines."""
        out: list[str] = []
        for ln in self.lines:
            if ln.kind == "@":
                out.append(ln.text)
            elif ln.kind == "-":
                out.append(f"      - {ln.text}")
            else:
                out.append(f"L{ln.new_lineno:<5}{ln.kind} {ln.text}")
        text = "\n".join(out)
        return text if len(text) <= max_chars else text[:max_chars] + "\n... [truncated]"


def parse_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    new_line = 0
    in_hunk = False
    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", raw)
            current = FileDiff(path=m.group(2) if m else raw.split()[-1])
            files.append(current)
            in_hunk = False
            continue
        if current is None:
            continue
        if not in_hunk:
            if raw.startswith("--- "):
                old = raw[4:]
                current.old_path = None if old == "/dev/null" else old.removeprefix("a/")
                continue
            if raw.startswith("+++ "):
                new = raw[4:]
                if new == "/dev/null":
                    current.is_deleted = True
                else:
                    current.path = new.removeprefix("b/")
                continue
            if raw.startswith("Binary files"):
                current.is_binary = True
                continue
        m = HUNK_RE.match(raw)
        if m:
            new_line = int(m.group(1))
            in_hunk = True
            current.lines.append(DiffLine("@", None, raw))
            continue
        if not in_hunk:
            continue
        if raw.startswith("+"):
            current.lines.append(DiffLine("+", new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            current.lines.append(DiffLine("-", None, raw[1:]))
        elif raw.startswith(" ") or raw == "":
            current.lines.append(DiffLine(" ", new_line, raw[1:]))
            new_line += 1
        # "\ No newline at end of file" and anything else is ignored
    return files


def file_as_diff(path: str, content: str) -> FileDiff:
    """Treat a whole file as newly added (used by the IDE endpoint)."""
    fd = FileDiff(path=path)
    fd.lines.append(DiffLine("@", None, f"@@ -0,0 +1,{content.count(chr(10)) + 1} @@"))
    for i, line in enumerate(content.splitlines(), start=1):
        fd.lines.append(DiffLine("+", i, line))
    return fd


def git_diff(base: str, head: str = "HEAD", cwd: str = ".") -> str:
    result = subprocess.run(
        ["git", "diff", "--unified=3", "--no-color", f"{base}...{head}"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout
