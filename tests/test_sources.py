from datetime import UTC, datetime
from xml.etree import ElementTree

import pytest

from job_radar.sources import (
    format_salary,
    parse_date,
    parse_dou,
    parse_himalayas,
    parse_hn_comments,
    parse_remoteok,
    split_dou_title,
    split_wwr_title,
    strip_html,
)

NOON = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)


def test_strip_html():
    assert strip_html("<p>Hello&nbsp;<b>world</b> &amp; co</p>") == "Hello world & co"
    assert strip_html("&lt;p&gt;Remote, async&lt;/p&gt;") == "Remote, async"  # escaped twice
    assert strip_html(None) == ""


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
    assert (job.title, job.company, job.location) == ("Node.js Developer", "Acme", "віддалено")
    assert (job.id, job.description, job.posted_at) == (
        "https://jobs.dou.ua/companies/acme/vacancies/123/", "TypeScript, NestJS", NOON
    )
