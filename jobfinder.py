#!/usr/bin/env python3
"""Job finder: scans configured sites, filters postings, emails NEW matches.

Usage:
    python jobfinder.py --dry-run     # print matches, send nothing, remember nothing
    python jobfinder.py --baseline    # mark everything currently listed as seen (no email)
    python jobfinder.py               # normal run: email new matches, remember them
"""
import argparse
import hashlib
import html
import json
import os
import smtplib
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (personal-jobfinder)"}
TIMEOUT = 25


@dataclass
class Job:
    source: str
    title: str
    url: str
    company: str = ""
    location: str = ""
    job_type: str = ""        # full-time | internship | part-time | contract | ""
    description: str = ""
    posted: datetime | None = None

    @property
    def uid(self) -> str:
        return hashlib.sha1(self.url.encode()).hexdigest()


# ---------------------------------------------------------------- helpers
def clean_html(raw: str) -> str:
    return BeautifulSoup(html.unescape(raw or ""), "html.parser").get_text(" ", strip=True)


def parse_date(value) -> datetime | None:
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):  # epoch millis (Lever)
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def infer_type(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if "intern" in blob:
        return "internship"
    if "part-time" in blob or "part time" in blob or "parttime" in blob:
        return "part-time"
    if "contract" in blob or "temporary" in blob:
        return "contract"
    if "full-time" in blob or "full time" in blob or "fulltime" in blob:
        return "full-time"
    return ""


def get(url: str, **kw):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- fetchers
def fetch_greenhouse(src):
    data = get(f"https://boards-api.greenhouse.io/v1/boards/{src['board']}/jobs",
               params={"content": "true"}).json()
    for j in data.get("jobs", []):
        desc = clean_html(j.get("content", ""))
        yield Job(src["name"], j["title"], j["absolute_url"], src.get("company", src["board"]),
                  (j.get("location") or {}).get("name", ""),
                  infer_type(j["title"], desc[:500]), desc, parse_date(j.get("updated_at")))


def fetch_lever(src):
    data = get(f"https://api.lever.co/v0/postings/{src['company']}", params={"mode": "json"}).json()
    for j in data:
        cat = j.get("categories") or {}
        yield Job(src["name"], j["text"], j["hostedUrl"], src["company"],
                  cat.get("location", ""),
                  infer_type(cat.get("commitment", ""), j["text"]),
                  j.get("descriptionPlain", ""), parse_date(j.get("createdAt")))


def fetch_ashby(src):
    data = get(f"https://api.ashbyhq.com/posting-api/job-board/{src['board']}").json()
    mapping = {"fulltime": "full-time", "parttime": "part-time",
               "intern": "internship", "contract": "contract"}
    for j in data.get("jobs", []):
        et = (j.get("employmentType") or "").replace("_", "").lower()
        yield Job(src["name"], j["title"], j["jobUrl"], src.get("company", src["board"]),
                  j.get("location", ""),
                  mapping.get(et) or infer_type(j["title"]),
                  j.get("descriptionPlain", ""), parse_date(j.get("publishedAt")))


def fetch_rss(src):
    feed = feedparser.parse(src["url"], request_headers=HEADERS)
    for e in feed.entries:
        desc = clean_html(e.get("summary", ""))
        posted = None
        if e.get("published_parsed"):
            posted = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        yield Job(src["name"], e.get("title", ""), e.get("link", ""), src.get("company", ""),
                  e.get("location", ""), infer_type(e.get("title", ""), desc[:500]), desc, posted)


def fetch_html(src):
    """Generic scraper driven by CSS selectors in config.
    Needs: url, item (selector for each job card), title, link.
    Optional: location, description (selectors relative to the card)."""
    soup = BeautifulSoup(get(src["url"]).text, "html.parser")

    def pick(card, sel, attr=None):
        if not sel:
            return ""
        el = card.select_one(sel)
        if not el:
            return ""
        return el.get(attr, "") if attr else el.get_text(" ", strip=True)

    for card in soup.select(src["item"]):
        title = pick(card, src["title"])
        link = card.get("href") if card.name == "a" and src["link"] == "self" else pick(card, src["link"], "href")
        if not title or not link:
            continue
        desc = pick(card, src.get("description"))
        yield Job(src["name"], title, urljoin(src["url"], link), src.get("company", ""),
                  pick(card, src.get("location")), infer_type(title, desc[:500]), desc)


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "rss": fetch_rss, "html": fetch_html}


# ---------------------------------------------------------------- filtering
def matches(job: Job, f: dict) -> bool:
    title = job.title.lower()
    text = f"{job.title} {job.description}".lower()
    loc = f"{job.location}".lower()

    if any(k.lower() in title for k in f.get("exclude_title_keywords", [])):
        return False
    inc = [k.lower() for k in f.get("include_keywords", [])]
    if inc and not any(k in text for k in inc):
        return False
    locs = [l.lower() for l in f.get("locations", [])]
    # jobs with unknown location are kept; the email shows it blank
    if locs and loc and not any(l in loc for l in locs):
        return False
    types = [t.lower() for t in f.get("job_types", [])]
    if types and job.job_type and job.job_type not in types:
        return False
    max_age = f.get("max_age_days")
    if max_age and job.posted and job.posted < datetime.now(timezone.utc) - timedelta(days=max_age):
        return False
    return True


# ---------------------------------------------------------------- storage
def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS seen (uid TEXT PRIMARY KEY, title TEXT, url TEXT, first_seen TEXT)")
    return db


def is_seen(db, job: Job) -> bool:
    return db.execute("SELECT 1 FROM seen WHERE uid=?", (job.uid,)).fetchone() is not None


def mark_seen(db, jobs):
    now = datetime.now(timezone.utc).isoformat()
    db.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                   [(j.uid, j.title, j.url, now) for j in jobs])
    db.commit()


# ---------------------------------------------------------------- email
def build_email(jobs, cfg):
    max_n = cfg["email"].get("max_jobs_per_email", 50)
    shown = jobs[:max_n]
    rows, plain = [], []
    for j in shown:
        meta = " · ".join(x for x in [j.company, j.location, j.job_type, j.source] if x)
        snippet = html.escape(j.description[:240]) + ("…" if len(j.description) > 240 else "")
        rows.append(f'<p><a href="{html.escape(j.url)}"><b>{html.escape(j.title)}</b></a><br>'
                    f'<small>{html.escape(meta)}</small><br>{snippet}</p>')
        plain.append(f"{j.title}\n  {meta}\n  {j.url}\n")
    extra = f"\n(+{len(jobs) - max_n} more not shown)" if len(jobs) > max_n else ""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"{len(jobs)} new job match{'es' if len(jobs) != 1 else ''}"
    msg["From"] = os.environ[cfg["email"]["username_env"]]
    msg["To"] = email_to(cfg)
    msg.attach(MIMEText("\n".join(plain) + extra, "plain"))
    msg.attach(MIMEText("<html><body>" + "".join(rows) + html.escape(extra) + "</body></html>", "html"))
    return msg


def email_to(cfg) -> str:
    # EMAIL_TO env var (a GitHub secret) wins, so the address needn't live in a public repo
    return os.environ.get("EMAIL_TO") or cfg["email"].get("to", "")


def email_ready(cfg) -> bool:
    e = cfg.get("email", {})
    return bool(e.get("enabled", True)
                and os.environ.get(e.get("username_env", ""))
                and os.environ.get(e.get("password_env", ""))
                and email_to(cfg))


def export_json(db, jobs, path):
    """Write all currently-matching jobs for the website (newest first)."""
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for j in jobs:
        r = db.execute("SELECT first_seen FROM seen WHERE uid=?", (j.uid,)).fetchone()
        rows.append({
            "title": j.title, "company": j.company, "location": j.location,
            "type": j.job_type, "source": j.source, "url": j.url,
            "desc": j.description[:800],
            "posted": j.posted.isoformat() if j.posted else None,
            "first_seen": r[0] if r else now,
        })
    rows.sort(key=lambda r: r["first_seen"], reverse=True)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"updated": now, "jobs": rows}, fh, ensure_ascii=False, indent=1)
    print(f"Exported {len(rows)} jobs to {path}")


def send_email(msg, cfg):
    e = cfg["email"]
    with smtplib.SMTP(e["smtp_host"], e.get("smtp_port", 587)) as s:
        s.starttls()
        s.login(os.environ[e["username_env"]], os.environ[e["password_env"]])
        s.send_message(msg)


# ---------------------------------------------------------------- main
def collect(cfg):
    all_jobs = []
    for src in cfg["sources"]:
        fetch = FETCHERS.get(src["type"])
        if not fetch:
            print(f"[skip] unknown source type {src['type']!r} for {src.get('name')}", file=sys.stderr)
            continue
        try:
            jobs = list(fetch(src))
            print(f"[ok]   {src['name']}: {len(jobs)} postings")
            all_jobs.extend(jobs)
        except Exception as ex:  # one broken site must not stop the rest
            print(f"[fail] {src['name']}: {ex}", file=sys.stderr)
    return all_jobs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print matches; don't email or save state")
    ap.add_argument("--baseline", action="store_true", help="mark current postings as seen, no email")
    ap.add_argument("--export", metavar="PATH", help="write matching jobs to a JSON file for the website")
    args = ap.parse_args()

    with open(args.config) as fh:
        cfg = yaml.safe_load(fh)

    db_path = cfg.get("database", "jobs.db")
    first_run = not os.path.exists(db_path)
    db = open_db(db_path)
    if first_run and not args.dry_run and not args.baseline:
        # brand-new copy of the project: don't email every job that already exists
        print("First run detected: saving current jobs as the baseline (no email this time).")
        args.baseline = True
    jobs = [j for j in collect(cfg) if matches(j, cfg["filters"])]
    # de-dupe within this run
    jobs = list({j.uid: j for j in jobs}.values())

    if args.baseline:
        mark_seen(db, jobs)
        print(f"Baseline saved: {len(jobs)} matching postings marked as seen.")
        if args.export:
            export_json(db, jobs, args.export)
        return

    new = [j for j in jobs if not is_seen(db, j)]
    new.sort(key=lambda j: j.posted or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    print(f"{len(jobs)} matching, {len(new)} new.")

    if args.dry_run:
        for j in new:
            print(f"- {j.title} | {j.company} | {j.location} | {j.job_type} | {j.url}")
        return
    if new:
        if email_ready(cfg):
            send_email(build_email(new, cfg), cfg)  # raises on failure -> nothing marked seen, retried next run
            print(f"Emailed {len(new)} jobs.")
        else:
            print("[warn] email not configured (SMTP_USER / SMTP_PASS / EMAIL_TO); skipping email.")
        mark_seen(db, new)
    if args.export:
        export_json(db, jobs, args.export)


if __name__ == "__main__":
    main()
