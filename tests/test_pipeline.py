import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from job_radar.__main__ import pages_to_read, pick_digest, pick_shortlist, select_new
from job_radar.config import Config
from job_radar.llm import Verdict, apply, excerpt, rank
from job_radar.models import Candidate, Job
from job_radar.notify import Summary, pack_messages, send_telegram, telegram_messages
from job_radar.sources import enrich
from job_radar.state import SeenJobs

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def job(job_id="1", title="React Developer", source="remotive", company="Acme", posted_at=NOW,
        url="https://example.com", **extra) -> Job:
    return Job(source=source, id=job_id, title=title, company=company, url=url, posted_at=posted_at, **extra)


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
    dropped = Counter()
    new = select_new(jobs, seen, since=NOW - timedelta(days=3), dropped=dropped)
    assert [j.id for j in new] == ["fresh", "undated"]
    assert dropped == {"duplicate": 2, "old": 1}


def test_pages_are_read_for_jobs_that_pass_or_nearly_do():
    cfg = Config.model_validate({
        "run": {"min_score": 6},
        "keywords": [
            {"terms": ["react"], "weight": 3},
            {"terms": ["node"], "weight": 2},
            {"terms": ["typescript"], "weight": 1},
        ],
        "title": {"require_any": ["developer"]},
        "location": {"bonus": 2},
        "sources": {"detail_pages": 2},
    })

    def djinni(job_id, title="Developer", description=""):
        return job(job_id, title=title, source="djinni", description=description)

    jobs = [
        djinni("short", description="React"),                # 3: short even with the location bonus
        djinni("ts", description="React and TypeScript"),    # 4: near, but past the page limit
        djinni("close", description="React and Node"),       # 5: near
        djinni("pass", title="React Developer"),             # 6 on the feed alone
        djinni("manager", title="Manager", description="React and Node"),  # no page fixes the title
        job("other", title="React Developer"),               # its board has no pages to read
    ]
    assert [j.id for j in pages_to_read(jobs, cfg, NOW)] == ["pass", "close"]


def test_enrich_reads_pages_and_keeps_jobs_whose_page_failed():
    pages = {
        "/jobs/1/": '<div id="job-publication-info"><span>Published 30 September 2025</span></div>',
        "/jobs/2/": '<div id="job-publication-info"><span>Published 1 November</span></div>',
    }

    def handler(request):
        page = pages.get(request.url.path)
        return httpx.Response(200, text=page) if page else httpx.Response(500)

    later = NOW + timedelta(hours=3)
    jobs = [job(str(n), source="djinni", posted_at=later, url=f"https://djinni.co/jobs/{n}/") for n in (1, 2, 3)]
    enriched = call_with(handler, enrich, jobs, NOW)
    assert list(enriched) == ["djinni:1", "djinni:2"]  # page 3 failed
    assert enriched["djinni:1"].posted_at == datetime(2025, 9, 30, 12, tzinfo=UTC)  # renewed since
    assert enriched["djinni:2"].posted_at == later  # same day: the feed's time is kept


def test_apply_ranks_by_fit_and_splits_off_low_fit():
    a, b, c = (Candidate(job(name, title=name), score=score) for name, score in (("a", 9), ("b", 7), ("c", 8)))
    verdicts = {a.job.key: Verdict(id="j1", fit=3), b.job.key: Verdict(id="j2", fit=8)}
    kept, dismissed = apply([a, b, c], verdicts, min_fit=5)
    assert [k.job.id for k in kept] == ["b", "c"]  # c got no verdict, so it goes last
    assert [d.job.id for d in dismissed] == ["a"] and a.fit == 3


def test_apply_adds_the_bonus_to_passing_jobs_and_takes_off_warnings():
    a = Candidate(job("a"), fit_bonus=2)                                  # 6 + 2
    b = Candidate(job("b", title="B"), fit_bonus=2)                       # 4: no bonus below min_fit
    c = Candidate(job("c", title="C"), warnings=["low reply rate"])       # 5 - 1
    d = Candidate(job("d", title="D"), fit_bonus=3)                       # 9 + 3, capped
    verdicts = {cand.job.key: Verdict(id="j", fit=fit) for cand, fit in ((a, 6), (b, 4), (c, 5), (d, 9))}
    kept, dismissed = apply([a, b, c, d], verdicts, min_fit=5)
    assert [(k.job.id, k.fit) for k in kept] == [("d", 10), ("a", 8)]
    assert [(k.job.id, k.fit) for k in dismissed] == [("b", 4), ("c", 4)]


def cyprus(job_id: str) -> Candidate:
    return Candidate(job(job_id, title=job_id, track="cyprus"))


def test_shortlist_keeps_slots_for_cyprus_and_gigs():
    cfg = Config.model_validate({"llm": {"max_jobs": 4}, "cyprus": {"max_jobs": 2}, "gigs": {"max_jobs": 1}})
    remote = [Candidate(job(f"r{i}", title=f"r{i}")) for i in range(5)]
    gigs = [Candidate(job(f"g{i}", title=f"g{i}"), gig=True) for i in range(2)]
    shortlist = pick_shortlist([*gigs, *remote, *(cyprus(f"c{i}") for i in range(3))], cfg)
    assert [c.job.id for c in shortlist] == ["r0", "c0", "c1", "g0"]


def test_digest_takes_each_section_up_to_its_limit():
    cfg = Config.model_validate({"run": {"max_results": 1}, "cyprus": {"max_results": 1}, "gigs": {"max_results": 2}})
    kept = [Candidate(job("g", title="g"), gig=True), Candidate(job("r1", title="r1")), cyprus("c1"),
            Candidate(job("r2", title="r2")), cyprus("c2")]
    assert [c.job.id for c in pick_digest(kept, cfg)] == ["r1", "c1", "g"]


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


def test_telegram_digest_has_sections_and_flags():
    remote = Candidate(job("r"), fit=7, bonus=["stripe", "mcp"], warnings=["low reply rate"])
    local = Candidate(job("c", title="Node Developer", track="cyprus", work_mode="hybrid", location="Larnaca"), fit=6)
    gig = Candidate(job("g", title="AI Trainer"), gig=True, score=3)
    summary = Summary(sources=[], new=3, rejected=Counter(), shortlisted=2, llm_used=False, dismissed=0,
                      digest=[remote, local, gig], enriched=4)
    [message] = telegram_messages(summary)
    assert message.startswith("<b>Job Radar</b> · 1 remote · 1 Cyprus · 1 gig\n\n<b>Remote</b>\n\n<b>1.</b> ")
    assert "fit 7/10 · +stripe +mcp · ⚠ low reply rate" in message
    assert "Acme · Larnaca · hybrid" in message and "score 3" in message
    assert message.index("<b>Cyprus</b>") < message.index("<b>Gigs</b>")
    assert "job pages: 4" in summary.stats() and "digest: 1 remote + 1 Cyprus + 1 gig" in summary.stats()


def test_empty_digest_message():
    summary = Summary(sources=[], new=0, rejected=Counter(), shortlisted=0, llm_used=False, dismissed=0, digest=[])
    assert telegram_messages(summary) == ["<b>Job Radar</b> · nothing new matched this time"]


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


def test_rank_tells_the_llm_what_the_board_says(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"jobs": []}'}}]})

    shortlist = [
        Candidate(job("a", track="cyprus", work_mode="hybrid", level="Mid-Level", min_years=0)),
        Candidate(job("b", title="AI Trainer"), gig=True),
    ]
    call_with(handler, rank, shortlist, Config())
    cyprus_job, gig = json.loads(bodies[0]["messages"][1]["content"])
    assert {k: cyprus_job[k] for k in ("track", "format", "level", "years")} == {
        "track": "cyprus", "format": "hybrid", "level": "Mid-Level", "years": 0,
    }
    assert "gig" not in cyprus_job
    assert gig["gig"] is True and not {"track", "format", "level", "years"} & gig.keys()


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
