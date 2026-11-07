import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from job_radar.__main__ import select_new
from job_radar.config import Config
from job_radar.llm import Verdict, apply, excerpt, rank
from job_radar.models import Candidate, Job
from job_radar.notify import Summary, pack_messages, send_telegram, telegram_messages
from job_radar.state import SeenJobs

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def job(job_id="1", title="React Developer", source="remotive", company="Acme", posted_at=NOW) -> Job:
    return Job(source=source, id=job_id, title=title, company=company, url="https://example.com", posted_at=posted_at)


def test_fingerprint_ignores_case_and_punctuation():
    original = job(title="Senior React Developer!")
    repost = job(source="wwr", title="senior  react-developer")
    assert original.fingerprint == repost.fingerprint


def test_select_new_drops_old_seen_and_cross_posted_jobs(tmp_path):
    seen = SeenJobs(tmp_path / "seen.json")
    seen.add([job("seen").key], NOW)
    jobs = [
        job("fresh"),
        job("x", source="wwr", title="React developer"),  # same company and title as "fresh"
        job("old", title="Old", posted_at=NOW - timedelta(days=5)),
        job("seen", title="Seen"),
        job("undated", title="Undated", posted_at=None),
    ]
    new = select_new(jobs, seen, since=NOW - timedelta(days=3))
    assert [j.id for j in new] == ["fresh", "undated"]


def test_apply_ranks_by_fit_and_splits_off_low_fit():
    a, b, c = (Candidate(job(name, title=name), score=score) for name, score in (("a", 9), ("b", 7), ("c", 8)))
    verdicts = {a.job.key: Verdict(id="j1", fit=3), b.job.key: Verdict(id="j2", fit=8)}
    kept, dismissed = apply([a, b, c], verdicts, min_fit=5)
    assert [k.job.id for k in kept] == ["b", "c"]  # c got no verdict, so it goes last
    assert [d.job.id for d in dismissed] == ["a"] and a.fit == 3


def test_verdict_rounds_and_clamps_fit():
    assert Verdict(id="j1", fit="7.6").fit == 8
    assert Verdict(id="j1", fit=12).fit == 10


def test_excerpt_keeps_requirements():
    text = "About us. " * 50 + "Requirements: TypeScript, React." + " Benefits." * 50
    short = excerpt(text, 300)
    assert len(short) <= 303  # head + " … " + tail
    assert short.startswith("About us.") and "Requirements: TypeScript" in short


def test_pack_messages_never_splits_a_block():
    assert pack_messages(["a" * 40, "b" * 40, "c" * 40], 90) == ["a" * 40 + "\n\n" + "b" * 40, "c" * 40]


def test_telegram_message_escapes_html():
    cand = Candidate(job(title="Dev <TS> & AI"), score=9, fit=8, reason="Стек совпадает")
    summary = Summary(sources=[], new=1, rejected=Counter(), shortlisted=1, llm_used=True, dismissed=0, digest=[cand])
    [message] = telegram_messages(summary)
    assert "Dev &lt;TS&gt; &amp; AI" in message and "fit 8/10" in message and "<i>Стек совпадает</i>" in message


def test_seen_jobs_round_trip_and_expiry(tmp_path):
    path = tmp_path / "state" / "seen.json"
    seen = SeenJobs(path)
    seen.add(["old"], NOW - timedelta(days=60))
    seen.add(["new"], NOW)
    seen.save(NOW)
    loaded = SeenJobs.load(path)
    assert "new" in loaded and "old" not in loaded


def test_seen_jobs_survive_a_corrupt_file(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert SeenJobs.load(path).entries == {}


def call_with(handler, func, *args):
    """Run an async function that takes an HTTP client, with `handler` answering every request."""
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await func(client, *args)
    return asyncio.run(go())


def test_rank_sends_one_request_and_maps_verdicts_to_jobs(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        verdicts = {"jobs": [{"id": "j1", "fit": 8, "reason": "Next.js и TypeScript"}, {"id": "j9", "fit": 3}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(verdicts)}}]})

    shortlist = [Candidate(job("a")), Candidate(job("b", title="Other"))]
    verdicts = call_with(handler, rank, shortlist, Config())
    assert list(verdicts) == ["remotive:a"] and verdicts["remotive:a"].fit == 8  # unknown "j9" is ignored
    [body] = bodies
    assert [j["id"] for j in json.loads(body["messages"][1]["content"])] == ["j1", "j2"]


def test_rank_falls_back_when_the_llm_fails(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    assert call_with(lambda request: httpx.Response(500, text="boom"), rank, [Candidate(job())], Config()) is None


def test_telegram_errors_never_include_the_token(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")

    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(RuntimeError) as error:
        call_with(handler, send_telegram, ["hi"])
    assert "SECRET" not in str(error.value)
