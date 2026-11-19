"""Job boards. Each board has a pure parser (tested on saved payloads) and a small async fetcher."""

import asyncio
import html
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import partial
from xml.etree import ElementTree

import httpx

from .config import Config
from .models import Job
from .scoring import find_any

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
    "dou_cy": "DOU Cyprus",
    "djinni": "Djinni",
    "ergodotisi": "ergodotisi",
    "telegram": "Telegram",
}

HN_SEARCH = "https://hn.algolia.com/api/v1/search_by_date"
HN_API = "https://hacker-news.firebaseio.com/v0"
ERGODOTISI = "https://ergodotisi.com"


# --- Helpers -------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_REMOTE = re.compile(r"\bremote", re.IGNORECASE)
_BLOCK = re.compile(r"<(?:br|/?p|/?li|/?ul|/?ol|/?div|/?h[1-6]|/?tr|/?blockquote)\b[^>]*>", re.IGNORECASE)
_HSPACE = re.compile(r"[^\S\n]+")
_BLANKS = re.compile(r"\s*\n\s*")


def strip_html(text: str | None) -> str:
    """HTML (or plain text) → one line of plain text."""
    if not text:
        return ""
    text = _TAG.sub(" ", html.unescape(_TAG.sub(" ", text)))  # some feeds escape their HTML twice
    return _SPACE.sub(" ", text).strip()


def html_to_text(text: str | None) -> str:
    """HTML (or plain text) → plain text that keeps paragraph and list breaks."""
    if not text:
        return ""
    for _ in range(2):  # some feeds escape their HTML twice
        text = html.unescape(_TAG.sub(" ", _BLOCK.sub("\n", text)))
    return _BLANKS.sub("\n", _HSPACE.sub(" ", text)).strip()


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


_AGO = re.compile(r"(\d+|an?)\s+(minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
_DAYS_PER = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}


def parse_ago(text: str, now: datetime) -> datetime | None:
    """'12 days ago', 'an hour ago', 'yesterday' → aware datetime; None when unreadable."""
    text = text.strip().lower()
    if text in ("today", "just now"):
        return now
    if text == "yesterday":
        return now - timedelta(days=1)
    if not (m := _AGO.search(text)):
        return None
    count = 1 if m.group(1) in ("a", "an") else int(m.group(1))
    return now - timedelta(days=count * _DAYS_PER[m.group(2).lower()])


_MONTHS = {
    name: n for n, name in enumerate((
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    ), 1)
}
_DAY = re.compile(r"(\d{1,2}) ([a-z]+)(?: (\d{4}))?")


def parse_day(text: str, now: datetime) -> datetime | None:
    """'28 September', '30 September 2025', 'today' → noon UTC that day; a date without a year is in the past."""
    text = _SPACE.sub(" ", text).strip().lower()
    if text in ("today", "yesterday"):
        day = now.date() - timedelta(days=text == "yesterday")
    elif (m := _DAY.fullmatch(text)) and m.group(2) in _MONTHS:
        try:
            day = date(int(m.group(3) or now.year), _MONTHS[m.group(2)], int(m.group(1)))
            if not m.group(3) and day > now.date():
                day = day.replace(year=day.year - 1)
        except ValueError:
            return None
    else:
        return None
    return datetime(day.year, day.month, day.day, 12, tzinfo=UTC)


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


_DOU_SALARY = re.compile(r"[$€]|грн")


def split_dou_salary(location: str) -> tuple[str, str]:
    """'до $3000, Київ, віддалено' → ('до $3000', 'Київ, віддалено'); DOU puts the salary first."""
    first, _, rest = location.partition(", ")
    if _DOU_SALARY.search(first):
        return first, rest
    return "", location


_PARENS = re.compile(r"\(([^)]+)\)")


def dou_countries(location: str) -> list[str]:
    """Countries a DOU job is open to: its cities' countries, [] when it says "за кордоном" (abroad).

    'Київ, Варшава (Польща), віддалено' → ['Україна', 'Польща']; plain 'віддалено' means remote within Ukraine.
    """
    parts = [part.strip() for part in location.split(",") if part.strip()]
    if not parts or "за кордоном" in parts:
        return []
    countries = [m.group(1) if (m := _PARENS.search(part)) else "Україна" for part in parts if part != "віддалено"]
    return list(dict.fromkeys(countries)) or ["Україна"]


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


_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)


def job_posting_ld(page: str) -> dict:
    """The schema.org JobPosting a job page embeds for search engines, or {}."""
    for m in _LD.finditer(page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return {}


def _ld_company(ld: dict) -> str:
    org = ld.get("hiringOrganization")
    return strip_html(org.get("name") if isinstance(org, dict) else org if isinstance(org, str) else "")


def _ld_years(ld: dict) -> int | None:
    need = ld.get("experienceRequirements")
    try:
        return int(float(need["monthsOfExperience"]) / 12) if isinstance(need, dict) else None
    except (KeyError, TypeError, ValueError):
        return None


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
            description=html_to_text(item.get("description")),
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
            description=html_to_text(item.get("description")),
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
            description=html_to_text(_text(item, "description")),
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
            tags=item.get("categories") or [],
            level=", ".join(item.get("seniority") or []),
            description=html_to_text(item.get("description") or item.get("excerpt")),
            posted_at=parse_date(item.get("pubDate")),
            salary=format_salary(item.get("minSalary"), item.get("maxSalary"), item.get("currency")),
            employment=item.get("employmentType") or "",
        ))
    return jobs


def parse_jobicy(data: dict) -> list[Job]:
    jobs = []
    for item in data.get("jobs", []):
        salary = format_salary(
            item.get("salaryMin") or item.get("annualSalaryMin"),
            item.get("salaryMax") or item.get("annualSalaryMax"),
            item.get("salaryCurrency"),
        )
        period = item.get("salaryPeriod") or ""
        if salary and period and period != "yearly":
            salary += f" {period}"
        level = item.get("jobLevel") or ""
        jobs.append(Job(
            source="jobicy",
            id=str(item["id"]),
            title=strip_html(item["jobTitle"]),
            company=strip_html(item.get("companyName")),
            url=item["url"],
            location=item.get("jobGeo") or "",
            tags=[strip_html(tag) for tag in item.get("jobIndustry") or []],
            level="" if level.lower() == "any" else level,
            description=html_to_text(item.get("jobDescription") or item.get("jobExcerpt")),
            posted_at=parse_date(item.get("pubDate")),
            salary=salary,
            employment=", ".join(item.get("jobType") or []),
        ))
    return jobs


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
            description=html_to_text(item.get("description")),
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
        text = html_to_text(body)
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
        text = html_to_text(item.get("text"))
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


_DOU_ID = re.compile(r"/vacancies/(\d+)")
DOU_IDS_PER_DAY = 100  # DOU numbers vacancies in order; about this many are added a day
DOU_RENEWED_DAYS = 7   # a feed date this much newer than the id suggests means the vacancy was renewed


def parse_dou(items: list[ElementTree.Element], source: str = "dou", track: str = "remote") -> list[Job]:
    jobs = []
    for item in items:
        # Titles come HTML-escaped twice ("до&amp;nbsp;$3000"); strip_html undoes both.
        title, company, location = split_dou_title(strip_html(_text(item, "title")))
        salary, location = split_dou_salary(location)
        link = _text(item, "link")
        jobs.append(Job(
            source=source,
            id=link.split("?")[0],
            title=title,
            company=company,
            url=link,
            location=location,
            countries=dou_countries(location),
            salary=salary,
            description=html_to_text(_text(item, "description")),
            posted_at=parse_date(_text(item, "pubDate")),
            track=track,
            work_mode="remote" if "віддалено" in location else "on-site" if location else "",
        ))
    return dou_first_published(jobs)


def dou_first_published(jobs: list[Job]) -> list[Job]:
    """DOU dates a renewed vacancy by its renewal. Its id still tells roughly when it first appeared.

    The newest vacancy in the feed anchors the estimate: each 100 ids older is about a day older.
    """
    numbered = [(int(m.group(1)), job) for job in jobs if job.posted_at and (m := _DOU_ID.search(job.url))]
    if len(numbered) < 2:
        return jobs
    top_id, top = max(numbered, key=lambda pair: pair[0])
    estimates = {}
    for vacancy_id, job in numbered:
        estimate = top.posted_at - timedelta(days=(top_id - vacancy_id) / DOU_IDS_PER_DAY)
        if job.posted_at - estimate > timedelta(days=DOU_RENEWED_DAYS):
            estimates[job.key] = estimate
    return [job.model_copy(update={"posted_at": estimates[job.key]}) if job.key in estimates else job for job in jobs]


def parse_djinni(items: list[ElementTree.Element]) -> list[Job]:
    return [
        Job(
            source="djinni",
            id=_text(item, "link"),
            title=strip_html(_text(item, "title")),
            url=_text(item, "link"),
            tags=[c.text.strip() for c in item.findall("category") if c.text and c.text.strip()],
            description=html_to_text(_text(item, "description")),
            posted_at=parse_date(_text(item, "pubDate")),  # the latest renewal; the job page has the first one
        )
        for item in items
    ]


_DJ_CONSIDER = re.compile(r"Considering with (\d+(?:\.\d+)?|no) (?:years? of )?experience", re.IGNORECASE)
_DJ_FROM = re.compile(r"from (\d+(?:\.\d+)?) years? of experience", re.IGNORECASE)
_DJ_NONE = re.compile(r"\bNo experience\b", re.IGNORECASE)
_DJ_COUNTRIES = re.compile(
    r'<span class="location-text">(.*?)</span>\s*</strong>\s*'
    r'<div class="font-size-extra-small">Countries where we consider candidates',
    re.DOTALL,
)
_DJ_REPLY_RATE = re.compile(r"Response activity:\s*<span[^>]*>(.*?)</span>", re.DOTALL)
_DJ_LAST_REPLY = re.compile(
    r'title="(\d{1,2}:\d{2}\s+\d{1,2}\.\d{1,2}\.\d{4})"[^>]*>(?:\s*<[^>]*>)*\s*Last responded'
)
_DJ_PUBLISHED = re.compile(r'id="job-publication-info".*?Published\s+([^<]+?)\s*<', re.DOTALL)


def parse_djinni_page(page: str, now: datetime) -> dict:
    """What a Djinni job page adds to its RSS item: years, countries, recruiter activity, first publication."""
    found: dict = {}
    aside = strip_html(page[page.find("<aside"):page.find("</aside>")])
    if m := _DJ_CONSIDER.search(aside):  # "from 5 years… Considering with 4 years": the lower bar counts
        found["min_years"] = 0 if m.group(1).lower() == "no" else int(float(m.group(1)))
    elif m := _DJ_FROM.search(aside):
        found["min_years"] = int(float(m.group(1)))
    elif _DJ_NONE.search(aside):
        found["min_years"] = 0
    if m := _DJ_COUNTRIES.search(page):
        where = strip_html(m.group(1))
        found["location"] = where
        found["countries"] = [part.strip() for part in where.split(",") if part.strip()]
    if m := _DJ_REPLY_RATE.search(page):
        found["reply_rate"] = strip_html(m.group(1)).lower()
    if m := _DJ_LAST_REPLY.search(page):
        try:
            # Kyiv time; a couple of hours don't matter for "no reply in a month".
            found["last_reply"] = datetime.strptime(_SPACE.sub(" ", m.group(1)), "%H:%M %d.%m.%Y").replace(tzinfo=UTC)
        except ValueError:
            pass
    if (m := _DJ_PUBLISHED.search(page)) and (published := parse_day(m.group(1), now)):
        found["posted_at"] = published
    ld = job_posting_ld(page)  # its datePosted is the latest renewal too, so only the company is used
    if company := _ld_company(ld):
        found["company"] = company
    if "min_years" not in found and (years := _ld_years(ld)) is not None:
        found["min_years"] = years
    return found


_ERGO_ID = re.compile(r'data-analytics-job-id-value="(\d+)"')
_ERGO_HREF = re.compile(r'href="(/en-CY/jobs/[^"]+)"')
_ERGO_TITLE = re.compile(r'<h3 class="jobs-card__title"[^>]*>(.*?)</h3>', re.DOTALL)
_ERGO_COMPANY = re.compile(r'class="jobs-card__company"[^>]*>(.*?)</a>', re.DOTALL)
_ERGO_META = re.compile(r'<div class="jobs-card__meta-item[^"]*">(.*?)</div>', re.DOTALL)
_ERGO_TEXT = re.compile(r'<div class="jobs-card__description">(.*?)</div>', re.DOTALL)
_ERGO_AGO = re.compile(r'<div class="jobs-card__timestamp[^"]*">\s*<span>(.*?)</span>', re.DOTALL)
_ERGO_MODES = {"remote": "remote", "hybrid": "hybrid", "on-site": "on-site", "onsite": "on-site"}
_ERGO_EMPLOYMENT = re.compile(r"full[ -]time|part[ -]time|contract|freelance|temporary|internship|seasonal", re.I)
_ERGO_NO_SALARY = re.compile(r"[€$£]\s?1\s?[–-]\s?1\b")  # how the board shows a salary that isn't disclosed


def parse_ergodotisi(page: str, now: datetime) -> list[Job]:
    """Job cards from an ergodotisi.com search page."""
    jobs = []
    for card in re.split(r"<article\b", page)[1:]:
        card = card.split("</article>")[0]
        job_id, href, title = _ERGO_ID.search(card), _ERGO_HREF.search(card), _ERGO_TITLE.search(card)
        if not (job_id and href and title):
            continue
        fields = {"work_mode": "", "level": "", "salary": "", "employment": ""}
        places = []
        for raw in _ERGO_META.findall(card):
            item = strip_html(raw)
            if mode := _ERGO_MODES.get(item.lower()):
                fields["work_mode"] = mode
            elif item.lower().endswith("-level"):
                fields["level"] = item
            elif re.search(r"[€$£]", item):
                fields["salary"] = "" if _ERGO_NO_SALARY.search(item) else item
            elif _ERGO_EMPLOYMENT.fullmatch(item):
                fields["employment"] = item
            elif item:
                places.append(item)
        company, text, ago = _ERGO_COMPANY.search(card), _ERGO_TEXT.search(card), _ERGO_AGO.search(card)
        jobs.append(Job(
            source="ergodotisi",
            id=job_id.group(1),
            title=strip_html(title.group(1)),
            company=strip_html(company.group(1)) if company else "",
            url=ERGODOTISI + html.unescape(href.group(1)),
            location=", ".join(places),
            description=html_to_text(text.group(1)) if text else "",
            posted_at=parse_ago(strip_html(ago.group(1)), now) if ago else None,
            track="cyprus",
            **fields,
        ))
    return jobs


def parse_ergodotisi_page(page: str) -> dict:
    """What an ergodotisi job page adds to its card: the full text, the exact date, the company's full name."""
    ld = job_posting_ld(page)
    found: dict = {}
    if text := html_to_text(ld.get("description")):
        found["description"] = text
    if posted := parse_date(ld.get("datePosted")):
        found["posted_at"] = posted
    if company := _ld_company(ld):
        found["company"] = company
    if (years := _ld_years(ld)) is not None:
        found["min_years"] = years
    return found


_TG_WRAP = '<div class="tgme_widget_message_wrap'
_TG_POST = re.compile(r'data-post="([^"/]+/\d+)"')
_TG_POST_ID = re.compile(r'data-post="[^"/]+/(\d+)"')
_TG_TIME = re.compile(r'<time[^>]*datetime="([^"]+)"')
_TG_TEXT = re.compile(r'<div class="tgme_widget_message_text js-message_text"[^>]*>(.*?)</div>', re.DOTALL)
_TG_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TG_TAGS_ONLY = re.compile(r"(?:[#@]\w+[\s,]*)+")
_TG_LEADING_TAGS = re.compile(r"^(?:#\w+[\s,]*){2,}")
_TG_HASHTAG = re.compile(r"(?<!\w)#(\w+)")  # "C#" isn't one
_TG_PREFIX = re.compile(
    r"^(?:вакансия|вакансія|vacancy|job vacancy|new role|hiring|we.?re hiring|we are hiring|now hiring|"
    r"open position|position)\s*[:|–—-]\s*",
    re.IGNORECASE,
)
_TG_PLACE_LABEL = re.compile(r"^\W*(?:location|локация|локація|город|місто|city)\s*:\s*", re.IGNORECASE)
_TG_PIN = "\N{ROUND PUSHPIN}"
# Emoji, their modifiers and the invisible joiners between them.
_EMOJI_CATEGORIES = {"So", "Sk", "Cs", "Co", "Cn", "Cf", "Mn"}


def _drop_emoji(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) not in _EMOJI_CATEGORIES)


def _tg_line(line: str) -> str:
    """One line of a post as plain text: no emoji, no runs of hashtags, '#Remote' read as 'Remote'."""
    line = _SPACE.sub(" ", _drop_emoji(line)).strip()
    if not line or _TG_TAGS_ONLY.fullmatch(line):
        return ""
    return _TG_HASHTAG.sub(r"\1", _TG_LEADING_TAGS.sub("", line)).strip()


def _tg_place(line: str) -> str:
    """The place from a '📍 Limassol, Cyprus' or 'Location: Limassol' line, else ''."""
    if _TG_PIN in line:
        before, _, after = line.partition(_TG_PIN)
        text = after if _drop_emoji(after).strip() else before
    elif _TG_PLACE_LABEL.match(_drop_emoji(line)):
        text = line
    else:
        return ""
    text = _TG_PLACE_LABEL.sub("", _SPACE.sub(" ", _drop_emoji(text)).strip())
    return text.strip(" |:-–—")[:80]


def parse_telegram(page: str, headline_terms: list[str]) -> list[Job]:
    """Posts from a channel's t.me/s/ web preview. The headline is the first line that names a role."""
    jobs = []
    for chunk in page.split(_TG_WRAP)[1:]:
        post, text = _TG_POST.search(chunk), _TG_TEXT.search(chunk)
        if not (post and text):
            continue
        raw_lines = [strip_html(line) for line in _TG_BREAK.split(text.group(1))]
        lines = [clean for line in raw_lines if (clean := _tg_line(line))]
        if not lines:
            continue
        top = [_TG_PREFIX.sub("", line) for line in lines[:5]]
        headline = next((line for line in top if len(line) <= 120 and find_any(line, headline_terms)), top[0])
        posted = _TG_TIME.search(chunk)
        jobs.append(Job(
            source="telegram",
            id=post.group(1),
            title=headline.strip(" |:-–—")[:120],
            url=f"https://t.me/{post.group(1)}",
            location=next((place for line in raw_lines if (place := _tg_place(line))), ""),
            tags=list(dict.fromkeys(_TG_HASHTAG.findall("\n".join(raw_lines)))),  # not in the HTML: "&#39;" isn't a tag
            description="\n".join(lines),
            posted_at=parse_date(posted.group(1)) if posted else None,
            track="cyprus",
        ))
    return jobs


# --- Fetchers --------------------------------------------------------------------

async def _json(client: httpx.AsyncClient, url: str, **params: str | int) -> object:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


async def _rss(client: httpx.AsyncClient, url: str) -> list[ElementTree.Element]:
    resp = await client.get(url)
    resp.raise_for_status()
    return ElementTree.fromstring(resp.content).findall(".//item")


async def _page(client: httpx.AsyncClient, url: str, **params: str | int) -> str:
    resp = await client.get(url, params=params, headers={"Accept-Language": "en"})
    resp.raise_for_status()
    return resp.text


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
    # One parse over every feed's items, so DOU's id-based dating has the newest id of all of them.
    return _unique(parse([item for items in feeds for item in items]))


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


async def fetch_dou_cy(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return await _feeds(client, cfg.sources.dou_cyprus_feeds, partial(parse_dou, source="dou_cy", track="cyprus"))


async def fetch_djinni(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    return await _feeds(client, cfg.sources.djinni_feeds, parse_djinni)


async def fetch_ergodotisi(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    pages = await _gather_ok(_page(client, url) for url in cfg.sources.ergodotisi_urls)
    now = datetime.now(UTC)
    return _unique(job for page in pages for job in parse_ergodotisi(page, now))


async def fetch_telegram(client: httpx.AsyncClient, cfg: Config) -> list[Job]:
    channels = await _gather_ok(_telegram_channel(client, name, cfg) for name in cfg.sources.telegram_channels)
    return _unique(job for jobs in channels for job in jobs)


async def _telegram_channel(client: httpx.AsyncClient, name: str, cfg: Config) -> list[Job]:
    """The channel's newest posts, page by page, until they get older than max_age_days."""
    since = datetime.now(UTC) - timedelta(days=cfg.run.max_age_days)
    jobs: list[Job] = []
    params: dict[str, int] = {}
    for _ in range(cfg.sources.telegram_pages):
        page = await _page(client, f"https://t.me/s/{name}", **params)
        jobs += parse_telegram(page, cfg.title.require_any)
        ids = [int(n) for n in _TG_POST_ID.findall(page)]
        times = [t for t in map(parse_date, _TG_TIME.findall(page)) if t]
        if not ids or min(ids) == params.get("before") or (times and min(times) < since):
            break
        params["before"] = min(ids)
    return jobs


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
    "dou_cy": fetch_dou_cy,
    "djinni": fetch_djinni,
    "ergodotisi": fetch_ergodotisi,
    "telegram": fetch_telegram,
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


# --- Job pages: what feeds leave out -------------------------------------------------

async def _djinni_details(client: httpx.AsyncClient, job: Job, now: datetime) -> dict:
    return parse_djinni_page(await _page(client, job.url), now)


async def _ergodotisi_details(client: httpx.AsyncClient, job: Job, now: datetime) -> dict:
    return parse_ergodotisi_page(await _page(client, job.url))


# Boards whose feeds miss what screening needs, and how to read it from a job's own page.
ENRICHERS: dict[str, Callable[[httpx.AsyncClient, Job, datetime], Awaitable[dict]]] = {
    "djinni": _djinni_details,
    "ergodotisi": _ergodotisi_details,
}
PAGE_REQUESTS_AT_ONCE = 4


async def enrich(client: httpx.AsyncClient, jobs: list[Job], now: datetime) -> dict[str, Job]:
    """Fetch each job's page and return the updated jobs by key. A page that fails leaves its job as it was."""
    limit = asyncio.Semaphore(PAGE_REQUESTS_AT_ONCE)

    async def details(job: Job) -> dict:
        async with limit:
            return await ENRICHERS[job.source](client, job, now)

    results = await asyncio.gather(*(details(job) for job in jobs), return_exceptions=True)
    enriched: dict[str, Job] = {}
    errors = []
    for job, found in zip(jobs, results, strict=True):
        if isinstance(found, BaseException):
            errors.append(found)
            continue
        posted = found.get("posted_at")
        if posted and job.posted_at and posted.date() == job.posted_at.date():
            del found["posted_at"]  # same day: the feed's time is more precise than the page's date
        enriched[job.key] = job.model_copy(update=found)
    if errors:
        log.warning("%d of %d job pages failed, e.g. %s", len(errors), len(jobs), describe_error(errors[0]))
    return enriched
