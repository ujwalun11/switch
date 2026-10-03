# Job Finder (free, hosted on GitHub)

Scans the career sites you choose three times a day, emails you new jobs that match your filters,
and publishes a searchable website of everything it found. It runs entirely on GitHub's free tier,
with no server and no cost.

Each person who wants their own job alerts makes their own copy of this repo
(own filters, own email, own website). Nothing is shared between copies.

---

## Get your own copy (about 10 minutes)

### 1. Copy the repo
Click the green **Use this template** button at the top of the repo page -> **Create a new repository**.
- Name it anything (e.g. `my-job-finder`).
- Choose **Public**. GitHub Pages is only free for public repos.
  Because it's public, don't type your email or passwords into any file. They go in Secrets (step 3).

> Why "Use this template" and not "Fork"? A template copy starts clean, and its scheduled jobs run without
> extra switching on. If you do fork, open the **Actions** tab and click "I understand my workflows, enable them".

### 2. Set your filters
In your new repo, open `config.yaml` -> pencil icon (Edit). Change:
- `filters`: keywords, locations, job types (`full-time`, `internship`), max age in days.
- `sources`: the companies you want to watch (see "Adding companies" below).

Click **Commit changes**.

### 3. Add your email secrets
Settings -> Secrets and variables -> Actions -> **New repository secret**. Add three:

| Secret | Value |
|---|---|
| `SMTP_USER` | your Gmail address |
| `SMTP_PASS` | a Gmail **app password** (Google Account -> Security -> 2-Step Verification -> App passwords) |
| `EMAIL_TO` | where the job emails should go (can be the same Gmail) |

### 4. Turn on the website
Settings -> Pages -> Source: **GitHub Actions**.

### 5. Run it once
Actions tab -> **Job finder** -> **Run workflow**.
The first run only records the jobs that already exist (so you don't get hundreds of emails) and builds the site.
After about a minute your site is live at `https://<your-username>.github.io/<repo-name>/`.

From then on it runs by itself (07:00, 13:00, 19:00 IST) and emails you only **new** matches.

---

## Adding companies

Open the company's careers page and look at the URL:

| URL looks like | Add to `sources` |
|---|---|
| `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `type: greenhouse`, `board: <slug>` |
| `jobs.lever.co/<slug>` | `type: lever`, `company: <slug>` |
| `jobs.ashbyhq.com/<slug>` | `type: ashby`, `board: <slug>` |
| a job RSS feed | `type: rss`, `url: ...` |
| anything else | `type: html` with CSS selectors (example in `config.yaml`) |

Each entry also needs a `name:` (any label you like).
LinkedIn, Naukri and Indeed load jobs with JavaScript and restrict scraping, so use their own email alerts for those.

## Changing filters later
Edit `config.yaml` on GitHub, commit, then Actions -> Run workflow. The website's search box and dropdowns
filter what's already collected, instantly in your browser.

## If something looks wrong
- **No email arrived:** check the three secrets are set. If any is missing, the run still updates the website
  and logs `email not configured`. Open the run in the Actions tab to see the log. Gmail also needs 2-Step Verification on.
- **A company shows 0 jobs:** the slug is probably wrong. The log shows `[fail]` or `0 postings` for each source.
- **Want to start over:** delete `jobs.db` and run the workflow. The next run re-baselines.
- GitHub pauses scheduled workflows after 60 days with no repo activity; the bot's own commits count as activity.

---

## For the repo owner (sharing this with friends)
Make your repo a template so others can copy it: Settings -> General -> tick **Template repository**.
Send friends the repo link. Keep your own `jobs.db` and `docs/jobs.json` out of the template if you'd rather
they start empty (they're regenerated automatically either way).

## Run locally instead
```bash
pip install -r requirements.txt
python jobfinder.py --dry-run                 # print matches only, change nothing
python jobfinder.py --export docs/jobs.json   # email new matches + update site data
```
Set `SMTP_USER`, `SMTP_PASS` and `EMAIL_TO` as environment variables first.

## Files
```
config.yaml                  your sites + filters (the file you edit)
jobfinder.py                 the scraper and emailer
jobs.db                      remembers jobs already seen (auto-created and committed)
docs/index.html              the website
docs/jobs.json               website data (auto-updated)
.github/workflows/jobs.yml   the schedule
```
