"""Shared data models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GateSeverity = Literal["info", "warning", "error"]
ReviewSeverity = Literal["nit", "minor", "major", "critical"]
Category = Literal["bug", "security", "performance", "maintainability", "test", "style"]

REVIEW_SEVERITY_RANK = {"nit": 0, "minor": 1, "major": 2, "critical": 3}


class Finding(BaseModel):
    """A deterministic finding from a scanner (Ruff, Semgrep, ESLint, CodeQL SARIF, SonarQube)."""

    tool: str
    rule: str
    path: str
    line: int = 0
    severity: GateSeverity = "warning"
    message: str


class ReviewComment(BaseModel):
    path: str
    line: int = Field(ge=1)
    severity: ReviewSeverity
    category: Category
    body: str
    suggestion: str | None = None


class ReviewResult(BaseModel):
    summary: str
    comments: list[ReviewComment] = Field(default_factory=list)


class GateResult(BaseModel):
    tier: str
    findings: list[Finding] = Field(default_factory=list)
    blocked: bool = False
    reasons: list[str] = Field(default_factory=list)
    require_human_review: bool = False
    auto_approve: bool = False
    tools_run: list[str] = Field(default_factory=list)


class GeneratedTests(BaseModel):
    code: str
    notes: str = ""
