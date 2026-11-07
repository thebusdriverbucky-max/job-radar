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
    penalty_titles: list[str] = []
    title_penalty: int = 3
    max_years: int = 99
    years_penalty: int = 4


class SourcesConfig(Section):
    disabled: list[str] = []
    himalayas_queries: list[str] = []
    himalayas_country: str = ""
    wwr_feeds: list[str] = []
    dou_feeds: list[str] = []
    djinni_feeds: list[str] = []


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
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)


def load_config(path: Path) -> Config:
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))
