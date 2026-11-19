"""Data shared by every stage: a normalised job posting and a job under evaluation."""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Self

from pydantic import BaseModel

SECTIONS = ("remote", "cyprus", "gigs")  # digest sections, in the order they're sent


class Job(BaseModel):
    """One posting from any board."""

    source: str
    id: str
    title: str
    url: str
    company: str = ""
    location: str = ""                  # free text, as the board shows it
    countries: list[str] = []           # allowed countries, when the board lists them
    utc_offsets: list[float] = []       # allowed time zones, when the board lists them
    tags: list[str] = []
    description: str = ""               # plain text; line breaks kept
    posted_at: datetime | None = None   # timezone-aware; first publication, not the latest renewal
    salary: str = ""
    employment: str = ""
    track: str = "remote"               # "remote" or "cyprus": which rules and digest section apply
    work_mode: str = ""                 # "remote", "hybrid" or "on-site", when the board says
    level: str = ""                     # seniority as the board labels it, e.g. "Senior-Level"
    min_years: int | None = None        # years of experience from the board's own field
    reply_rate: str = ""                # how often the recruiter replies, as the board rates it
    last_reply: datetime | None = None  # when the recruiter last replied to anyone

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
    notes: list[str] = field(default_factory=list)     # how the score was built, for tuning
    rejected: str = ""                                  # "<rule>: <detail>" once screened out
    gig: bool = False                                   # paid-per-task side work, e.g. AI training
    bonus: list[str] = field(default_factory=list)     # terms that raise the fit, e.g. "stripe"
    fit_bonus: int = 0                                  # points they add to a passing fit
    warnings: list[str] = field(default_factory=list)  # each one takes a point off the fit
    fit: int | None = None                              # final fit, 0-10
    reason: str = ""                                    # LLM's one-line explanation

    @property
    def section(self) -> str:
        """Digest section: "remote", "gigs" or "cyprus"."""
        return "gigs" if self.gig else self.job.track

    def add(self, points: int, note: str) -> None:
        self.score += points
        self.notes.append(f"{points:+d} {note}")

    def reject(self, rule: str, detail: str) -> Self:
        self.rejected = f"{rule}: {detail}"
        return self


def by_section(cands: Iterable[Candidate]) -> dict[str, list[Candidate]]:
    """Candidates grouped by digest section, in SECTIONS order; each keeps its own order."""
    groups: dict[str, list[Candidate]] = {name: [] for name in SECTIONS}
    for cand in cands:
        groups.setdefault(cand.section, []).append(cand)
    return groups
