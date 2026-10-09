import csv
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from jobtracker import sources, tracker
from jobtracker.matching import Matcher
from jobtracker.sources import Job

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / "config.toml").read_text())
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)


def job(title, location="Remote", desc="", days_old=1, company="Acme"):
    return Job(title=title, company=company, location=location, url="https://x", source="test",
               posted=NOW - timedelta(days=days_old), description=desc, remote="remote" in location.lower())


class TitleMatching(unittest.TestCase):
    def setUp(self):
        self.m = Matcher({**CONFIG, "fit": {**CONFIG["fit"], "min_score": 0}})

    def test_senior_revops_titles_match(self):
        for t in ["VP, Revenue Operations", "Head of RevOps", "SVP Rev Ops", "Vice President of GTM Operations",
                  "Senior Vice President, Revenue Strategy & Operations", "VP Sales Operations",
                  "Head of Go-To-Market Operations", "EVP Revenue Operations"]:
            self.assertIsNotNone(self.m.match(job(t), NOW), t)

    def test_non_leadership_or_off_function_rejected(self):
        for t in ["Revenue Operations Manager", "Senior Director, Revenue Operations", "VP Engineering",
                  "Head of Revenue Cycle Operations", "Revenue Operations Intern", "Head of Marketing",
                  "Executive Assistant to VP Revenue Operations"]:
            self.assertIsNone(self.m.match(job(t), NOW), t)

    def test_scoring_prefers_core_remote_and_relevant(self):
        strong = self.m.match(job("SVP Revenue Operations", desc="B2B SaaS, Salesforce, forecast, CPQ, deal desk"), NOW)
        weak = self.m.match(job("Head of Business Operations", location="Austin, TX", days_old=20), NOW)
        self.assertGreater(strong.score, weak.score)
        self.assertEqual(strong.level, "SVP")
        self.assertIn("core RevOps title", strong.reasons)

    def test_stale_postings_dropped(self):
        self.assertIsNone(self.m.match(job("VP Revenue Operations", days_old=90), NOW))

    def test_min_score(self):
        m = Matcher(CONFIG)  # min_score 40
        self.assertIsNone(m.match(job("Head of Business Operations", location="Austin, TX", days_old=20), NOW))


FAKE = {
    "boards-api.greenhouse.io/v1/boards/acme": {"meta": {"name": "Acme"}, "jobs": [
        {"title": "VP, Revenue Operations", "absolute_url": "https://gh/acme/1", "location": {"name": "Remote - US"},
         "first_published": "2026-10-08T12:00:00Z", "content": "&lt;p&gt;B2B SaaS. Salesforce.&lt;/p&gt;"},
        {"title": "Software Engineer", "absolute_url": "https://gh/acme/2", "location": {"name": "Remote"},
         "updated_at": "2026-10-08T12:00:00-04:00", "content": ""},
    ]},
    "api.lever.co/v0/postings/beta": [
        {"text": "Head of Revenue Operations", "hostedUrl": "https://lever/beta/1", "createdAt": 1791460800000,
         "categories": {"location": "New York, NY"}, "descriptionPlain": "Own forecast and territory planning",
         "workplaceType": "hybrid", "salaryRange": {"currency": "USD", "min": 220000, "max": 260000}},
    ],
    "api.ashbyhq.com/posting-api/job-board/gamma": {"jobs": [
        {"title": "Senior Vice President, GTM Operations", "jobUrl": "https://ashby/gamma/1", "location": "San Francisco",
         "isRemote": True, "publishedAt": "2026-10-07T00:00:00.000+00:00", "descriptionPlain": "Series C",
         "compensation": {"compensationTierSummary": "$300K - $350K"}},
    ]},
    "remotive.com/api/remote-jobs": {"jobs": [
        # Same role as the Greenhouse one: should be deduped in favour of the direct ATS link.
        {"title": "VP, Revenue Operations", "company_name": "Acme Inc", "url": "https://remotive/1",
         "candidate_required_location": "USA", "publication_date": "2026-10-08T10:00:00", "description": "SaaS"},
    ]},
    "remoteok.com/api": [{"legal": "notice"}, {"position": "VP Marketing", "company": "Delta", "epoch": 1791460800}],
    "boards-api.greenhouse.io/v1/boards/missing": sources.SourceError("HTTP 404 for missing"),
}


def fake_get_json(url, headers=None):
    for prefix, value in FAKE.items():
        if prefix in url:
            if isinstance(value, Exception):
                raise value
            return value
    raise AssertionError(f"unexpected url {url}")


class EndToEnd(unittest.TestCase):
    def run_tracker(self, tmp, today):
        cfg = Path(tmp) / "config.toml"
        text = (ROOT / "config.toml").read_text().split("[companies]")[0] + """
[companies]
greenhouse = ["acme", "missing"]
lever = ["beta"]
ashby = ["gamma"]
[sources]
remotive = true
remoteok = true
serpapi = true
adzuna = true
"""
        cfg.write_text(text)
        with mock.patch.object(sources, "get_json", fake_get_json), \
             mock.patch.object(tracker, "date") as d, \
             mock.patch.object(tracker, "datetime") as dt, \
             mock.patch.dict("os.environ", {}, clear=True):
            d.today.return_value.isoformat.return_value = today
            dt.now.return_value = NOW
            tracker.main(["--config", str(cfg), "--data-dir", f"{tmp}/data", "--reports-dir", f"{tmp}/reports"])

    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_tracker(tmp, "2026-10-09")
            with open(f"{tmp}/data/jobs.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(sorted(r["title"] for r in rows),
                             ["Head of Revenue Operations", "Senior Vice President, GTM Operations", "VP, Revenue Operations"])
            acme = next(r for r in rows if r["company"] == "Acme")
            self.assertEqual(acme["url"], "https://gh/acme/1")
            self.assertEqual(next(r for r in rows if r["company"] == "Beta")["salary"], "USD 220000-260000")
            report = Path(f"{tmp}/reports/latest.md").read_text()
            self.assertIn("**3 new** matches", report)
            self.assertIn("greenhouse:missing: HTTP 404", report)

            # Second day: user marks one as applied; nothing is new and their edits survive.
            for r in rows:
                if r["company"] == "Acme":
                    r["status"], r["notes"] = "applied", "referral from Sam"
            with open(f"{tmp}/data/jobs.csv", "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=tracker.FIELDS)
                w.writeheader()
                w.writerows(rows)
            self.run_tracker(tmp, "2026-10-10")
            with open(f"{tmp}/data/jobs.csv") as f:
                acme = next(r for r in csv.DictReader(f) if r["company"] == "Acme")
            self.assertEqual((acme["status"], acme["notes"], acme["first_seen"], acme["last_seen"]),
                             ("applied", "referral from Sam", "2026-10-09", "2026-10-10"))
            self.assertIn("**0 new** matches", Path(f"{tmp}/reports/latest.md").read_text())


class Helpers(unittest.TestCase):
    def test_job_id_normalises_company(self):
        self.assertEqual(tracker.job_id("Acme Inc.", "VP, Revenue Operations"),
                         tracker.job_id("acme", "VP Revenue Operations"))

    def test_strip_html_double_escaped(self):
        self.assertEqual(sources.strip_html("&amp;lt;p&amp;gt;Hi &amp;amp; bye&amp;lt;/p&amp;gt;"), "Hi & bye")

    def test_relative_date(self):
        self.assertIsNotNone(sources._relative_date("3 days ago"))
        self.assertIsNone(sources._relative_date("yesterday-ish"))


if __name__ == "__main__":
    unittest.main()
