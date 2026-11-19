import json
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import pytest

from job_radar.sources import (
    dou_countries,
    format_salary,
    html_to_text,
    parse_ago,
    parse_date,
    parse_day,
    parse_djinni_page,
    parse_dou,
    parse_ergodotisi,
    parse_ergodotisi_page,
    parse_himalayas,
    parse_hn_comments,
    parse_jobicy,
    parse_remoteok,
    parse_telegram,
    split_dou_salary,
    split_dou_title,
    split_wwr_title,
    strip_html,
)

NOON = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b> &amp; co</p>") == "Hello world & co"
    assert strip_html("&lt;p&gt;Remote, async&lt;/p&gt;") == "Remote, async"  # escaped twice
    assert strip_html(None) == ""


def test_html_to_text_keeps_paragraphs_and_list_items():
    assert html_to_text("<p>Hi&nbsp;there</p><ul><li>React</li><li>Node</li></ul>") == "Hi there\nReact\nNode"


@pytest.mark.parametrize("value", [
    int(NOON.timestamp()),
    str(int(NOON.timestamp())),
    "2026-10-30T12:00:00Z",
    "2026-10-30T12:00:00",  # naive means UTC
    "2026-10-30 14:00:00+02:00",
    "Fri, 30 Oct 2026 12:00:00 +0000",
    "Fri, 30 Oct 2026 12:00:00 GMT",
])
def test_parse_date(value):
    assert parse_date(value) == NOON


@pytest.mark.parametrize("value", [None, "", "soon", "32/13/2026"])
def test_parse_date_returns_none_for_garbage(value):
    assert parse_date(value) is None


def test_format_salary():
    assert format_salary(80000, 100000) == "$80k–100k"
    assert format_salary(None, 120000, "EUR") == "€120k"
    assert format_salary(0, 0) == ""
    assert format_salary("n/a", None) == ""


def test_split_titles():
    assert split_wwr_title("Acme Inc: Engineer: Platform") == ("Acme Inc", "Engineer: Platform")
    assert split_wwr_title("No company") == ("", "No company")
    assert split_dou_title("Full Stack Developer в Acme, віддалено") == ("Full Stack Developer", "Acme", "віддалено")
    assert split_dou_title("Розробник в команду AI в Acme, Київ, віддалено") == (
        "Розробник в команду AI", "Acme", "Київ, віддалено"
    )
    assert split_dou_salary("$3000–10000, Київ, віддалено") == ("$3000–10000", "Київ, віддалено")
    assert split_dou_salary("Київ, віддалено") == ("", "Київ, віддалено")


def test_parse_remoteok_skips_terms_notice():
    data = [
        {"legal": "API terms"},
        {"id": "123", "position": "React Developer", "company": "Acme", "epoch": 1793361600,
         "salary_min": 60000, "salary_max": 90000, "description": "<p>Hi</p>"},
    ]
    [job] = parse_remoteok(data)
    assert (job.key, job.url, job.salary, job.description) == (
        "remoteok:123", "https://remoteok.com/remote-jobs/123", "$60k–90k", "Hi"
    )


def test_parse_himalayas_reads_location_restrictions():
    link = "https://himalayas.app/companies/acme/jobs/dev"
    data = {"jobs": [
        {"guid": link, "applicationLink": f"{link}?utm_source=x", "title": "Frontend Engineer",
         "companyName": "Acme", "locationRestrictions": ["Cyprus", "Greece"], "timezoneRestrictions": [2, 3],
         "pubDate": 1793361600, "minSalary": 50000, "maxSalary": 70000, "currency": "EUR"},
        {"guid": f"{link}-2", "title": "Backend Engineer", "companyName": "Beta", "locationRestrictions": []},
    ]}
    cyprus, anywhere = parse_himalayas(data)
    assert (cyprus.url, cyprus.countries, cyprus.utc_offsets) == (link, ["Cyprus", "Greece"], [2, 3])
    assert (cyprus.location, cyprus.salary) == ("Cyprus, Greece", "€50k–70k")
    assert (anywhere.countries, anywhere.location) == ([], "Worldwide")


def test_parse_hn_comments_keeps_top_level_remote_posts():
    data = {"hits": [
        {"objectID": "1", "parent_id": 100, "created_at_i": 1793361600,
         "comment_text": "Acme | Full Stack Engineer | REMOTE (EU)<p>We use TypeScript &amp; React."},
        {"objectID": "2", "parent_id": 100, "comment_text": "Beta | Engineer | NYC<p>Office only."},
        {"objectID": "3", "parent_id": 1, "comment_text": "Is this remote?"},
        {"objectID": "4", "parent_id": 100, "comment_text": None},
    ]}
    [job] = parse_hn_comments(data, "100")
    assert (job.title, job.company) == ("Acme | Full Stack Engineer | REMOTE (EU)", "Acme")
    assert job.url == "https://news.ycombinator.com/item?id=1"
    assert "TypeScript & React" in job.description


def test_parse_dou_feed():
    rss = """<rss><channel><item>
      <title>Node.js Developer в Acme, віддалено</title>
      <link>https://jobs.dou.ua/companies/acme/vacancies/123/?utm_source=jobsrss</link>
      <description>&lt;p&gt;TypeScript, NestJS&lt;/p&gt;</description>
      <pubDate>Fri, 30 Oct 2026 14:00:00 +0200</pubDate>
    </item></channel></rss>"""
    [job] = parse_dou(ElementTree.fromstring(rss).findall(".//item"))
    assert (job.title, job.company, job.location, job.salary) == ("Node.js Developer", "Acme", "віддалено", "")
    assert (job.id, job.description, job.posted_at) == (
        "https://jobs.dou.ua/companies/acme/vacancies/123/", "TypeScript, NestJS", NOON
    )


def test_parse_dou_unescapes_title_and_moves_salary():
    rss = """<rss><channel><item>
      <title>Full-Stack Dev &amp;amp; AI в Acme, до&amp;nbsp;$3000, віддалено</title>
      <link>https://jobs.dou.ua/companies/acme/vacancies/124/</link>
    </item></channel></rss>"""
    [job] = parse_dou(ElementTree.fromstring(rss).findall(".//item"))
    assert (job.title, job.company, job.salary, job.location) == ("Full-Stack Dev & AI", "Acme", "до $3000", "віддалено")


def test_parse_jobicy_reads_salary_period_and_level():
    data = {"jobs": [
        {"id": 7, "jobTitle": "Full Stack Engineer", "companyName": "Acme", "url": "https://jobicy.com/jobs/7",
         "jobGeo": "EMEA", "jobLevel": "Senior", "jobType": ["full-time"], "pubDate": "2026-10-30 12:00:00",
         "salaryMin": 4000, "salaryMax": 5000, "salaryCurrency": "EUR", "salaryPeriod": "monthly",
         "jobDescription": "<p>React</p>"},
        {"id": 8, "jobTitle": "Developer", "url": "https://jobicy.com/jobs/8", "jobLevel": "Any"},
    ]}
    senior, anyone = parse_jobicy(data)
    assert (senior.salary, senior.level, senior.employment, senior.posted_at) == (
        "€4k–5k monthly", "Senior", "full-time", NOON
    )
    assert (anyone.level, anyone.salary) == ("", "")


@pytest.mark.parametrize(("location", "countries"), [
    ("віддалено", ["Україна"]),
    ("Київ, віддалено", ["Україна"]),
    ("за кордоном, віддалено", []),  # abroad: the location text decides
    ("Київ, Варшава (Польща), віддалено", ["Україна", "Польща"]),
    ("Ларнака (Кіпр)", ["Кіпр"]),
    ("", []),
])
def test_dou_countries(location, countries):
    assert dou_countries(location) == countries


def test_parse_dou_dates_a_renewed_vacancy_by_its_id():
    rss = """<rss><channel>
      <item><title>Dev в Acme, віддалено</title><link>https://jobs.dou.ua/companies/acme/vacancies/350000/</link>
        <pubDate>Fri, 30 Oct 2026 12:00:00 +0000</pubDate></item>
      <item><title>Dev в Beta, віддалено</title><link>https://jobs.dou.ua/companies/beta/vacancies/349000/</link>
        <pubDate>Fri, 30 Oct 2026 10:00:00 +0000</pubDate></item>
      <item><title>Dev в Gamma, віддалено</title><link>https://jobs.dou.ua/companies/gamma/vacancies/349950/</link>
        <pubDate>Thu, 29 Oct 2026 12:00:00 +0000</pubDate></item>
    </channel></rss>"""
    newest, renewed, recent = parse_dou(ElementTree.fromstring(rss).findall(".//item"))
    assert newest.posted_at == NOON
    assert renewed.posted_at == NOON - timedelta(days=10)  # 1000 ids, about 10 days, older
    assert recent.posted_at == datetime(2026, 10, 29, 12, tzinfo=UTC)  # its date fits its id


@pytest.mark.parametrize(("text", "days"), [
    ("12 days ago", 12), ("an hour ago", 1 / 24), ("a month ago", 30), ("yesterday", 1), ("today", 0),
])
def test_parse_ago(text, days):
    assert parse_ago(text, NOON) == NOON - timedelta(days=days)


@pytest.mark.parametrize(("text", "day"), [
    ("28 September", datetime(2026, 9, 28, 12, tzinfo=UTC)),
    ("30 September 2025", datetime(2025, 9, 30, 12, tzinfo=UTC)),
    ("15 December", datetime(2025, 12, 15, 12, tzinfo=UTC)),  # no year and still ahead: last year
    ("today", NOON),
    ("31 February", None),
    ("soon", None),
])
def test_parse_day(text, day):
    assert parse_day(text, NOON) == day


DJINNI_PAGE = """
<div id="job-publication-info" class="mb-1">
  <div>
    <span class="font-weight-500">Published 30 September 2025</span>
      <span class="middot"> · </span>Updated 30 October
  </div>
</div>
<div>
    Response activity: <span class="fw-medium text-secondary">Low</span>
</div>
<div data-bs-toggle="tooltip" title="21:21 27.08.2026">
    <span class="dot bg-secondary"></span>
 Last responded more than a month ago
</div>
<aside>
  <strong class="font-weight-600 capitalize-first-letter">
      Only
        from
        5 years of experience
  </strong>
  <strong class="d-block font-weight-600">
    <span class="location-text">Countries of Europe or Ukraine</span>
</strong>

      <div class="font-size-extra-small">Countries where we consider candidates</div>
</aside>
<script type="application/ld+json">
  {"@context": "https://schema.org", "@type": "JobPosting", "datePosted": "2026-10-30T09:00:00",
   "hiringOrganization": {"@type": "Organization", "name": "Acme"}}
</script>
"""


def test_parse_djinni_page():
    assert parse_djinni_page(DJINNI_PAGE, NOON) == {
        "min_years": 5,
        "location": "Countries of Europe or Ukraine",
        "countries": ["Countries of Europe or Ukraine"],
        "reply_rate": "low",
        "last_reply": datetime(2026, 8, 27, 21, 21, tzinfo=UTC),
        "posted_at": datetime(2025, 9, 30, 12, tzinfo=UTC),  # first publication, not the renewal
        "company": "Acme",
    }


def test_djinni_years_take_the_lower_bar():
    page = "<aside>Only from 5 years of experience. Considering with 3 years of experience</aside>"
    assert parse_djinni_page(page, NOON) == {"min_years": 3}
    assert parse_djinni_page("<aside>No experience</aside>", NOON) == {"min_years": 0}


ERGODOTISI_PAGE = """
<article class="jobs-card " data-analytics-job-id-value="40356202">
  <a class="jobs-card__ref d-lg-none" href="/en-CY/jobs/react-developer-40356202?utm=x&amp;y=1">View</a>
  <a class="jobs-card__company" aria-label="View company: Acme" href="https://ergodotisi.com/en-CY/companies/acme">
    Acme
</a>      <h3 class="jobs-card__title" aria-label="Vacancy title">React Developer</h3>
  <div class="jobs-card__meta">
    <div class="jobs-card__meta-item featured"><svg></svg><span>€34,000&ndash;45,000 yearly</span></div>
    <div class="jobs-card__meta-item"><span>Larnaca, Cyprus</span></div>
    <div class="jobs-card__meta-item"><span>Hybrid</span></div>
    <div class="jobs-card__meta-item"><span>Full-time</span></div>
    <div class="jobs-card__meta-item"><span>Mid-Level</span></div>
  </div>
  <div class="jobs-card__description">We build fast apps &amp; tools.</div>
  <div class="jobs-card__timestamp "><span>12 days ago</span></div>
</article>
<article class="jobs-card" data-analytics-job-id-value="2">
  <a href="/en-CY/jobs/developer-2">View</a><h3 class="jobs-card__title">Developer</h3>
  <div class="jobs-card__meta-item"><span>€1–1</span></div>
</article>
<article class="jobs-card">an ad, not a job</article>
"""


def test_parse_ergodotisi_cards():
    full, bare = parse_ergodotisi(ERGODOTISI_PAGE, NOON)
    assert (full.key, full.title, full.company, full.url) == (
        "ergodotisi:40356202", "React Developer", "Acme",
        "https://ergodotisi.com/en-CY/jobs/react-developer-40356202?utm=x&y=1",
    )
    assert (full.location, full.work_mode, full.level, full.salary, full.employment) == (
        "Larnaca, Cyprus", "hybrid", "Mid-Level", "€34,000–45,000 yearly", "Full-time"
    )
    assert (full.description, full.posted_at, full.track) == ("We build fast apps & tools.", NOON - timedelta(days=12), "cyprus")
    assert (bare.salary, bare.company, bare.posted_at) == ("", "", None)  # "€1–1" means not disclosed


def test_parse_ergodotisi_page_reads_json_ld():
    ld = {"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": "Ergodotisi"},
        {"@type": "JobPosting", "description": "<p>React &amp; Node</p><p>Office in Larnaca</p>",
         "datePosted": "2026-10-30T12:00:00Z", "hiringOrganization": {"name": "Acme Ltd"},
         "experienceRequirements": {"@type": "OccupationalExperienceRequirements", "monthsOfExperience": 36}},
    ]}
    page = f'<script type="application/ld+json">{json.dumps(ld)}</script>'
    assert parse_ergodotisi_page(page) == {
        "description": "React & Node\nOffice in Larnaca", "posted_at": NOON, "company": "Acme Ltd", "min_years": 3,
    }
    assert parse_ergodotisi_page("<html>no data</html>") == {}


TELEGRAM_PAGE = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="cyithr/20118">
<div class="tgme_widget_message_text js-message_text" dir="auto"><a href="?q=%23vacancy">#vacancy</a> \
<a href="?q=%23frontend">#frontend</a><br/><br/><b><i class="emoji"><b>\N{ROCKET}</b></i> Vacancy: Frontend Developer \
(React) <i class="emoji"><b>\N{ROCKET}</b></i></b><br/>\N{ROUND PUSHPIN} Larnaca, Cyprus<br/>We&#39;re on #TypeScript, \
Next.js and C#&#33;</div>
<time datetime="2026-10-30T12:00:00+00:00" class="time"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="cyithr/20119">
<div class="tgme_widget_message_text js-message_text" dir="auto">Hiring in Limassol<br/>Location: Limassol, office\
<br/>Payroll Accountant</div></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message" data-post="cyithr/20120">
<div class="tgme_widget_message_photo"></div></div></div>
"""


def test_parse_telegram_posts():
    dev, other = parse_telegram(TELEGRAM_PAGE, ["developer", "engineer"])
    assert (dev.key, dev.url, dev.title) == ("telegram:cyithr/20118", "https://t.me/cyithr/20118", "Frontend Developer (React)")
    assert (dev.location, dev.posted_at, dev.track) == ("Larnaca, Cyprus", NOON, "cyprus")
    assert dev.tags == ["vacancy", "frontend", "TypeScript"]
    assert dev.description == "Vacancy: Frontend Developer (React)\nLarnaca, Cyprus\nWe're on TypeScript, Next.js and C#!"
    assert (other.title, other.location, other.posted_at) == ("Hiring in Limassol", "Limassol, office", None)
