"""Typed view of config.toml, so a typo fails loudly instead of silently changing results."""

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunConfig(Section):
    max_age_days: int = 3
    min_score: int = 6
    max_results: int = 15
    notify_empty: bool = False


class ProfileConfig(Section):
    summary: str = ""
    reason_language: str = "English"


class KeywordGroup(Section):
    terms: list[str]
    weight: int = 1
    fit_bonus: int = 0  # added to the LLM fit when the job passes anyway


class TitleConfig(Section):
    require_any: list[str] = []
    reject_any: list[str] = []
    rescue_any: list[str] = []


class LocationConfig(Section):
    ok: list[str] = []
    blocked: list[str] = []
    bonus: int = 2
    utc_offsets: tuple[float, float] = (-12, 14)
    reject_phrases: list[str] = []


class SeniorityConfig(Section):
    reject_titles: list[str] = []
    ceiling_titles: list[str] = []
    lower_titles: list[str] = []
    max_years: int = 99


class GigsConfig(Section):
    companies: list[str] = []
    phrases: list[str] = []
    min_score: int = 2
    max_jobs: int = 3
    max_results: int = 5


class CyprusConfig(Section):
    local: list[str] = []
    elsewhere: list[str] = []
    country: list[str] = []
    remote_words: list[str] = []
    office_words: list[str] = []
    min_score: int = 3
    max_jobs: int = 5
    max_results: int = 8


class SourcesConfig(Section):
    disabled: list[str] = []
    himalayas_queries: list[str] = []
    himalayas_country: str = ""
    wwr_feeds: list[str] = []
    dou_feeds: list[str] = []
    dou_cyprus_feeds: list[str] = []
    djinni_feeds: list[str] = []
    ergodotisi_urls: list[str] = []
    telegram_channels: list[str] = []
    telegram_pages: int = 4
    detail_pages: int = 40


class LlmConfig(Section):
    base_url: str = "https://api.groq.com/openai/v1"
    model: str = "openai/gpt-oss-120b"
    reasoning_effort: str = ""
    max_jobs: int = 15
    min_fit: int = 5
    description_chars: int = 900
    max_completion_tokens: int = 2000


class Config(Section):
    run: RunConfig = Field(default_factory=RunConfig)
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    keywords: list[KeywordGroup] = []
    title: TitleConfig = Field(default_factory=TitleConfig)
    location: LocationConfig = Field(default_factory=LocationConfig)
    seniority: SeniorityConfig = Field(default_factory=SeniorityConfig)
    gigs: GigsConfig = Field(default_factory=GigsConfig)
    cyprus: CyprusConfig = Field(default_factory=CyprusConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
