"""Fetchers for job sources. Each returns a list of Job records; failures raise SourceError."""

from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

USER_AGENT = "Mozilla/5.0 (compatible; revops-job-tracker/1.0)"
TIMEOUT = 30


class SourceError(Exception):
    pass


@dataclass
class Job:
    title: str
    company: str
    location: str
    url: str
    source: str
    posted: datetime | None = None
    description: str = ""
    salary: str = ""
    remote: bool = False
    extra: dict = field(default_factory=dict)


def get_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SourceError(f"HTTP {e.code} for {url.split('?')[0]}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise SourceError(f"{type(e).__name__} for {url.split('?')[0]}: {e}") from e


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    # Greenhouse double-escapes its HTML content.
    text = html.unescape(html.unescape(text))
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", text)).strip()


def parse_date(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        # Lever uses epoch milliseconds, RemoteOK epoch seconds.
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _looks_remote(*parts: str) -> bool:
    return any(re.search(r"\b(remote|anywhere|distributed)\b", p or "", re.I) for p in parts)


# --- Company career boards (ATS APIs) -------------------------------------------------------

def greenhouse(slug: str) -> list[Job]:
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    company = data.get("meta", {}).get("name") or slug
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            title=j.get("title", ""),
            company=j.get("company_name") or company,
            location=loc,
            url=j.get("absolute_url", ""),
            source=f"greenhouse:{slug}",
            posted=parse_date(j.get("first_published") or j.get("updated_at")),
            description=strip_html(j.get("content")),
            remote=_looks_remote(loc),
        ))
    return jobs


def lever(slug: str) -> list[Job]:
    data = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or [])
        salary = ""
        if rng := j.get("salaryRange"):
            salary = f"{rng.get('currency', '')} {rng.get('min', '')}-{rng.get('max', '')}".strip()
        jobs.append(Job(
            title=j.get("text", ""),
            company=slug,
            location=loc,
            url=j.get("hostedUrl", ""),
            source=f"lever:{slug}",
            posted=parse_date(j.get("createdAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("description")),
            salary=salary,
            remote=j.get("workplaceType") == "remote" or _looks_remote(loc),
        ))
    return jobs


def ashby(slug: str) -> list[Job]:
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location", "")
        jobs.append(Job(
            title=j.get("title", ""),
            company=slug,
            location=loc,
            url=j.get("jobUrl", ""),
            source=f"ashby:{slug}",
            posted=parse_date(j.get("publishedAt")),
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            salary=(j.get("compensation") or {}).get("compensationTierSummary") or "",
            remote=bool(j.get("isRemote")) or _looks_remote(loc),
        ))
    return jobs


# --- Aggregators --------------------------------------------------------------------------

def remotive(query: str) -> list[Job]:
    q = urllib.parse.quote(query)
    data = get_json(f"https://remotive.com/api/remote-jobs?search={q}&limit=100")
    return [Job(
        title=j.get("title", ""),
        company=j.get("company_name", ""),
        location=j.get("candidate_required_location") or "Remote",
        url=j.get("url", ""),
        source="remotive",
        posted=parse_date(j.get("publication_date")),
        description=strip_html(j.get("description")),
        salary=j.get("salary") or "",
        remote=True,
    ) for j in data.get("jobs", [])]


def remoteok() -> list[Job]:
    data = get_json("https://remoteok.com/api")
    return [Job(
        title=j.get("position", ""),
        company=j.get("company", ""),
        location=j.get("location") or "Remote",
        url=j.get("url", ""),
        source="remoteok",
        posted=parse_date(j.get("epoch") or j.get("date")),
        description=strip_html(j.get("description")),
        salary=f"${j['salary_min']:,}-${j['salary_max']:,}" if j.get("salary_min") and j.get("salary_max") else "",
        remote=True,
    ) for j in data if isinstance(j, dict) and j.get("position")]  # first element is a legal notice


def serpapi(query: str) -> list[Job]:
    key = os.environ.get("SERPAPI_KEY")
    if not key:
        return []
    params = urllib.parse.urlencode({"engine": "google_jobs", "q": query, "hl": "en", "api_key": key})
    data = get_json(f"https://serpapi.com/search.json?{params}")
    if "error" in data and not data.get("jobs_results"):
        if "hasn't returned any results" in data["error"]:
            return []
        raise SourceError(f"SerpAPI: {data['error']}")
    jobs = []
    for j in data.get("jobs_results", []):
        ext = j.get("detected_extensions") or {}
        apply = (j.get("apply_options") or [{}])[0].get("link")
        loc = j.get("location", "")
        jobs.append(Job(
            title=j.get("title", ""),
            company=j.get("company_name", ""),
            location=loc,
            url=apply or j.get("share_link", ""),
            source=f"google-jobs ({j.get('via', '').removeprefix('via ')})".replace(" ()", ""),
            posted=_relative_date(ext.get("posted_at")),
            description=j.get("description", ""),
            salary=ext.get("salary", ""),
            remote=bool(ext.get("work_from_home")) or _looks_remote(loc),
        ))
    return jobs


def adzuna(query: str, country: str = "us") -> list[Job]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return []
    params = urllib.parse.urlencode({
        "app_id": app_id, "app_key": app_key, "what_phrase": query,
        "results_per_page": 50, "max_days_old": 30, "content-type": "application/json",
    })
    data = get_json(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{params}")
    jobs = []
    for j in data.get("results", []):
        loc = (j.get("location") or {}).get("display_name", "")
        lo, hi = j.get("salary_min"), j.get("salary_max")
        jobs.append(Job(
            title=strip_html(j.get("title")),
            company=(j.get("company") or {}).get("display_name", ""),
            location=loc,
            url=j.get("redirect_url", ""),
            source="adzuna",
            posted=parse_date(j.get("created")),
            description=strip_html(j.get("description")),
            salary=f"${lo:,.0f}-${hi:,.0f}" if lo and hi else "",
            remote=_looks_remote(loc, j.get("title", "")),
        ))
    return jobs


def _relative_date(text: str | None) -> datetime | None:
    """Turn Google Jobs' '3 days ago' into a datetime."""
    if not text:
        return None
    m = re.match(r"(\d+)\+?\s*(hour|day|week|month)", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"hour": timedelta(hours=n), "day": timedelta(days=n),
             "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
    return datetime.now(timezone.utc) - delta
