"""Job boards. Each board has a pure parser (tested on saved payloads) and a small async fetcher."""

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from .config import Config
from .models import Job

log = logging.getLogger(__name__)

# Shown next to every job in the digest: the boards' API terms ask for a link back and a credit.
LABELS = {
    "remotive": "Remotive",
    "remoteok": "Remote OK",
    "wwr": "We Work Remotely",
    "himalayas": "Himalayas",
    "jobicy": "Jobicy",
    "workingnomads": "Working Nomads",
    "hn": "HN Who is hiring",
    "hn_jobs": "HN Jobs",
    "dou": "DOU",
    "djinni": "Djinni",
}

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_API = "https://hacker-news.firebaseio.com/v0"


# --- Helpers -------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_REMOTE = re.compile(r"\bremote", re.IGNORECASE)


def strip_html(text: str | None) -> str:
    """HTML (or plain text) → one line of plain text."""
    if not text:
        return ""
    text = _TAG.sub(" ", html.unescape(_TAG.sub(" ", text)))  # some feeds escape their HTML twice
    return _SPACE.sub(" ", text).strip()


def parse_date(value: object) -> datetime | None:
    """Epoch seconds, ISO 8601 or RFC 822 → aware UTC datetime; None when missing or unreadable."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) or (isinstance(value, str) and value.strip().isdigit()):
        return datetime.fromtimestamp(int(value), UTC)
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:  # Remotive and Jobicy send naive timestamps in UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_salary(low: object, high: object, currency: str | None = "USD") -> str:
    """(80000, 100000, "USD") → "$80k–100k"; "" when the board gives no numbers."""

    def amount(n: float) -> str:
        return f"{n / 1000:.0f}k" if n >= 1000 else f"{n:g}"

    try:
        lo, hi = float(low or 0), float(high or 0)
    except (TypeError, ValueError):
        return ""
    if not lo and not hi:
        return ""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency or "USD", f"{currency} ")
    if lo and hi and lo != hi:
        return f"{symbol}{amount(lo)}–{amount(hi)}"
    return f"{symbol}{amount(lo or hi)}"


def split_wwr_title(raw: str) -> tuple[str, str]:
    """'Acme Inc: Senior Engineer' → ('Acme Inc', 'Senior Engineer')."""
    company, sep, title = raw.partition(": ")
    return (company.strip(), title.strip()) if sep else ("", raw.strip())


def split_dou_title(raw: str) -> tuple[str, str, str]:
    """'Node.js Developer в Acme, Київ, віддалено' → (title, company, location)."""
    title, sep, rest = raw.rpartition(" в ")
    if not sep:
        return raw.strip(), "", ""
    company, _, location = rest.partition(", ")
    return title.strip(), company.strip(), location.strip()


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    return f"{type(exc).__name__}: {exc}"[:200]


def _text(item: ElementTree.Element, tag: str) -> str:
    return (item.findtext(tag) or "").strip()


def _tags(*values: str) -> list[str]:
    return [tag.strip() for value in values for tag in value.split(",") if tag.strip()]


def _countries_label(countries: list[str]) -> str:
    if not countries:
        return "Worldwide"
    return ", ".join(countries) if len(countries) <= 3 else f"{len(countries)} countries"


def _unique(jobs: Iterable[Job]) -> list[Job]:
    return list({job.key: job for job in jobs}.values())


# --- Parsers: board payload → jobs ---------------------------------------------

def parse_remotive(data: dict) -> list[Job]:
    return [
        Job(
            source="remotive",
            id=str(item["id"]),
            title=strip_html(item["title"]),
            company=item.get("company_name") or "",
            url=item["url"],
            location=item.get("candidate_required_location") or "",
            tags=item.get("tags") or [],
            description=strip_html(item.get("description")),
            posted_at=parse_date(item.get("publication_date")),
            salary=item.get("salary") or "",
            employment=(item.get("job_type") or "").replace("_", " "),
        )
        for item in data.get("jobs", [])
    ]


def parse_remoteok(data: list) -> list[Job]:
    return [
        Job(
            source="remoteok",
            id=str(item["id"]),
            title=strip_html(item["position"]),
            company=item.get("company") or "",
            url=item.get("url") or f"https://remoteok.com/remote-jobs/{item['id']}",
            location=item.get("location") or "",
            tags=item.get("tags") or [],
            description=strip_html(item.get("description")),
            posted_at=parse_date(item.get("epoch")),
            salary=format_salary(item.get("salary_min"), item.get("salary_max")),
        )
        for item in data
        if isinstance(item, dict) and item.get("position")  # the first element is the API terms notice
    ]


def parse_wwr(items: list[ElementTree.Element]) -> list[Job]:
    jobs = []
    for item in items:
        company, title = split_wwr_title(_text(item, "title"))
        link = _text(item, "link")
        jobs.append(Job(
            source="wwr",
            id=_text(item, "guid") or link,
            title=strip_html(title),
            company=company,
            url=link,
            location=", ".join(x for x in (_text(item, "region"), _text(item, "country")) if x),
            tags=_tags(_text(item, "skills")),
            description=strip_html(_text(item, "description")),
            posted_at=parse_date(_text(item, "pubDate")),
            employment=_text(item, "type"),
        ))
    return jobs


def parse_himalayas(data: dict) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        countries = item.get("locationRestrictions") or []
        url = item.get("guid") or item.get("applicationLink") or ""  # guid is the clean Himalayas link
        jobs.append(Job(
            source="himalayas",
            id=item.get("guid") or url,
            title=strip_html(item["title"]),
            company=item.get("companyName") or "",
            url=url,
            location=_countries_label(countries),
            countries=countries,
            utc_offsets=item.get("timezoneRestrictions") or [],
            tags=[*(item.get("categories") or []), *(item.get("seniority") or [])],
            description=strip_html(item.get("description") or item.get("excerpt")),
            posted_at=parse_date(item.get("pubDate")),
            salary=format_salary(item.get("minSalary"), item.get("maxSalary"), item.get("currency")),
            employment=item.get("employmentType") or "",
        ))
    return jobs


def parse_jobicy(data: dict) -> list[Job]:
    return [
        Job(
            source="jobicy",
            id=str(item["id"]),
            title=strip_html(item["jobTitle"]),
            company=strip_html(item.get("companyName")),
            url=item["url"],
            location=item.get("jobGeo") or "",
            tags=[strip_html(tag) for tag in item.get("jobIndustry") or []],
            description=strip_html(item.get("jobDescription") or item.get("jobExcerpt")),
            posted_at=parse_date(item.get("pubDate")),
            salary=format_salary(
                item.get("annualSalaryMin"), item.get("annualSalaryMax"), item.get("salaryCurrency")
            ),
            employment=", ".join(item.get("jobType") or []),
        )
        for item in data.get("jobs", [])
    ]


def parse_workingnomads(data: list) -> list[Job]:
    return [
        Job(
            source="workingnomads",
            id=item["url"],  # the API has no id field
            title=strip_html(item.get("title")),
            company=item.get("company_name") or "",
            url=item["url"],
            location=item.get("location") or "",
            tags=_tags(item.get("category_name") or "", item.get("tags") or ""),
            description=strip_html(item.get("description")),
            posted_at=parse_date(item.get("pub_date")),
        )
        for item in data
        if item.get("url")
    ]


def parse_hn_comments(data: dict, story_id: str) -> list[Job]:
    """Top-level posts of a 'Who is hiring?' thread that mention remote work."""
    jobs = []
    for hit in data.get("hits", []):
        body = hit.get("comment_text") or ""
        if str(hit.get("parent_id")) != str(story_id) or not body:
            continue  # replies and deleted posts
        text = strip_html(body)
        if not _REMOTE.search(text):
            continue
        # Posts start with a line like "Acme | Senior Engineer | Remote (EU) | Full-time".
        headline = strip_html(body.partition("<p>")[0])[:160]
        jobs.append(Job(
            source="hn",
            id=str(hit["objectID"]),
            title=headline,
            company=headline.split("|")[0].strip() if "|" in headline else "",
            url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
            location=headline,
            description=text,
            posted_at=parse_date(hit.get("created_at_i")),
        ))
    return jobs


def parse_hn_jobs(items: list[dict]) -> list[Job]:
    """Y Combinator job ads; most are on-site, so only ones that mention remote are kept."""
    jobs = []
    for item in items:
        if not item or item.get("deleted") or item.get("dead"):
            continue
        title = strip_html(item.get("title"))
        text = strip_html(item.get("text"))
        if not _REMOTE.search(f"{title} {text}"):
            continue
        jobs.append(Job(
            source="hn_jobs",
            id=str(item["id"]),
            title=title,
            company=re.split(r"\s+\(YC |\s+is hiring", title, maxsplit=1, flags=re.IGNORECASE)[0],
            url=item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
            location=title,
            description=text,
            posted_at=parse_date(item.get("time")),
        ))
    return jobs


def parse_dou(items: list[ElementTree.Element]) -> list[Job]:
    jobs = []
    for item in items:
        title, company, location = split_dou_title(_text(item, "title"))
        link = _text(item, "link")
        jobs.append(Job(
            source="dou",
            id=link.split("?")[0],
            title=title,
            company=company,
            url=link,
            location=location,
            description=strip_html(_text(item, "description")),
            posted_at=parse_date(_text(item, "pubDate")),
        ))
    return jobs


def parse_djinni(items: list[ElementTree.Element]) -> list[Job]:
    return [
        Job(
            source="djinni",
            id=_text(item, "link"),
            title=strip_html(_text(item, "title")),
            url=_text(item, "link"),
            tags=[c.text.strip() for c in item.findall("category") if c.text and c.text.strip()],
            description=strip_html(_text(item, "description")),
            posted_at=parse_date(_text(item, "pubDate")),
        )
        for item in items
    ]


# --- Fetchers --------------------------------------------------------------------

async def _json(client: httpx.AsyncClient, url: str, **params: str | int) -> object:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def _rss(client: httpx.AsyncClient, url: str) -> list[ElementTree.Element]:
    resp = await client.get(url)
    resp.raise_for_status()
    return ElementTree.fromstring(resp.content).findall(".//item")


async def _gather_ok(requests: Iterable[Awaitable]) -> list:
    """Run requests together and keep the ones that worked; fail only when all of them failed."""
    results = await asyncio.gather(*requests, return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors and len(errors) == len(results):
        raise errors[0]
    for error in errors:
        log.warning("Partial failure: %s", describe_error(error))
    return [r for r in results if not isinstance(r, BaseException)]


async def _feeds(client: httpx.AsyncClient, urls: list[str], parse: Callable) -> list[Job]:
    feeds = await _gather_ok(_rss(client, url) for url in urls)
    return _unique(job for items in feeds for job in parse(items))


async def fetch_remotive(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    url = "https://remotive.com/api/remote-jobs"
    return parse_remotive(await _json(client, url, category="software-dev", limit=100))


async def fetch_remoteok(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return parse_remoteok(await _json(client, "https://remoteok.com/api"))


async def fetch_wwr(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return await _feeds(client, cfg.sources.wwr_feeds, parse_wwr)


async def fetch_himalayas(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    # The plain feed mixes every category and covers only minutes, so search for the stack instead.
    url = "https://himalayas.app/jobs/api/search"
    params = {"sort": "recent"}
    if cfg.sources.himalayas_country:  # jobs open to this country, worldwide ones included
        params["country"] = cfg.sources.himalayas_country
    pages = await _gather_ok(_json(client, url, q=q, **params) for q in cfg.sources.himalayas_queries)
    return _unique(job for page in pages for job in parse_himalayas(page))


async def fetch_jobicy(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    url = "https://jobicy.com/api/v2/remote-jobs"
    return parse_jobicy(await _json(client, url, count=50, industry="dev"))


async def fetch_workingnomads(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return parse_workingnomads(await _json(client, "https://www.workingnomads.com/api/exposed_jobs/"))


async def fetch_hn(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    threads = await _json(client, HN_SEARCH, tags="story,author_whoishiring", hitsPerPage=5)
    story = next((h for h in threads["hits"] if "who is hiring" in h["title"].lower()), None)
    if story is None:
        return []
    since = datetime.now(UTC) - timedelta(days=cfg.run.max_age_days)
    comments = await _json(
        client,
        HN_SEARCH,
        tags=f"comment,story_{story['objectID']}",
        numericFilters=f"created_at_i>{int(since.timestamp())}",
        hitsPerPage=1000,
    )
    return parse_hn_comments(comments, story["objectID"])


async def fetch_hn_jobs(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    ids = await _json(client, f"{HN_API}/jobstories.json")
    items = await _gather_ok(_json(client, f"{HN_API}/item/{i}.json") for i in ids[:30])
    return parse_hn_jobs(items)


async def fetch_dou(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return await _feeds(client, cfg.sources.dou_feeds, parse_dou)


async def fetch_djinni(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return await _feeds(client, cfg.sources.djinni_feeds, parse_djinni)


Fetcher = Callable[[httpx.AsyncClient, Config], Awaitable[list[Job]]]

# Order matters: when boards carry the same job, the one listed first is kept.
SOURCES: dict[str, Fetcher] = {
    "remotive": fetch_remotive,
    "remoteok": fetch_remoteok,
    "wwr": fetch_wwr,
    "himalayas": fetch_himalayas,
    "jobicy": fetch_jobicy,
    "workingnomads": fetch_workingnomads,
    "hn": fetch_hn,
    "hn_jobs": fetch_hn_jobs,
    "dou": fetch_dou,
    "djinni": fetch_djinni,
}


@dataclass
class SourceResult:
    name: str
    count: int = 0
    error: str = ""


async def fetch_all(client: httpx.AsyncClient, cfg: Config) -> tuple[list[Job], list[SourceResult]]:
    """Query every enabled board at once. A board that fails is reported, not fatal."""
    if unknown := set(cfg.sources.disabled) - SOURCES.keys():
        log.warning("Unknown names in [sources].disabled: %s", ", ".join(sorted(unknown)))
    names = [name for name in SOURCES if name not in cfg.sources.disabled]
    outcomes = await asyncio.gather(*(SOURCES[n](client, cfg) for n in names), return_exceptions=True)
    jobs: list[Job] = []
    results: list[SourceResult] = []
    for name, outcome in zip(names, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.warning("%s failed: %s", name, describe_error(outcome))
            results.append(SourceResult(name, error=describe_error(outcome)))
        else:
            jobs.extend(outcome)
            results.append(SourceResult(name, count=len(outcome)))
    return jobs, results
