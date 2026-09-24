"""Risk-based policy: map changed paths to risk tiers and decide whether the gate blocks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .models import Finding, GateResult


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob with ** support: '**/' = any dirs (including none), '*' = within one path segment."""
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


@dataclass
class Tier:
    name: str
    paths: list[str] = field(default_factory=list)
    block_on: list[str] = field(default_factory=lambda: ["error"])
    require_human_review: bool = False
    auto_approve: bool = False


DEFAULT_POLICY = {
    "tiers": [
        {
            "name": "high",
            "paths": ["**/auth/**", "**/crypto/**", "**/*secret*", ".github/workflows/**", "**/migrations/**"],
            "block_on": ["error", "warning"],
            "require_human_review": True,
        },
        {"name": "low", "paths": ["docs/**", "**/*.md"], "block_on": [], "auto_approve": True},
    ],
    "default": {"name": "medium", "block_on": ["error"]},
}


class Policy:
    """Tiers are listed highest-risk first; the first matching tier wins for a file."""

    def __init__(self, tiers: list[Tier], default: Tier):
        self.tiers = tiers
        self.default = default

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        tiers = [Tier(**t) for t in data.get("tiers", [])]
        default = Tier(**data.get("default", {"name": "medium"}))
        return cls(tiers, default)

    @classmethod
    def load(cls, path: str | Path | None) -> Policy:
        if path and Path(path).is_file():
            return cls.from_dict(yaml.safe_load(Path(path).read_text()) or {})
        return cls.from_dict(DEFAULT_POLICY)

    def tier_for(self, path: str) -> Tier:
        for tier in self.tiers:
            if path_matches(path, tier.paths):
                return tier
        return self.default

    def _rank(self, tier: Tier) -> int:
        """Lower = riskier. Listed tiers keep their order, except auto-approve tiers rank below default."""
        names = [t.name for t in self.tiers]
        if tier.name not in names:
            return len(names)
        idx = names.index(tier.name)
        return len(names) + 1 + idx if tier.auto_approve else idx

    def evaluate(self, changed_paths: list[str], findings: list[Finding], tools_run: list[str]) -> GateResult:
        file_tiers = {p: self.tier_for(p) for p in changed_paths}
        pr_tier = min(file_tiers.values(), key=self._rank) if file_tiers else self.default
        reasons: list[str] = []
        for f in findings:
            tier = (file_tiers.get(f.path) or self.tier_for(f.path)) if f.path else self.default
            if f.severity in tier.block_on:
                reasons.append(f"[{tier.name}] {f.tool}:{f.rule} {f.path}:{f.line} {f.message}")
        require_human = any(t.require_human_review for t in file_tiers.values())
        auto_approve = bool(file_tiers) and all(t.auto_approve for t in file_tiers.values()) and not reasons
        if require_human:
            high = sorted(p for p, t in file_tiers.items() if t.require_human_review)
            reasons_note = f"Human review required for high-risk paths: {', '.join(high[:10])}"
        else:
            reasons_note = ""
        return GateResult(
            tier=pr_tier.name,
            findings=findings,
            blocked=bool(reasons),
            reasons=reasons + ([reasons_note] if reasons_note else []),
            require_human_review=require_human,
            auto_approve=auto_approve,
            tools_run=tools_run,
        )
