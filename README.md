# Rev Ops leadership job tracker

A daily scan for new **Head of / VP / SVP Revenue Operations** roles (plus adjacent GTM, sales and business ops leadership), scored for fit and collected in one tracker.

Every morning a GitHub Action:

1. Pulls postings from company career boards (Greenhouse, Lever, Ashby), remote job boards (Remotive, RemoteOK) and, if you add free API keys, the broader web via Google Jobs (which indexes LinkedIn, Indeed and company sites) and Adzuna.
2. Keeps only titles that are senior **and** Rev Ops-related, drops excluded titles, companies and stale postings.
3. Scores each role 0–100 on title, location, description keywords and freshness, and says why.
4. Dedupes the same role seen on several sites (preferring the direct company link).
5. Updates [`data/jobs.csv`](data/jobs.csv), writes [`reports/latest.md`](reports/latest.md) plus a dated copy, and optionally emails you the new ones.

## Daily workflow for you

- Open **`reports/latest.md`** on GitHub (or the email) to see what's new today.
- Track your pipeline in **`data/jobs.csv`**: edit the `status` (e.g. `applied`, `interviewing`, `not interested`, `rejected`) and `notes` columns. The tracker never overwrites those two columns, and roles marked `not interested`, `rejected` or `closed` drop off the report.

## Setup

1. **Turn on the workflow**: it runs from `.github/workflows/daily.yml` once this is on the default branch. Use *Actions → Daily Rev Ops job scan → Run workflow* to run it immediately.
2. **Tune `config.toml`**: your locations, keywords that signal a good fit, companies to skip, minimum score, and the list of target companies to watch.
3. **Add target companies**: find the board slug in a company's careers URL, e.g. `job-boards.greenhouse.io/`**`figma`**, `jobs.lever.co/`**`plaid`**, `jobs.ashbyhq.com/`**`ramp`**. The starter list is a guess; wrong slugs are listed under "Sources that failed" in the report.
4. **(Recommended) widen coverage**: add repository secrets under *Settings → Secrets and variables → Actions*:
   - `SERPAPI_KEY` for Google Jobs ([serpapi.com](https://serpapi.com), free tier = 250 searches/month; 4 queries/day ≈ 120/month)
   - `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` ([developer.adzuna.com](https://developer.adzuna.com), free)
5. **(Optional) email digest**: add `SMTP_HOST`, `SMTP_PORT` (default 587), `SMTP_USER`, `SMTP_PASSWORD` and `EMAIL_TO`. For Gmail use `smtp.gmail.com` and an [app password](https://myaccount.google.com/apppasswords).

## Running locally

Python 3.11+, no dependencies.

```sh
python -m jobtracker --no-email     # scan, update data/ and reports/
python -m unittest discover -s tests -t .
```

## Why not scrape LinkedIn / Indeed directly?

Their terms prohibit scraping and they actively block it, so a scraper would break often. Google Jobs (via SerpAPI) and Adzuna aggregate those listings through sanctioned APIs, and direct ATS boards are where the original postings live anyway.
