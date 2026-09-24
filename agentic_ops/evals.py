"""Offline eval harness for the reviewer: golden PRs with known issues -> precision / recall.

Case file format (JSON):
  {"name": "...", "diff": "<unified diff>", "expected": [{"path": "...", "line": 12, "category": "security"}]}
An expected issue counts as found when a comment lands on the same path within +/- `tolerance` lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .diff import parse_diff
from .llm import LLMClient
from .models import ReviewComment
from .reviewer import review_diff


@dataclass
class CaseResult:
    name: str
    true_positives: int
    comments: int
    expected: int
    misses: list[dict] = field(default_factory=list)
    false_positives: list[str] = field(default_factory=list)


def match(comments: list[ReviewComment], expected: list[dict], tolerance: int = 3) -> tuple[int, list[dict], list[str]]:
    used: set[int] = set()
    tp, misses = 0, []
    for exp in expected:
        hit = next(
            (
                i
                for i, c in enumerate(comments)
                if i not in used and c.path == exp["path"] and abs(c.line - exp["line"]) <= tolerance
            ),
            None,
        )
        if hit is None:
            misses.append(exp)
        else:
            used.add(hit)
            tp += 1
    fps = [f"{c.path}:{c.line} {c.body[:80]}" for i, c in enumerate(comments) if i not in used]
    return tp, misses, fps


def run_evals(llm: LLMClient, cases_dir: str | Path, min_severity: str = "minor") -> dict:
    results: list[CaseResult] = []
    for case_file in sorted(Path(cases_dir).glob("*.json")):
        case = json.loads(case_file.read_text())
        review = review_diff(llm, parse_diff(case["diff"]), min_severity=min_severity)
        tp, misses, fps = match(review.comments, case["expected"])
        results.append(CaseResult(case["name"], tp, len(review.comments), len(case["expected"]), misses, fps))
    tp = sum(r.true_positives for r in results)
    total_comments = sum(r.comments for r in results)
    total_expected = sum(r.expected for r in results)
    return {
        "cases": len(results),
        "precision": round(tp / total_comments, 3) if total_comments else 1.0,
        "recall": round(tp / total_expected, 3) if total_expected else 1.0,
        "details": [r.__dict__ for r in results],
    }
