"""Codebase index: symbol-aware chunking + BM25 retrieval (no external services needed).

Swap `search` for an embedding store (pgvector, Qdrant, etc.) when the repo outgrows lexical search.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".agentic_ops",
    "out",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "coverage",
    ".next",
    "target",
}
EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".java",
    ".rb",
    ".rs",
    ".cs",
    ".kt",
    ".sql",
    ".yml",
    ".yaml",
    ".md",
    ".toml",
    ".json",
    ".sh",
    ".tf",
}
MAX_FILE_BYTES = 200_000
WINDOW, OVERLAP, MAX_CHUNK_LINES = 60, 10, 120
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for tok in TOKEN_RE.findall(text):
        low = tok.lower()
        tokens.append(low)
        parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", tok.replace("_", " "))
        if len(parts) > 1:
            tokens.extend(p.lower() for p in parts)
    return tokens


@dataclass
class Chunk:
    path: str
    start: int
    end: int
    symbol: str
    text: str


def _windows(path: str, lines: list[str], start_offset: int = 1, symbol: str = "") -> list[Chunk]:
    chunks = []
    step = WINDOW - OVERLAP
    for i in range(0, max(len(lines), 1), step):
        seg = lines[i : i + WINDOW]
        if not seg:
            break
        chunks.append(Chunk(path, start_offset + i, start_offset + i + len(seg) - 1, symbol, "\n".join(seg)))
        if i + WINDOW >= len(lines):
            break
    return chunks


def chunk_file(path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    if path.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return _windows(path, lines)
        chunks: list[Chunk] = []
        covered: set[int] = set()
        for node in tree.body:
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                start = min([d.lineno for d in node.decorator_list] + [node.lineno])
                end = node.end_lineno or node.lineno
                seg = lines[start - 1 : end]
                covered.update(range(start, end + 1))
                if len(seg) > MAX_CHUNK_LINES:
                    chunks.extend(_windows(path, seg, start, node.name))
                else:
                    chunks.append(Chunk(path, start, end, node.name, "\n".join(seg)))
        rest = [ln if (i + 1) not in covered else "" for i, ln in enumerate(lines)]
        if any(r.strip() for r in rest):  # module-level code: imports, constants, main
            header = [i for i, r in enumerate(rest) if r.strip()]
            s, e = header[0], header[-1]
            chunks.extend(c for c in _windows(path, rest[s : e + 1], s + 1, "<module>") if c.text.strip())
        return chunks
    return _windows(path, lines)


class CodeIndex:
    def __init__(self, root: str | Path, chunks: list[Chunk] | None = None):
        self.root = Path(root)
        self.chunks: list[Chunk] = chunks or []
        self._prepare()

    def _prepare(self) -> None:
        self._tfs = [Counter(tokenize(c.path + " " + c.symbol + " " + c.text)) for c in self.chunks]
        self._lens = [sum(tf.values()) for tf in self._tfs]
        self._avg = (sum(self._lens) / len(self._lens)) if self._lens else 1.0
        df: Counter[str] = Counter()
        for tf in self._tfs:
            df.update(tf.keys())
        n = len(self.chunks)
        self._idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}

    @classmethod
    def build(cls, root: str | Path) -> CodeIndex:
        root = Path(root)
        chunks: list[Chunk] = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix not in EXTENSIONS:
                continue
            rel = p.relative_to(root)
            if any(part in SKIP_DIRS for part in rel.parts) or p.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            chunks.extend(chunk_file(rel.as_posix(), text))
        return cls(root, chunks)

    def save(self, path: str | Path | None = None) -> Path:
        out = Path(path) if path else self.root / ".agentic_ops" / "index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps([asdict(c) for c in self.chunks]))
        return out

    @classmethod
    def load_or_build(cls, root: str | Path) -> CodeIndex:
        cache = Path(root) / ".agentic_ops" / "index.json"
        if cache.is_file():
            return cls(root, [Chunk(**c) for c in json.loads(cache.read_text())])
        return cls.build(root)

    def search(self, query: str, k: int = 5, k1: float = 1.5, b: float = 0.75) -> list[tuple[float, Chunk]]:
        q = set(tokenize(query))
        scored = []
        for i, tf in enumerate(self._tfs):
            score = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    score += self._idf[t] * f * (k1 + 1) / (f + k1 * (1 - b + b * self._lens[i] / self._avg))
            if score > 0:
                scored.append((score, self.chunks[i]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:k]
