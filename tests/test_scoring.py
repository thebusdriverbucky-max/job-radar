from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from job_radar.config import Config, load_config
from job_radar.models import Job
from job_radar.scoring import find_any, near_miss, region_verdict, required_years, screen

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)

# A small fixed config, so tuning config.toml never breaks these tests.
CONFIG = Config.model_validate({
    "run": {"min_score": 6},
    "keywords": [
        {"terms": ["typescript"], "weight": 3},
        {"terms": ["react"], "weight": 3},
        {"terms": ["node.js", "node"], "weight": 2},
        {"terms": ["stripe", "webhook"], "weight": 2, "fit_bonus": 1},
    ],
    "title": {
        "require_any": ["developer", "engineer"],
        "reject_any": ["java", "ios", "react native"],
        "rescue_any": ["react"],
    },
    "location": {
        "ok": ["worldwide", "europe", "cyprus"],
        "blocked": ["us", "usa"],
        "bonus": 2,
        "utc_offsets": [-1, 5],
        "reject_phrases": ["us only", "fluent german"],
    },
    "seniority": {
        "reject_titles": ["principal"],
        "ceiling_titles": ["senior", "lead"],
        "lower_titles": ["junior", "middle", "mid"],
        "max_years": 3,
    },
    "gigs": {"companies": ["micro1"], "phrases": ["ai trainer"], "min_score": 2},
    "cyprus": {
        "local": ["larnaca"],
        "elsewhere": ["limassol"],
        "country": ["cyprus"],
        "remote_words": ["remote"],
        "office_words": ["office", "on-site", "hybrid"],
        "min_score": 3,
    },
})


def job(title="React Developer", location="Worldwide", description="TypeScript and Node.js", **extra) -> Job:
    return Job(source="test", id=title, title=title, url="https://example.com",
               location=location, description=description, **extra)


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.keywords and cfg.title.require_any and cfg.sources.wwr_feeds and cfg.cyprus.local


def test_find_any_matches_whole_words_only():
    assert find_any("JavaScript developer", ["java"]) is None
    assert find_any("Maintain the API", ["ai"]) is None
    assert find_any("Build AI agents", ["ai"]) == "ai"
    assert find_any("Next.js and C# backend", ["c#"]) == "c#"
    assert find_any("Design REST APIs", ["api"]) == "api"


@pytest.mark.parametrize(("text", "years"), [
    ("5+ years of experience with React", 5),
    ("3-5 years of professional experience", 3),
    ("4-6 years experience", 4),
    ("At least 4 years of hands-on experience", 4),
    ("Minimum of 5 years in web development", 5),
    ("Experience: 6+ years in web development", 6),
    ("2 years or more of commercial experience", 2),
    ("Досвід від 3 років з Node.js", 3),
    ("Досвід роботи від 4+ років", 4),
    ("Комерційний досвід 2-3 роки", 2),
    ("React 2+ роки", 2),
    ("Опыт от 2-х лет", 2),
    ("Опыт работы с React от 3 лет", 3),
    # Years that are only a wish don't count.
    ("2+ years of experience; 7 years of experience is a plus", 2),
    ("Ideally 5+ years of experience with React", None),
    ("3+ years of experience with Node.js (preferred)", None),
    # Neither do the company's own years.
    ("We have 20 years of experience in fintech", None),
    ("Our company has been 12 years on the market", None),
    ("We were founded 5 years ago", None),
    ("Great team, flexible hours", None),
])
def test_required_years(text, years):
    assert required_years(text) == years


def test_good_job_scores_keywords_and_location():
    cand = screen(job(), CONFIG, NOW)
    assert not cand.rejected
    assert cand.score == 6 + 3 + 2 + 2  # react in title, typescript and node in text, location


@pytest.mark.parametrize(("title", "rule"), [
    ("Product Manager", "title: not a dev role"),
    ("iOS Engineer", "title: ios"),
    ("React Native Developer", "title: react native"),
    ("Principal Engineer", "level: principal"),
    ("Senior React Developer", "level: senior"),
    ("Team Lead, React", "title: not a dev role"),
    ("Lead React Developer", "level: lead"),
])
def test_title_rules(title, rule):
    assert screen(job(title=title), CONFIG, NOW).rejected == rule


def test_stack_in_title_rescues_mixed_roles():
    assert not screen(job(title="Java/React Developer"), CONFIG, NOW).rejected


def test_a_lower_level_in_the_title_lets_the_llm_decide():
    assert not screen(job(title="Middle/Senior React Developer"), CONFIG, NOW).rejected


def test_board_level_counts_too():
    assert screen(job(level="Senior-Level"), CONFIG, NOW).rejected == "level: senior (board level)"
    assert not screen(job(level="Mid-Level, Senior-Level"), CONFIG, NOW).rejected


def test_years_from_the_text_or_the_board():
    assert screen(job(description="TypeScript, 5+ years of experience"), CONFIG, NOW).rejected == "years: 5"
    assert screen(job(min_years=4), CONFIG, NOW).rejected == "years: 4"
    # The board's own field wins over the text.
    assert not screen(job(description="TypeScript, 5+ years of experience", min_years=2), CONFIG, NOW).rejected


def test_gigs_skip_the_level_and_years_rules():
    cand = screen(job(title="Senior Software Engineer - AI Trainer", company="micro1",
                      description="React. 5+ years of experience"), CONFIG, NOW)
    assert not cand.rejected and cand.gig and cand.section == "gigs"


def test_phrase_rules_job_out():
    assert screen(job(description="React. US only, sorry."), CONFIG, NOW).rejected == "phrase: us only"


@pytest.mark.parametrize(("location", "verdict"), [
    ("Worldwide", True),
    ("Europe, Americas", True),
    ("USA", False),
    ("Remote", None),
])
def test_region_from_location_text(location, verdict):
    assert region_verdict(job(location=location), CONFIG.location) is verdict


def test_region_from_country_and_time_zone_lists():
    loc = CONFIG.location
    assert region_verdict(job(location="", countries=["Cyprus", "Greece"]), loc) is True
    assert region_verdict(job(location="", countries=["United States"]), loc) is False
    assert region_verdict(job(location="", utc_offsets=[-8, -5]), loc) is False
    assert region_verdict(job(location="", utc_offsets=[-5, 0, 2]), loc) is None


def test_blocked_region_rejects():
    assert screen(job(location="USA"), CONFIG, NOW).rejected == "region: USA"


def test_remote_track_needs_fully_remote():
    assert screen(job(work_mode="hybrid"), CONFIG, NOW).rejected == "format: hybrid"


@pytest.mark.parametrize(("location", "mode", "text", "rule"), [
    ("Larnaca", "on-site", "", ""),                     # near home: any format
    ("Limassol", "remote", "", ""),                     # elsewhere: remote only
    ("Limassol", "hybrid", "", "format: hybrid in limassol"),
    ("Limassol", "", "Work from our office.", "format: on-site in limassol"),  # format from the text
    ("Limassol", "", "", ""),                           # format unknown: the LLM reads the posting
    ("", "", "", "format: unknown"),                    # not even a place
])
def test_cyprus_jobs_need_remote_unless_near_home(location, mode, text, rule):
    cyprus_job = job(location=location, work_mode=mode, description=f"TypeScript. {text}", track="cyprus")
    assert screen(cyprus_job, CONFIG, NOW).rejected == rule


def test_bonus_terms_and_reply_warnings():
    cand = screen(job(description="TypeScript, Stripe webhooks", reply_rate="low",
                      last_reply=NOW - timedelta(days=40)), CONFIG, NOW)
    assert not cand.rejected
    assert cand.bonus == ["stripe"] and cand.fit_bonus == 1
    assert cand.warnings == ["low reply rate", "no reply in 30+ days"]


def test_near_miss_is_short_only_by_what_the_page_may_add():
    def miss(**fields) -> bool:
        return near_miss(screen(job(**{"title": "Software Engineer", "location": "", **fields}), CONFIG, NOW), CONFIG)

    assert miss(description="React and Node")             # 5 points: the location bonus would make it 7
    assert not miss(description="React")                  # 3 + 2 is still short
    assert not miss(description="React. US only")         # ruled out for good
    assert miss(location="Larnaca", description="", track="cyprus")  # Cyprus pages add the full text
