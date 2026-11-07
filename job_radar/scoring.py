"""Cheap, deterministic screening: drop what clearly doesn't fit, score the rest by keywords."""

import re
from collections import Counter
from datetime import UTC, datetime
from functools import cache

from .config import Config, LocationConfig
from .models import Candidate, Job

_YEARS = (
    # "5+ years of experience", "3-5 years of professional experience"
    re.compile(
        r"(?<!\d)(\d{1,2})\s*(?:\+|(?:-|–|to)\s*\d{1,2})?\s*(?:years?|yrs?)"
        r"(?:\s+of)?(?:\s+[\w-]+){0,3}?\s+experience",
        re.IGNORECASE,
    ),
    # "Experience: 5+ years"
    re.compile(r"experience\s*(?:of|:|-|–)?\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.IGNORECASE),
    # "досвід від 3 років", "опыт от 2-х лет"
    re.compile(r"(?<!\w)(?:від|от)\s+(\d{1,2})\S*\s+(?:рок|лет|год)", re.IGNORECASE),
)


@cache
def _pattern(term: str) -> re.Pattern[str]:
    # Whole words only, plural "s" allowed: "java" must not match "javascript", "ai" not "maintain".
    return re.compile(rf"(?<!\w){re.escape(term)}s?(?!\w)", re.IGNORECASE)


def find_any(text: str, terms: list[str]) -> str | None:
    """The first of `terms` that appears in `text` as a whole word."""
    return next((term for term in terms if _pattern(term).search(text)), None)


def required_years(text: str) -> int | None:
    """Most years of experience the posting asks for, if it says."""
    found = (int(m.group(1)) for p in _YEARS for m in p.finditer(text))
    # Bigger numbers are about the company ("20 years of experience in fintech"), not the hire.
    return max((n for n in found if n <= 15), default=None)


def region_verdict(job: Job, loc: LocationConfig) -> bool | None:
    """True = location fits, False = it rules the job out, None = can't tell."""
    if job.countries:
        return any(find_any(country, loc.ok) for country in job.countries)
    low, high = loc.utc_offsets
    if job.utc_offsets and not any(low <= tz <= high for tz in job.utc_offsets):
        return False
    if find_any(job.location, loc.ok):
        return True
    if find_any(job.location, loc.blocked):
        return False
    return None


def screen(job: Job, cfg: Config) -> Candidate:
    """Apply the hard rules, then add up keyword points. Check `.rejected` on the result."""
    cand = Candidate(job)
    title = job.title
    body = " ".join([job.description, job.employment, *job.tags])

    if not find_any(title, cfg.title.require_any):
        return cand.reject("title", "not a dev role")
    if term := find_any(title, cfg.seniority.reject_titles):
        return cand.reject("seniority", term)
    if (term := find_any(title, cfg.title.reject_any)) and not find_any(title, cfg.title.rescue_any):
        return cand.reject("title", term)
    if phrase := find_any(f"{title} {job.location} {body}", cfg.location.reject_phrases):
        return cand.reject("phrase", phrase)
    region = region_verdict(job, cfg.location)
    if region is False:
        return cand.reject("region", job.location[:40] or "time zone")

    for group in cfg.keywords:
        if term := find_any(title, group.terms):
            cand.add(2 * group.weight, f"{term} (title)")
        elif term := find_any(body, group.terms):
            cand.add(group.weight, term)
    if region:
        cand.add(cfg.location.bonus, "location")
    if term := find_any(title, cfg.seniority.penalty_titles):
        cand.add(-cfg.seniority.title_penalty, term)
    years = required_years(body)
    if years and years > cfg.seniority.max_years:
        cand.add(-cfg.seniority.years_penalty, f"{years}+ years")

    if cand.score < cfg.run.min_score:
        return cand.reject("score", str(cand.score))
    return cand


def screen_all(jobs: list[Job], cfg: Config) -> tuple[list[Candidate], Counter[str]]:
    """Screen every job; return the survivors best first and how many each rule rejected."""
    passed: list[Candidate] = []
    rejected: Counter[str] = Counter()
    for job in jobs:
        cand = screen(job, cfg)
        if cand.rejected:
            rejected[cand.rejected.split(":")[0]] += 1
        else:
            passed.append(cand)
    oldest = datetime.min.replace(tzinfo=UTC)
    passed.sort(key=lambda c: (c.score, c.job.posted_at or oldest), reverse=True)
    return passed, rejected
