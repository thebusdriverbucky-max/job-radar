"""Second opinion from an LLM: one batched request rates every shortlisted job against the profile.

Any OpenAI-compatible API works. Without LLM_API_KEY, or if the call fails, the caller
falls back to keyword scores, so the digest still goes out. LLM_PROFILE, when set, replaces
[profile].summary, so details you'd rather not commit can live in a secret.
"""

import asyncio
import json
import logging
import os
import re

import httpx
from pydantic import BaseModel, Field, field_validator

from .config import Config
from .models import Candidate

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You screen job postings for one candidate.

Candidate:
{profile}

Rate each job's fit from 0 to 10: 8-10 strong match on stack, level and location; 5-7 worth applying; 0-4 skip.
Give 0-2 when the job is on-site or hybrid, needs relocation, a location or work permit the candidate lacks, \
a language they don't speak, a main stack outside JavaScript/TypeScript/Python, or far more experience than they have.
"reason": in {language}, at most 15 words, the deciding factor.
Reply with JSON only: {{"jobs": [{{"id": "<id>", "fit": <0-10>, "reason": "<text>"}}]}} with one entry per job."""

# Where postings usually list what they need; the opening lines are mostly about the company.
_REQUIREMENTS = re.compile(
    r"requirements|qualifications|you have|you'll need|you will need|what we.re looking for|"
    r"must have|skills|вимоги|требования",
    re.IGNORECASE,
)


class Verdict(BaseModel):
    id: str
    fit: int = Field(ge=0, le=10)
    reason: str = ""

    @field_validator("fit", mode="before")
    @classmethod
    def _round(cls, value: object) -> int:
        # Models sometimes answer 7.5 or "8".
        return max(0, min(10, round(float(value))))


class Verdicts(BaseModel):
    jobs: list[Verdict]


def excerpt(text: str, limit: int) -> str:
    """The start of a posting plus its requirements, within `limit` characters."""
    if len(text) <= limit:
        return text
    head = limit // 3
    match = _REQUIREMENTS.search(text, head)
    if not match:
        return text[:limit]
    return f"{text[:head]} … {text[match.start():match.start() + limit - head]}"


async def rank(client: httpx.AsyncClient, shortlist: list[Candidate], cfg: Config) -> dict[str, Verdict] | None:
    """Verdicts keyed by job key; None when the LLM is off or failed."""
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        return None
    if not shortlist:
        return {}
    by_id = {f"j{i}": cand for i, cand in enumerate(shortlist, 1)}  # short ids save tokens
    jobs = [
        {
            "id": job_id,
            "title": cand.job.title,
            "company": cand.job.company,
            "location": cand.job.location[:120],
            "type": cand.job.employment,
            "salary": cand.job.salary,
            "text": excerpt(cand.job.description, cfg.llm.description_chars),
        }
        for job_id, cand in by_id.items()
    ]
    profile = os.environ.get("LLM_PROFILE") or cfg.profile.summary
    system = SYSTEM_PROMPT.format(profile=profile.strip(), language=cfg.profile.reason_language)
    body: dict = {
        "model": cfg.llm.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(jobs, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": cfg.llm.max_completion_tokens,
    }
    if cfg.llm.reasoning_effort:
        body["reasoning_effort"] = cfg.llm.reasoning_effort
    try:
        content = await _complete(client, cfg.llm.base_url, api_key, body)
        verdicts = Verdicts.model_validate_json(content)
    except httpx.HTTPStatusError as exc:
        log.warning("LLM ranking skipped: HTTP %s %s", exc.response.status_code, exc.response.text[:300])
        return None
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        log.warning("LLM ranking skipped: %s: %s", type(exc).__name__, str(exc)[:300])
        return None
    return {by_id[v.id].job.key: v for v in verdicts.jobs if v.id in by_id}


async def _complete(client: httpx.AsyncClient, base_url: str, api_key: str, body: dict) -> str:
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    resp = await client.post(url, json=body, headers=headers, timeout=90)
    if resp.status_code == 429 and (wait := _retry_after(resp)) is not None and wait <= 30:
        log.info("LLM rate-limited, retrying in %.0fs", wait)
        await asyncio.sleep(wait)
        resp = await client.post(url, json=body, headers=headers, timeout=90)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _retry_after(resp: httpx.Response) -> float | None:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


def apply(
    shortlist: list[Candidate], verdicts: dict[str, Verdict], min_fit: int
) -> tuple[list[Candidate], list[Candidate]]:
    """Attach verdicts and split the shortlist into kept (best first) and dismissed."""
    kept: list[Candidate] = []
    dismissed: list[Candidate] = []
    for cand in shortlist:
        verdict = verdicts.get(cand.job.key)
        if verdict:
            cand.fit, cand.reason = verdict.fit, verdict.reason
        if verdict and verdict.fit < min_fit:
            dismissed.append(cand)
        else:
            kept.append(cand)  # a job the model skipped stays in, ranked last
    kept.sort(key=lambda c: (c.fit if c.fit is not None else -1, c.score), reverse=True)
    return kept, dismissed
