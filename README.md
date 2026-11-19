# Job Radar

Four times a day, a GitHub Actions job checks 13 job boards for fresh developer jobs: remote ones, and local ones in Cyprus. It filters them by stack, level and location, has an LLM rate the best ones, and sends a short digest to Telegram.

```
Job Radar · 2 remote · 1 Cyprus · 1 gig

Remote

1. Full Stack Engineer (Next.js / TypeScript / AI)
Acme · Worldwide · Full Time · $60k–80k
Himalayas · fit 9/10 · +stripe
Next.js + TypeScript + LLM, 2+ years: strong match

2. Junior Full-Stack JavaScript Developer
Northwind · Countries of Europe or Ukraine
Djinni · fit 6/10 · ⚠ low reply rate
Junior, React + Node, but the recruiter rarely replies

Cyprus

1. Frontend Developer (React)
Bluebay · Larnaca, Cyprus · hybrid · Full-time
ergodotisi · fit 7/10
React + TypeScript, hybrid in Larnaca

Gigs

1. Software Engineer - AI Trainer
micro1 · Worldwide · Contract
Himalayas · fit 6/10
Per-task AI training, TypeScript tasks
```

## How it works

1. **Fetch.** All boards are queried in parallel through public APIs, RSS feeds and web pages. If a board fails, it is skipped and reported, and the run continues.
2. **Dedupe.** Three kinds of postings are dropped:
   - older than 3 days, counted from the first publication rather than the latest renewal (Djinni and DOU renew old postings);
   - already sent;
   - posted again on another board (same company and title).
3. **Screen.** Rules from [config.toml](config.toml) decide what stays:
   - the title must be a developer role in the right stack, below Senior or Lead ("Middle/Senior" stays);
   - the job may ask for at most 3 years of experience; years that are only "a plus" don't count;
   - the location, the board's list of countries or the time zones must fit;
   - remote jobs must be fully remote; Cyprus jobs may be on-site or hybrid near home, but must be remote anywhere else on the island;
   - phrases like "US only" rule a job out.

   The remaining jobs earn points for stack keywords. Paid-per-task gigs, such as AI training, skip the level rules and get a section of their own.
4. **Read job pages.** Djinni's RSS and ergodotisi's list leave out the years of experience, the countries, the recruiter's reply rate and the first publication date. For up to 40 jobs that pass, or nearly pass, the bot reads the job's page and screens it again.
5. **Rate.** The top 15 go to an LLM in one request; Cyprus jobs and gigs keep up to 5 and 3 of those places. The default is `gpt-oss-120b` on Groq's free tier. The LLM rates each job's fit from 0 to 10 and gives a one-line reason. Keywords with a `fit_bonus` in [config.toml](config.toml), such as Stripe, webhooks or MCP, add a point to the fit of a job that passes; a recruiter who rarely replies, or hasn't replied for 30 days, takes one off. If no key is set or the call fails, the digest falls back to the keyword ranking.
6. **Send.** The digest goes to Telegram in up to three sections: Remote, Cyprus and Gigs. Sent jobs, and jobs the LLM rejected, are stored in the Actions cache, so the same job never arrives twice.

## Setup

1. Push the repo to GitHub. For a public repo, Actions minutes are free (see [Limits](#limits-and-costs)).
2. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and send the bot any message. Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Optional: create an API key at [console.groq.com](https://console.groq.com). Any OpenAI-compatible API works; set `[llm].base_url` and `model` to use another one.
4. In **Settings → Secrets and variables → Actions**, add these secrets:
   - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`;
   - optionally `LLM_API_KEY` and `LLM_PROFILE` (see [Privacy](#privacy)).
5. Go to **Actions → Job Radar → Run workflow** to try it.

## Run locally

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv run python -m job_radar --dry-run    # print the digest with score breakdowns; send and store nothing
uv run pytest
```

To send a real digest from your machine, run `cp .env.example .env`, fill in the values, then run `uv run --env-file .env python -m job_radar`.

## Tuning

Every setting lives in [config.toml](config.toml): keywords and their weights, title rules, allowed locations, seniority and years limits, gigs, Cyprus towns, feeds and LLM settings. Terms match whole words only, so `java` doesn't match "JavaScript".

A dry run prints how each job's score was built, for example `+3 typescript +3 react +2 full-stack (title) +2 stripe +2 location`. Each run's summary also counts the jobs each rule rejected, so you can see which rule is too strict.

## Boards

| Name | Board | Source |
|---|---|---|
| `remotive` | [Remotive](https://remotive.com) | API, software development |
| `remoteok` | [Remote OK](https://remoteok.com) | API |
| `wwr` | [We Work Remotely](https://weworkremotely.com) | RSS: full-stack, front-end, back-end |
| `himalayas` | [Himalayas](https://himalayas.app) | Search API, one query per keyword, filtered by country |
| `jobicy` | [Jobicy](https://jobicy.com) | API, dev jobs |
| `workingnomads` | [Working Nomads](https://www.workingnomads.com) | API |
| `hn` | [HN "Who is hiring?"](https://news.ycombinator.com/submitted?id=whoishiring) | Algolia API: the newest thread, remote posts only |
| `hn_jobs` | [HN jobs](https://news.ycombinator.com/jobs) | HN API, remote posts only |
| `dou` | [DOU](https://jobs.dou.ua) | RSS, remote |
| `djinni` | [Djinni](https://djinni.co) | RSS, remote, plus job pages |
| `dou_cy` | [DOU](https://jobs.dou.ua) | RSS: Larnaca, Limassol, Nicosia and Paphos, any format |
| `ergodotisi` | [ergodotisi.com](https://ergodotisi.com) | Search page: Software Engineering in Cyprus, plus job pages |
| `telegram` | [@cyithr](https://t.me/s/cyithr) | The public channel's web preview |

The last three feed the Cyprus section.

As the boards' API terms ask, each digest entry links to the original posting and names its board. The bot only links to postings and doesn't republish them. Each run makes one request per feed, plus at most 40 job pages, and there are four runs a day.

To add a board, write two functions in [sources.py](job_radar/sources.py):

- `parse_x(payload) -> list[Job]`, with a test on a saved sample;
- `fetch_x(client, cfg)`, which downloads the payload and calls the parser.

Then register the board in `SOURCES` and `LABELS`.

## Limits and costs

- **GitHub Actions** is free for public repos on standard runners. In a private repo, each run bills about one minute, because GitHub rounds every job up to a full minute. Four runs a day come to roughly 124 of the Free plan's 2,000 minutes a month.
- **Scheduled runs** may start late, or be skipped, when GitHub is under load.
- **Inactive public repos:** GitHub disables scheduled workflows after 60 days without repository activity. A commit, or re-enabling the workflow, brings them back.
- **Groq free tier:** each run makes one request. The prompt size (`max_jobs` × `description_chars`) keeps that request under the 8k tokens per minute that the free tier allows for `gpt-oss-120b`.
- **Seen-jobs state** is a few KB in the Actions cache. GitHub deletes cache entries unused for 7 days. Every run reads and writes the cache, so it only expires if the bot stops running for that long.

## Privacy

In a public repo, anyone can see `config.toml`, the run logs and the run summaries. Job postings are public anyway, but keep personal details such as your work permit or rates out of `config.toml`. Put them in the `LLM_PROFILE` secret instead; when it's set, it replaces `[profile].summary`. Note that `[cyprus].local` lists the towns near your home, so it shows roughly where you live. Write the profile in `profile.txt` (git ignores it) and upload it with `gh secret set LLM_PROFILE < profile.txt`. Secrets never appear in the logs, and the bot reports Telegram errors without the URL, which contains the token.

## Layout

| Path | What it does |
|---|---|
| [job_radar/sources.py](job_radar/sources.py) | Boards: a parser and a fetcher for each |
| [job_radar/scoring.py](job_radar/scoring.py) | Screening rules and keyword scores |
| [job_radar/llm.py](job_radar/llm.py) | LLM rating in one batched request |
| [job_radar/notify.py](job_radar/notify.py) | Telegram digest, run summary and console report |
| [job_radar/state.py](job_radar/state.py) | Jobs already handled |
| [job_radar/\_\_main\_\_.py](job_radar/__main__.py) | The pipeline |
