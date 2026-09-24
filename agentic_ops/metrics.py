"""Developer-productivity metrics from the GitHub API: cycle time, review latency/load, bot acceptance."""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from .github_client import GitHubClient


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(int(q * len(ordered)), len(ordered) - 1)], 1)


def collect(gh: GitHubClient, owner: str, repo: str, days: int = 30, bot_login: str = "agentic-ops[bot]") -> dict:
    since = datetime.now(UTC) - timedelta(days=days)
    pulls = gh.paginate(f"/repos/{owner}/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"})
    merged = [p for p in pulls if p.get("merged_at") and _ts(p["merged_at"]) >= since]

    cycle_hours, first_review_hours, sizes = [], [], []
    review_load: Counter[str] = Counter()
    for p in merged:
        created, merged_at = _ts(p["created_at"]), _ts(p["merged_at"])
        cycle_hours.append((merged_at - created).total_seconds() / 3600)
        reviews = gh.paginate(f"/repos/{owner}/{repo}/pulls/{p['number']}/reviews", max_pages=3)
        humans = [
            r
            for r in reviews
            if r.get("user") and r["user"]["login"] not in (p["user"]["login"], bot_login) and r.get("submitted_at")
        ]
        for r in humans:
            review_load[r["user"]["login"]] += 1
        if humans:
            first = min(_ts(r["submitted_at"]) for r in humans)
            first_review_hours.append((first - created).total_seconds() / 3600)
        detail = gh.get_pr(owner, repo, p["number"])
        sizes.append(detail.get("additions", 0) + detail.get("deletions", 0))

    comments = gh.paginate(f"/repos/{owner}/{repo}/pulls/comments", {"since": since.isoformat()})
    bot_comments = [c for c in comments if c.get("user", {}).get("login") == bot_login]
    up = sum(c.get("reactions", {}).get("+1", 0) for c in bot_comments)
    down = sum(c.get("reactions", {}).get("-1", 0) for c in bot_comments)

    return {
        "repo": f"{owner}/{repo}",
        "window_days": days,
        "merged_prs": len(merged),
        "cycle_time_hours": {"p50": _pct(cycle_hours, 0.5), "p90": _pct(cycle_hours, 0.9)},
        "time_to_first_review_hours": {"p50": _pct(first_review_hours, 0.5), "p90": _pct(first_review_hours, 0.9)},
        "pr_size_lines": {"p50": _pct(sizes, 0.5), "mean": round(statistics.mean(sizes), 1) if sizes else None},
        "review_load": dict(review_load.most_common(15)),
        "bot": {
            "comments": len(bot_comments),
            "thumbs_up": up,
            "thumbs_down": down,
            "acceptance_rate": round(up / (up + down), 2) if (up + down) else None,
        },
    }


def to_markdown(m: dict) -> str:
    rows = [
        ("Merged PRs", m["merged_prs"]),
        ("Cycle time p50 / p90 (h)", f"{m['cycle_time_hours']['p50']} / {m['cycle_time_hours']['p90']}"),
        (
            "First review p50 / p90 (h)",
            f"{m['time_to_first_review_hours']['p50']} / {m['time_to_first_review_hours']['p90']}",
        ),
        ("PR size p50 (lines)", m["pr_size_lines"]["p50"]),
        ("Bot comments", m["bot"]["comments"]),
        ("Bot acceptance (👍 / 👍+👎)", m["bot"]["acceptance_rate"]),
    ]
    out = [
        f"## Engineering metrics: {m['repo']} (last {m['window_days']} days)",
        "",
        "| Metric | Value |",
        "| --- | --- |",
    ]
    out += [f"| {k} | {v} |" for k, v in rows]
    if m["review_load"]:
        out += ["", "| Reviewer | Reviews |", "| --- | --- |"] + [f"| {k} | {v} |" for k, v in m["review_load"].items()]
    return "\n".join(out)
