"""Run the radar once: python -m job_radar [--dry-run] [--config config.toml]."""

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import llm, notify, scoring, sources
from .config import Config, load_config
from .models import Job
from .state import SeenJobs

log = logging.getLogger("job_radar")

USER_AGENT = "job-radar/0.1 (personal job-search bot)"


def select_new(jobs: list[Job], seen: SeenJobs, since: datetime) -> list[Job]:
    """Drop old postings, ones already handled, and the same job reposted on another board."""
    new: list[Job] = []
    taken: set[str] = set()
    for job in jobs:
        ids = {job.key, job.fingerprint}
        if (job.posted_at and job.posted_at < since) or ids & taken or any(i in seen for i in ids):
            continue
        taken |= ids
        new.append(job)
    return new


async def run(cfg: Config, dry_run: bool) -> int:
    now = datetime.now(UTC)
    seen = SeenJobs.load(Path(os.environ.get("STATE_PATH", ".state/seen.json")))
    transport = httpx.AsyncHTTPTransport(retries=1)  # one retry on connection errors
    async with httpx.AsyncClient(
        transport=transport, headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True
    ) as client:
        jobs, results = await sources.fetch_all(client, cfg)
        if all(r.error for r in results):
            log.error("Every board failed, nothing to rank")
            return 1
        new = select_new(jobs, seen, since=now - timedelta(days=cfg.run.max_age_days))
        candidates, rejected = scoring.screen_all(new, cfg)

        shortlist = candidates[: cfg.llm.max_jobs]
        verdicts = await llm.rank(client, shortlist, cfg)
        if verdicts is None:
            kept, dismissed = candidates, []
        else:
            kept, dismissed = llm.apply(shortlist, verdicts, cfg.llm.min_fit)
        digest = kept[: cfg.run.max_results]

        summary = notify.Summary(
            sources=results,
            new=len(new),
            rejected=rejected,
            shortlisted=len(shortlist),
            llm_used=verdicts is not None,
            dismissed=len(dismissed),
            digest=digest,
        )
        print(notify.console_report(summary, verbose=dry_run))
        notify.write_github_summary(notify.markdown_report(summary))
        if dry_run:
            return 0
        if digest or cfg.run.notify_empty:
            try:
                await notify.send_telegram(client, notify.telegram_messages(summary))
            except (httpx.HTTPError, RuntimeError) as exc:
                log.error("Sending the digest failed: %s", sources.describe_error(exc))
                return 1  # state stays as it was, so the next run retries these jobs

    # Sent jobs and the ones the LLM turned down are never shown again.
    seen.add((key for cand in [*digest, *dismissed] for key in (cand.job.key, cand.job.fingerprint)), now)
    seen.save(now)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m job_radar", description="Find fresh remote jobs and send a Telegram digest."
    )
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="settings file (default: config.toml)")
    parser.add_argument("--dry-run", action="store_true", help="print the digest; don't send it or remember jobs")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL at INFO, and the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run(load_config(args.config), dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
