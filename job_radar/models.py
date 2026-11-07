"""Data shared by every stage: a normalised job posting and a job under evaluation."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Self

from pydantic import BaseModel


class Job(BaseModel):
    """One posting from any board."""

    source: str
    id: str
    title: str
    url: str
    company: str = ""
    location: str = ""                 # free text, as the board shows it
    countries: list[str] = []          # allowed countries, when the board lists them
    utc_offsets: list[float] = []      # allowed time zones, when the board lists them
    tags: list[str] = []
    description: str = ""              # plain text
    posted_at: datetime | None = None  # timezone-aware
    salary: str = ""
    employment: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def fingerprint(self) -> str:
        """The same company and title on another board counts as the same job."""
        if not self.company:
            return self.key
        return f"{_normalise(self.company)}|{_normalise(self.title)}"


def _normalise(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


@dataclass
class Candidate:
    """A job going through screening and ranking."""

    job: Job
    score: int = 0
    notes: list[str] = field(default_factory=list)  # how the score was built, for tuning
    rejected: str = ""                               # "<rule>: <detail>" once screened out
    fit: int | None = None                           # LLM verdict, 0-10
    reason: str = ""                                 # LLM's one-line explanation

    def add(self, points: int, note: str) -> None:
        self.score += points
        self.notes.append(f"{points:+d} {note}")

    def reject(self, rule: str, detail: str) -> Self:
        self.rejected = f"{rule}: {detail}"
        return self
