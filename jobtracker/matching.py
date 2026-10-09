"""Decide whether a posting is a senior Rev Ops leadership role, and score how well it fits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .sources import Job


@dataclass
class Match:
    job: Job
    score: int
    level: str
    reasons: list[str]


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


def _first(patterns: list[re.Pattern], text: str) -> re.Pattern | None:
    return next((p for p in patterns if p.search(text)), None)


class Matcher:
    def __init__(self, config: dict):
        s, f = config["search"], config["fit"]
        self.seniority = _compile(s["seniority"])
        self.primary = _compile(s["function_primary"])
        self.adjacent = _compile(s["function_adjacent"])
        self.exclude = _compile(s.get("exclude_titles", []))
        self.max_age_days = s.get("max_age_days")
        self.locations = [loc.lower() for loc in f.get("locations", [])]
        self.boost = [k.lower() for k in f.get("boost_keywords", [])]
        self.penalty = [k.lower() for k in f.get("penalty_keywords", [])]
        self.exclude_companies = {c.lower() for c in f.get("exclude_companies", [])}
        self.min_score = f.get("min_score", 0)

    def match(self, job: Job, now: datetime | None = None) -> Match | None:
        title = job.title
        if not _first(self.seniority, title) or _first(self.exclude, title):
            return None
        is_primary = bool(_first(self.primary, title))
        if not is_primary and not _first(self.adjacent, title):
            return None
        if job.company.lower() in self.exclude_companies:
            return None
        now = now or datetime.now(timezone.utc)
        if self.max_age_days and job.posted and (now - job.posted).days > self.max_age_days:
            return None

        reasons = []
        level = seniority_level(title)
        # Title: up to 50 points. SVP/Chief > VP > Head of; core RevOps > adjacent ops.
        score = {"SVP": 25, "VP": 22, "Head of": 18}.get(level, 15)
        if is_primary:
            score += 25
            reasons.append("core RevOps title")
        else:
            score += 12
            reasons.append("adjacent ops title")

        # Location: up to 25 points.
        loc = f"{job.location} {'remote' if job.remote else ''}".lower()
        hits = [l for l in self.locations if (l == "remote" and job.remote) or l in loc]
        if hits:
            score += 25
            reasons.append(f"location: {hits[0]}")
        elif not self.locations:
            score += 25
        else:
            reasons.append(f"location mismatch ({job.location or 'unknown'})")

        # Description keywords: up to 20 points.
        desc = job.description.lower()
        boosts = [k for k in self.boost if k in desc]
        if boosts:
            score += min(20, 4 * len(boosts))
            reasons.append("mentions " + ", ".join(boosts[:5]))
        penalties = [k for k in self.penalty if k in desc]
        if penalties:
            score -= 15 * len(penalties)
            reasons.append("flags: " + ", ".join(penalties))

        # Freshness: up to 5 points.
        if job.posted and (now - job.posted).days <= 7:
            score += 5
            reasons.append("posted this week")

        score = max(0, min(100, score))
        if score < self.min_score:
            return None
        return Match(job, score, level, reasons)


def seniority_level(title: str) -> str:
    if re.search(r"\b(svp|evp|senior vice[- ]president|executive vice[- ]president|chief)\b", title, re.I):
        return "SVP"
    if re.search(r"\b(vp|vice[- ]president)\b", title, re.I):
        return "VP"
    if re.search(r"\bhead of\b", title, re.I):
        return "Head of"
    return "Other"
