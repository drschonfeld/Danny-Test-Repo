"""Run every source, keep the matching roles, update the tracker CSV and write the daily report."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from . import notify, sources
from .matching import Match, Matcher

ROOT = Path(__file__).resolve().parent.parent
FIELDS = ["id", "first_seen", "last_seen", "score", "level", "title", "company", "location",
          "salary", "url", "source", "why", "status", "notes"]
# Columns you edit by hand in data/jobs.csv; the tracker never overwrites them.
USER_FIELDS = ("status", "notes")


def job_id(company: str, title: str) -> str:
    norm = lambda s: re.sub(r"\b(inc|llc|ltd|corp|co)\b|[^a-z0-9]", "", s.lower())
    return hashlib.sha1(f"{norm(company)}|{norm(title)}".encode()).hexdigest()[:12]


def build_tasks(config: dict) -> list[tuple[str, callable]]:
    tasks = []
    for ats in ("greenhouse", "lever", "ashby"):
        fetch = getattr(sources, ats)
        for slug in config.get("companies", {}).get(ats, []):
            tasks.append((f"{ats}:{slug}", lambda f=fetch, s=slug: f(s)))
    src, queries = config.get("sources", {}), config["search"].get("queries", [])
    if src.get("remoteok"):
        tasks.append(("remoteok", sources.remoteok))
    for q in queries:
        if src.get("remotive"):
            tasks.append((f"remotive:{q}", lambda q=q: sources.remotive(q)))
        if src.get("serpapi"):
            tasks.append((f"google-jobs:{q}", lambda q=q: sources.serpapi(q)))
        if src.get("adzuna"):
            country = src.get("adzuna_country", "us")
            tasks.append((f"adzuna:{q}", lambda q=q, c=country: sources.adzuna(q, c)))
    return tasks


def fetch_all(tasks) -> tuple[list[sources.Job], list[str]]:
    jobs, errors = [], []

    def run(task):
        name, fn = task
        try:
            return fn(), None
        except sources.SourceError as e:
            return [], f"{name}: {e}"
        except Exception as e:  # one broken source shouldn't sink the run
            return [], f"{name}: unexpected {type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=8) as pool:
        for found, err in pool.map(run, tasks):
            jobs.extend(found)
            if err:
                errors.append(err)
    return jobs, errors


def dedupe(matches: list[Match]) -> dict[str, Match]:
    """Same role often appears on several sources; keep the best-scoring, preferring direct ATS links."""
    best: dict[str, Match] = {}
    direct = ("greenhouse", "lever", "ashby")
    for m in matches:
        key = job_id(m.job.company, m.job.title)
        rank = (m.score, m.job.source.startswith(direct))
        cur = best.get(key)
        if cur is None or rank > (cur.score, cur.job.source.startswith(direct)):
            best[key] = m
    return best


def load_tracker(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def save_tracker(path: Path, rows: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(rows.values(), key=lambda r: (r["last_seen"], int(r["score"] or 0)), reverse=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)


def update_tracker(rows: dict[str, dict], found: dict[str, Match], today: str) -> list[str]:
    new_ids = []
    for key, m in found.items():
        j = m.job
        row = rows.get(key)
        if row is None:
            row = {"id": key, "first_seen": today, "status": "new", "notes": ""}
            rows[key] = row
            new_ids.append(key)
        row.update({
            "last_seen": today, "score": m.score, "level": m.level, "title": j.title,
            "company": pretty_company(j.company), "location": j.location, "salary": j.salary,
            "url": j.url, "source": j.source, "why": "; ".join(m.reasons),
        })
    return new_ids


def pretty_company(name: str) -> str:
    # Lever/Ashby only give us the board slug.
    return name.replace("-", " ").title() if name.islower() else name


def render_report(rows: dict[str, dict], new_ids: list[str], errors: list[str], today: str, scanned: int) -> str:
    active = [r for r in rows.values() if r["last_seen"] == today
              and r.get("status", "new") not in ("rejected", "not interested", "closed")]
    active.sort(key=lambda r: int(r["score"]), reverse=True)
    new = [rows[i] for i in new_ids]
    new.sort(key=lambda r: int(r["score"]), reverse=True)

    def table(items):
        if not items:
            return "_None._\n"
        out = ["| Score | Level | Role | Company | Location | Salary | Why |", "|---|---|---|---|---|---|---|"]
        for r in items:
            cell = lambda s: str(s or "").replace("|", "/").replace("\n", " ")
            out.append(f"| {r['score']} | {r['level']} | [{cell(r['title'])}]({r['url']}) | {cell(r['company'])} "
                       f"| {cell(r['location'])} | {cell(r['salary'])} | {cell(r['why'])} |")
        return "\n".join(out) + "\n"

    parts = [
        f"# Rev Ops leadership roles: {today}\n",
        f"Scanned {scanned} postings. **{len(new)} new** matches, {len(active)} open matches in total.\n",
        "## New today\n", table(new),
        "## All open matches\n", table(active),
    ]
    if errors:
        parts += ["## Sources that failed\n",
                  "Unknown board slugs show up as HTTP 404; fix or remove them in `config.toml`.\n",
                  "\n".join(f"- {e}" for e in sorted(errors)) + "\n"]
    return "\n".join(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default=ROOT / "config.toml", type=Path)
    ap.add_argument("--data-dir", default=ROOT / "data", type=Path)
    ap.add_argument("--reports-dir", default=ROOT / "reports", type=Path)
    ap.add_argument("--no-email", action="store_true")
    args = ap.parse_args(argv)

    config = tomllib.loads(args.config.read_text())
    today = date.today().isoformat()
    jobs, errors = fetch_all(build_tasks(config))

    matcher = Matcher(config)
    now = datetime.now(timezone.utc)
    found = dedupe([m for j in jobs if (m := matcher.match(j, now))])

    tracker_path = args.data_dir / "jobs.csv"
    rows = load_tracker(tracker_path)
    new_ids = update_tracker(rows, found, today)
    save_tracker(tracker_path, rows)

    report = render_report(rows, new_ids, errors, today, len(jobs))
    args.reports_dir.mkdir(parents=True, exist_ok=True)
    (args.reports_dir / f"{today}.md").write_text(report)
    (args.reports_dir / "latest.md").write_text(report)

    print(f"{len(jobs)} postings scanned, {len(found)} matches, {len(new_ids)} new, {len(errors)} source errors")
    for e in errors:
        print(f"  warn: {e}", file=sys.stderr)

    if new_ids and not args.no_email:
        notify.send_email([rows[i] for i in new_ids], today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
