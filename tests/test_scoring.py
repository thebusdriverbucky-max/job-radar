from pathlib import Path

import pytest

from job_radar.config import Config, load_config
from job_radar.models import Job
from job_radar.scoring import find_any, region_verdict, required_years, screen

ROOT = Path(__file__).resolve().parent.parent

# A small fixed config, so tuning config.toml never breaks these tests.
CONFIG = Config.model_validate({
    "run": {"min_score": 6},
    "keywords": [
        {"terms": ["typescript"], "weight": 3},
        {"terms": ["react"], "weight": 3},
        {"terms": ["node.js", "node"], "weight": 2},
    ],
    "title": {"require_any": ["developer", "engineer"], "reject_any": ["java", "ios"], "rescue_any": ["react"]},
    "location": {
        "ok": ["worldwide", "europe", "cyprus"],
        "blocked": ["us", "usa"],
        "bonus": 2,
        "utc_offsets": [-1, 5],
        "reject_phrases": ["us only", "fluent german"],
    },
    "seniority": {"reject_titles": ["principal"], "penalty_titles": ["senior"], "title_penalty": 3,
                  "max_years": 4, "years_penalty": 4},
})


def job(title="React Developer", location="Worldwide", description="TypeScript and Node.js", **extra) -> Job:
    return Job(source="test", id=title, title=title, url="https://example.com",
               location=location, description=description, **extra)


def test_repo_config_loads():
    cfg = load_config(ROOT / "config.toml")
    assert cfg.keywords and cfg.title.require_any and cfg.sources.wwr_feeds


def test_find_any_matches_whole_words_only():
    assert find_any("JavaScript developer", ["java"]) is None
    assert find_any("Maintain the API", ["ai"]) is None
    assert find_any("Build AI agents", ["ai"]) == "ai"
    assert find_any("Next.js and C# backend", ["c#"]) == "c#"
    assert find_any("Design REST APIs", ["api"]) == "api"


@pytest.mark.parametrize(("text", "years"), [
    ("5+ years of experience with React", 5),
    ("3-5 years of professional experience", 3),
    ("At least 4 years of hands-on experience", 4),
    ("Experience: 6+ years in web development", 6),
    ("Досвід від 3 років з Node.js", 3),
    ("Опыт от 2-х лет", 2),
    ("2+ years of experience; 7 years of experience is a plus", 7),
    ("We have 20 years of experience in fintech", None),
    ("Great team, flexible hours", None),
])
def test_required_years(text, years):
    assert required_years(text) == years


def test_good_job_scores_keywords_and_location():
    cand = screen(job(), CONFIG)
    assert not cand.rejected
    assert cand.score == 6 + 3 + 2 + 2  # react in title, typescript and node in text, location


@pytest.mark.parametrize(("title", "rule"), [
    ("Product Manager", "title: not a dev role"),
    ("iOS Engineer", "title: ios"),
    ("Principal Engineer", "seniority: principal"),
])
def test_title_rules(title, rule):
    assert screen(job(title=title), CONFIG).rejected == rule


def test_stack_in_title_rescues_mixed_roles():
    assert not screen(job(title="Java/React Developer"), CONFIG).rejected


def test_phrase_rules_job_out():
    assert screen(job(description="React. US only, sorry."), CONFIG).rejected == "phrase: us only"


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
    assert screen(job(location="USA"), CONFIG).rejected == "region: USA"


def test_seniority_and_years_lower_the_score():
    cand = screen(job(title="Senior React Developer", description="TypeScript, 7+ years of experience"), CONFIG)
    assert cand.rejected == "score: 4"  # 6 react + 3 typescript + 2 location - 3 senior - 4 years
