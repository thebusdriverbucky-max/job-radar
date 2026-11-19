"""Cheap, deterministic screening: drop what clearly doesn't fit, score the rest by keywords."""

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import cache

from .config import Config, CyprusConfig, GigsConfig, LocationConfig, SeniorityConfig, TitleConfig
from .models import Candidate, Job

REPLY_DAYS = 30  # a recruiter silent for longer than this probably won't answer

# --- Years of experience a posting asks for ----------------------------------------

_Y = r"(?:years?|yrs?)"
_RANGE = r"(?:\+|(?:-|–|to)\s*\d{1,2}\s*\+?)"           # "5+", "3-5", "3 to 5"
_UA_N = r"(\d{1,2})(?:[.,]\d)?(?:-[а-яіїєґ]{1,3})?"     # "3", "1,5", "2-х", "5-ти"
_UA_RANGE = r"(?:\+|(?:-|–)\s*\d{1,2}(?:-[а-яіїєґ]{1,3})?\s*\+?)"
_UA_Y = r"(?:рок\w*|рік|лет|года?)"                     # років, роки, року, рік; лет, года, год
_UA_EXP = r"(?:досвід\w*|опыт\w*)"

_YEARS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # "5+ years of experience", "3-5 years of professional experience", "4 yrs of hands-on React experience"
    rf"(?<![\d.,])(\d{{1,2}})\s*{_RANGE}?\s*{_Y}['’]?(?:\s+of)?(?:\s+[\w/-]+(?:\.\w+)*){{0,6}}?\s+experience",
    # "Experience: 6+ years", "experience of 3 years"
    rf"experience\s*(?:of|:|-|–)?\s*(\d{{1,2}})\s*\+?\s*{_Y}",
    # "3+ years in web development", "2-4 years with React", "2 years or more": "years" alone is enough
    rf"(?<![\d.,])(\d{{1,2}})\s*(?:{_RANGE}\s*{_Y}\b|{_Y}\s+or\s+more\b)(?!\s+(?:ago|old|on\s+the\s+market|in\s+business))",
    # "at least 4 years", "minimum of 3 years"
    rf"(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+(\d{{1,2}})\s*\+?\s*{_Y}\b",
    # "+4 years of active practice"
    rf"(?<![\w+])\+\s*(\d{{1,2}})\s*{_Y}\b",
    # "від 3 років", "от 2-х лет", "мінімум 2 роки"
    rf"(?<!\w)(?:від|от|мінімум|минимум|не\s+менше|не\s+менее)\s+{_UA_N}\s*\+?\s+{_UA_Y}\b",
    # "3+ роки досвіду", "6+ роками комерційного досвіду", "2 года опыта"
    rf"(?<![\d.,]){_UA_N}\s*{_UA_RANGE}?\s*{_UA_Y}(?:\s+[\w’'-]+){{0,3}}?\s+{_UA_EXP}",
    # "Досвід роботи: 3+ роки", "опыт разработки 2 года"
    rf"{_UA_EXP}(?:\s+[\w’'.-]+){{0,4}}?\s*[:—–-]?\s*{_UA_N}\s*{_UA_RANGE}?\s*{_UA_Y}\b",
    # "React 2+ роки", "2-3 роки": with a "+" or a range, the years word alone is enough
    rf"(?<![\d.,]){_UA_N}\s*{_UA_RANGE}\s*{_UA_Y}\b(?!\s+(?:тому|назад|на\s+ринку|на\s+рынке))",
))
# Years that are a wish, not a requirement: "Ideally 5+ years…", "…7 years of experience is a plus".
_WISH_BEFORE = re.compile(
    r"ideally|preferably|preferred|nice to have|bonus|\bplus\s*:|бажано|желательно|буде перевагою|будет плюсом",
    re.IGNORECASE,
)
_WISH_AFTER = re.compile(
    r"(?:is|are|would be|will be)\s+(?:a\s+)?(?:big\s+|huge\s+|strong\s+)?(?:plus|advantage|bonus)|advantageous|"
    r"preferred|desirable|nice to have|буде\s+(?:перевагою|плюсом)|є\s+перевагою|"
    r"будет\s+(?:плюсом|преимуществом)|бажано|желательно",
    re.IGNORECASE,
)
_WISH_NOTE = re.compile(
    r"[^(]*\(\s*(?:preferred|preferably|nice to have|a plus|plus|optional|desirable|бажано|желательно)\s*\)", re.I
)
_CLAUSE = re.compile(r"[;!?•\n]|\.\s")
_COMPANY_AGE = 10  # "20 years of experience in fintech" is about the company, not the hire


def _is_wish(text: str, start: int, end: int) -> bool:
    before = _CLAUSE.split(text[max(0, start - 30):start])[-1]
    clause = _CLAUSE.split(text[end:end + 60])[0]
    after = clause.split("(")[0]  # "5+ years (2 years with Go is a plus)": the bracket is about something else
    return bool(_WISH_BEFORE.search(before) or _WISH_AFTER.search(after) or _WISH_NOTE.match(clause))


def required_years(text: str) -> int | None:
    """Most years of experience the posting requires, if it says; years it only wishes for don't count."""
    found = (
        int(m.group(1))
        for pattern in _YEARS
        for m in pattern.finditer(text)
        if int(m.group(1)) < _COMPANY_AGE and not _is_wish(text, m.start(), m.end())
    )
    return max(found, default=None)


# --- Matching ------------------------------------------------------------------------

@cache
def _pattern(term: str) -> re.Pattern[str]:
    # Whole words only, plural "s" allowed: "java" must not match "javascript", "ai" not "maintain".
    return re.compile(rf"(?<!\w){re.escape(term)}s?(?!\w)", re.IGNORECASE)


def find_any(text: str, terms: list[str]) -> str | None:
    """The first of `terms` that appears in `text` as a whole word."""
    return next((term for term in terms if _pattern(term).search(text)), None)


def wrong_stack(title: str, cfg: TitleConfig) -> str | None:
    """The off-stack term in a title, unless the title still names the candidate's stack without it.

    "React Native Developer" → "react native": once that's removed, no "react" is left.
    "Java/React Developer" → None: "react" is still there.
    """
    found, rest = None, title
    for term in cfg.reject_any:
        if _pattern(term).search(rest):
            found = found or term
            rest = _pattern(term).sub(" ", rest)
    return found if found and not find_any(rest, cfg.rescue_any) else None


def above_level(job: Job, cfg: SeniorityConfig) -> str | None:
    """What puts a job above the candidate's level: a title like "Senior", or the board's own level field."""
    if find_any(job.title, cfg.lower_titles):
        return None  # "Middle/Senior": worth a look
    if term := find_any(job.title, cfg.ceiling_titles):
        return term
    if job.level and not find_any(job.level, cfg.lower_titles):
        if term := find_any(job.level, cfg.ceiling_titles + cfg.reject_titles):
            return f"{term} (board level)"
    return None


def is_gig(job: Job, cfg: GigsConfig) -> bool:
    """Paid-per-task side work: a known gig platform, or gig wording at the top of the posting."""
    opening = f"{job.title} {job.company} {job.description[:300]}"
    return bool(find_any(job.company, cfg.companies) or find_any(opening, cfg.phrases))


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


_HYBRID = re.compile(r"hybrid|гибрид|гібрид", re.IGNORECASE)


def guess_mode(text: str, cfg: CyprusConfig) -> str:
    """"remote", "hybrid" or "on-site" when a posting mentions only one way of working, else ""."""
    remote, office = find_any(text, cfg.remote_words), find_any(text, cfg.office_words)
    if remote and not office:
        return "remote"
    if office and not remote:
        return "hybrid" if _HYBRID.search(text) else "on-site"
    return ""


def cyprus_problem(job: Job, cfg: CyprusConfig) -> str:
    """Why a Cyprus job's place and format don't fit, or "": any format near home, fully remote elsewhere."""
    where = " ".join([job.title, job.location, job.description, *job.tags])
    mode = job.work_mode or guess_mode(where, cfg)
    if mode == "remote" or find_any(where, cfg.local):
        return ""  # a job naming home and another city goes to the LLM
    elsewhere = find_any(where, cfg.elsewhere)
    if mode:
        return f"{mode} in {elsewhere or job.location[:30] or 'Cyprus'}"
    if elsewhere or find_any(where, cfg.country):
        return ""  # format not stated: the LLM reads the text
    return "unknown"


# --- Screening -----------------------------------------------------------------------

def screen(job: Job, cfg: Config, now: datetime | None = None) -> Candidate:
    """Apply the hard rules, then add up keyword points. Check `.rejected` on the result."""
    now = now or datetime.now(UTC)
    cand = Candidate(job, gig=is_gig(job, cfg.gigs))
    title = job.title
    body = " ".join([job.description, job.employment, *job.tags])

    if not find_any(title, cfg.title.require_any):
        return cand.reject("title", "not a dev role")
    if term := find_any(title, cfg.seniority.reject_titles):
        return cand.reject("level", term)
    if not cand.gig and (term := above_level(job, cfg.seniority)):
        return cand.reject("level", term)
    if term := wrong_stack(title, cfg.title):
        return cand.reject("title", term)
    if phrase := find_any(f"{title} {job.location} {body}", cfg.location.reject_phrases):
        return cand.reject("phrase", phrase)

    region = None
    if job.track == "cyprus":
        if problem := cyprus_problem(job, cfg.cyprus):
            return cand.reject("format", problem)
    else:
        region = region_verdict(job, cfg.location)
        if region is False:
            return cand.reject("region", (", ".join(job.countries) or job.location)[:40] or "time zone")
        if job.work_mode in ("hybrid", "on-site"):
            return cand.reject("format", job.work_mode)

    for group in cfg.keywords:
        if term := find_any(title, group.terms):
            cand.add(2 * group.weight, f"{term} (title)")
        elif term := find_any(body, group.terms):
            cand.add(group.weight, term)
        else:
            continue
        if group.fit_bonus:
            cand.bonus.append(term)
            cand.fit_bonus += group.fit_bonus
    if region:
        cand.add(cfg.location.bonus, "location")

    if not cand.gig:
        years = job.min_years if job.min_years is not None else required_years(body)
        if years is not None and years > cfg.seniority.max_years:
            return cand.reject("years", str(years))

    if job.reply_rate in ("low", "very low"):
        cand.warnings.append("low reply rate")
    if job.last_reply and job.last_reply < now - timedelta(days=REPLY_DAYS):
        cand.warnings.append(f"no reply in {REPLY_DAYS}+ days")

    if cand.score < min_score(cand, cfg):
        return cand.reject("score", str(cand.score))
    return cand


def min_score(cand: Candidate, cfg: Config) -> int:
    if cand.gig:
        return cfg.gigs.min_score
    return cfg.cyprus.min_score if cand.job.track == "cyprus" else cfg.run.min_score


def near_miss(cand: Candidate, cfg: Config) -> bool:
    """Passed, or short on score only by what the job's page may add: the location bonus, or a Cyprus job's full text."""
    if not cand.rejected:
        return True
    if not cand.rejected.startswith("score:"):
        return False
    return cand.job.track == "cyprus" or cand.score + cfg.location.bonus >= min_score(cand, cfg)


def screen_all(jobs: list[Job], cfg: Config, now: datetime | None = None) -> tuple[list[Candidate], Counter[str]]:
    """Screen every job; return the survivors best first and how many each rule rejected."""
    passed: list[Candidate] = []
    rejected: Counter[str] = Counter()
    for job in jobs:
        cand = screen(job, cfg, now)
        if cand.rejected:
            rejected[cand.rejected.split(":")[0]] += 1
        else:
            passed.append(cand)
    oldest = datetime.min.replace(tzinfo=UTC)
    passed.sort(key=lambda c: (c.score, c.job.posted_at or oldest), reverse=True)
    return passed, rejected
