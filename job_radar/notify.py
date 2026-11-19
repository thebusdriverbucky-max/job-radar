"""Deliver the results: Telegram digest, GitHub job summary and console report."""

import asyncio
import html
import os
from collections import Counter
from dataclasses import dataclass

import httpx

from .models import Candidate, by_section
from .sources import LABELS, SourceResult

TELEGRAM_LIMIT = 3900  # the API allows 4096 characters per message
HEADINGS = {"remote": "Remote", "cyprus": "Cyprus", "gigs": "Gigs"}
_COUNTED = {"remote": ("remote", "remote"), "cyprus": ("Cyprus", "Cyprus"), "gigs": ("gig", "gigs")}


@dataclass
class Summary:
    """What happened in one run."""

    sources: list[SourceResult]
    new: int
    rejected: Counter[str]
    shortlisted: int
    llm_used: bool
    dismissed: int
    digest: list[Candidate]
    enriched: int = 0  # job pages read for what the feeds leave out

    def sections(self) -> list[tuple[str, list[Candidate]]]:
        """The digest by section, empty sections left out."""
        return [(name, cands) for name, cands in by_section(self.digest).items() if cands]

    def counts(self, sep: str = " · ") -> str:
        """"5 remote · 1 Cyprus · 2 gigs", or "" when the digest is empty."""
        return sep.join(_count(name, len(cands)) for name, cands in self.sections())

    def stats(self) -> str:
        boards = ", ".join(f"{s.name} FAILED" if s.error else f"{s.name} {s.count}" for s in self.sources)
        rejected = ", ".join(f"{rule} {n}" for rule, n in self.rejected.most_common()) or "none"
        if self.llm_used:
            llm = f"LLM kept {self.shortlisted - self.dismissed} of {self.shortlisted}"
        else:
            llm = "LLM off, ranked by keywords"
        pages = f" · job pages: {self.enriched}" if self.enriched else ""
        digest = self.counts(sep=" + ") or "empty"
        return f"Boards: {boards}\nNew: {self.new}{pages} · rejected: {rejected} · {llm} · digest: {digest}"


def _count(section: str, n: int) -> str:
    one, many = _COUNTED.get(section, (section, section))
    return f"{n} {one if n == 1 else many}"


def _label(source: str) -> str:
    return LABELS.get(source, source)


def _heading(section: str) -> str:
    return HEADINGS.get(section, section.title())


def _flags(cand: Candidate) -> list[str]:
    """What moved the fit: "+stripe +mcp" for the bonus, then one "⚠ …" per warning."""
    flags = [" ".join(f"+{term}" for term in cand.bonus)] if cand.bonus else []
    return flags + [f"⚠ {warning}" for warning in cand.warnings]


# --- Telegram --------------------------------------------------------------------

def telegram_messages(summary: Summary) -> list[str]:
    sections = summary.sections()
    blocks = [f"<b>Job Radar</b> · {summary.counts() or 'nothing new matched this time'}"]
    for name, cands in sections:
        for n, cand in enumerate(cands, 1):
            block = _telegram_job(n, cand)
            if n == 1 and len(sections) > 1:
                block = f"<b>{_heading(name)}</b>\n\n{block}"  # one block, so a message never ends on a heading
            blocks.append(block)
    return pack_messages(blocks, TELEGRAM_LIMIT)


def _telegram_job(n: int, cand: Candidate) -> str:
    job, esc = cand.job, html.escape
    location = job.location if job.location != job.title else ""  # HN titles already include it
    mode = job.work_mode if job.track == "cyprus" else ""  # every other job is remote
    details = " · ".join(x for x in (job.company, location[:60], mode, job.employment, job.salary) if x)
    rating = f"fit {cand.fit}/10" if cand.fit is not None else f"score {cand.score}"
    lines = [f'<b>{n}.</b> <a href="{esc(job.url)}">{esc(job.title)}</a>']
    if details:
        lines.append(esc(details))
    lines.append(esc(" · ".join([_label(job.source), rating, *_flags(cand)])))
    if cand.reason:
        lines.append(f"<i>{esc(cand.reason)}</i>")
    return "\n".join(lines)


def pack_messages(blocks: list[str], limit: int) -> list[str]:
    """Join blocks into as few messages as possible without splitting a block."""
    messages: list[str] = []
    for block in blocks:
        if messages and len(messages[-1]) + 2 + len(block) <= limit:
            messages[-1] += "\n\n" + block
        else:
            messages.append(block)
    return messages


async def send_telegram(client: httpx.AsyncClient, messages: list[str]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or use --dry-run")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, text in enumerate(messages):
        if i:
            await asyncio.sleep(1)  # Telegram allows about one message per second per chat
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }
        resp = await _post(client, url, payload)
        if resp.status_code == 429:
            await asyncio.sleep(resp.json().get("parameters", {}).get("retry_after", 5))
            resp = await _post(client, url, payload)
        if not resp.is_success:
            # Not raise_for_status(): its message includes the URL, and the URL holds the token.
            raise RuntimeError(f"Telegram API {resp.status_code}: {resp.text[:200]}")


async def _post(client: httpx.AsyncClient, url: str, payload: dict) -> httpx.Response:
    try:
        return await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        # Report the error type only, so the token in the URL can't reach the logs.
        raise RuntimeError(f"Telegram request failed: {type(exc).__name__}") from None


# --- Reports -----------------------------------------------------------------------

def console_report(summary: Summary, verbose: bool = False) -> str:
    lines = [summary.stats()]
    for name, cands in summary.sections():
        lines += ["", f"{_heading(name)}:"]
        for n, cand in enumerate(cands, 1):
            job = cand.job
            fit = f"{cand.fit:>2}" if cand.fit is not None else " -"
            lines.append(f"{n:>2}. fit {fit} · score {cand.score:>2} · {job.title} — {job.company or '?'}"
                         f" [{_label(job.source)}] {job.location[:40]}")
            lines.append(f"    {job.url}")
            if flags := _flags(cand):
                lines.append(f"    {' · '.join(flags)}")
            if cand.reason:
                lines.append(f"    {cand.reason}")
            if verbose:
                lines.append(f"    {' '.join(cand.notes)}")
    return "\n".join(lines)


def markdown_report(summary: Summary) -> str:
    lines = [f"### Job Radar: {summary.counts() or 'nothing new'}", "", "```", summary.stats(), "```", ""]
    if summary.digest:
        lines += [
            "| Section | # | Fit | Score | Job | Board | Location | Flags |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for name, cands in summary.sections():
            for n, cand in enumerate(cands, 1):
                job = cand.job
                title = _md(f"{job.title} — {job.company}" if job.company else job.title)
                fit = cand.fit if cand.fit is not None else "–"
                lines.append(f"| {_heading(name)} | {n} | {fit} | {cand.score} | [{title}]({job.url})"
                             f" | {_label(job.source)} | {_md(job.location[:40])} | {_md(' · '.join(_flags(cand)))} |")
    return "\n".join(lines) + "\n"


def _md(text: str) -> str:
    return text.replace("|", "\\|").replace("[", "(").replace("]", ")").replace("\n", " ")


def write_github_summary(markdown: str) -> None:
    """Show the report on the workflow run page (no-op outside GitHub Actions)."""
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown)
