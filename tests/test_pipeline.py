"""End-to-end test of the research pipeline with a scripted stand-in for Gemini, so it runs offline
and for free: uv run python -m pytest tests -q   (or: uv run tests/test_pipeline.py)

It checks the plumbing the Deep Research requirements depend on, with the real data and the real
tools: the Clarifier asks about a vague question, can't ask twice in a row, and turns a follow-up
into a full brief using the session's history; sessions don't leak into each other; the Lead fans
out 2+ researchers in one turn and they overlap in time; a follow-up round fills a gap; caps hold;
a failed researcher is reported, not hidden; /chat keeps Project 1's shape with an agent field on
every tool call; tool errors are JSON with a next step; the guardrail stops a filter on a value the
data never holds; and a report citing a wrong number is caught and revised.
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# A throwaway session store, so tests never touch a real conversations.db
os.environ["SESSIONS_DB"] = str(Path(tempfile.mkdtemp()) / "test_sessions.db")

from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from fastapi.testclient import TestClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

import app

INTERVALS: list[tuple[float, float]] = []  # (start, end) of every researcher model call
CLARIFIER_INPUTS: list[list[dict]] = []  # every message list the Clarifier got
VAGUE = ("tell me about the shelter", "doing better")
FAIL_NEXT_RESEARCHER = {"on": False}


def _call(name: str, args: dict) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(type="function_call", call_id=f"c{time.time_ns()}", name=name,
                                    arguments=json.dumps(args), id=f"fc{time.time_ns()}", status="completed")


def _text(text: str) -> ResponseOutputMessage:
    return ResponseOutputMessage(type="message", id=f"m{time.time_ns()}", role="assistant", status="completed",
                                 content=[ResponseOutputText(type="output_text", text=text, annotations=[])])


def _outputs(items) -> list[str]:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]


def _messages(items) -> list[dict]:
    return [i for i in items if isinstance(i, dict) and i.get("role") in ("user", "assistant") and isinstance(i.get("content"), str)]


TASK_SQL = {
    "age": "SELECT age_group, median(days_in_shelter) AS median_days, count(*) AS n FROM stays "
           "WHERE animal_type='Dog' AND outcome_type='Adoption' GROUP BY 1 ORDER BY 1",
    "intake": "SELECT intake_type, median(days_in_shelter) AS median_days, count(*) AS n FROM stays "
              "WHERE animal_type='Dog' AND outcome_type='Adoption' GROUP BY 1 HAVING count(*) >= 30 ORDER BY 2 DESC",
    "season": "SELECT month(intake_date) AS month, count(*) AS n FROM stays WHERE animal_type='Cat' GROUP BY 1 ORDER BY 2 DESC LIMIT 3",
}


class Scripted(Model):
    def __init__(self, role: str):
        self.role = role

    async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                           tracing, *, previous_response_id=None, conversation_id=None, prompt=None):
        items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)
        out = getattr(self, self.role)(items, [t.name for t in tools])
        return ModelResponse(output=out, usage=Usage(), response_id=None)

    def stream_response(self, *a, **k):
        raise NotImplementedError

    def clarifier(self, items, tools):
        CLARIFIER_INPUTS.append(_messages(items))
        latest = _messages(items)[-1]["content"]
        if any(v in latest.lower() for v in VAGUE) and "ask_user" in tools:
            return [_call("ask_user", {"question": "Do you mean stay length, adoption rates, or intake trends?"})]
        earlier = [m["content"] for m in _messages(items)[:-1] if m["role"] == "user"]
        brief = latest + (f" (following up on: {' / '.join(earlier)})" if earlier else "")
        return [_call("submit_brief", {"brief": brief})]

    def lead(self, items, tools):
        outputs = _outputs(items)
        if not outputs:
            return [_call("dispatch_researchers", {"tasks": ["age: median days to adoption for dogs by age group",
                                                             "intake: median days to adoption for dogs by intake type"]})]
        if len(outputs) == 1:
            return [_call("dispatch_researchers", {"tasks": ["season: which months bring the most cats in"]})]
        return [_text("DONE\n- Age matters most.\n- The data has no temperament records.")]

    def researcher(self, items, tools):
        task = items[0]["content"]
        key = task.removeprefix("Task: ").split(":")[0]
        outputs = _outputs(items)
        if not outputs:
            return [_call("run_sql", {"purpose": f"{key} breakdown", "sql": TASK_SQL[key]})]
        result = json.loads(outputs[0])
        first = result["rows"][0]
        return [_text(f"- {first[0]}: {first[1]} (n={first[-1]}) {result['cite_as']}\n- Limits: one shelter only.")]

    def writer(self, items, tools):
        prompt = items[0]["content"]
        bullets = re.findall(r"^- (?!Limits|FAILED)(.+\[Q\d+\])$", prompt, re.M)
        body = "\n".join(f"{b.split(':')[0]} stood out at {b.split(':', 1)[1].strip()}" for b in bullets)
        if "Fix these problems" in prompt:
            return [_text(f"## Answer\nAge and intake type are associated with stay length.\n\n{body}\n\n"
                          "## What this data can't tell you\nTemperament.")]
        # First draft: one invented number, which the citation check must catch.
        return [_text(f"## Answer\nAge matters.\n\n{body}\nDogs waited 999 days on average [Q1].\n\n"
                      "## What this data can't tell you\nTemperament.")]


# Researchers sleep like a real model call, so we can see that they overlap in time.
class TimedResearcher(Scripted):
    async def get_response(self, system_instructions, input, *a, **k):
        start = time.monotonic()
        await asyncio.sleep(0.3)
        INTERVALS.append((start, time.monotonic()))
        if FAIL_NEXT_RESEARCHER["on"]:
            FAIL_NEXT_RESEARCHER["on"] = False
            raise ConnectionError("model API unreachable")
        return await super().get_response(system_instructions, input, *a, **k)


def install():
    app.clarifier.model = Scripted("clarifier")
    app.clarifier_must_brief.model = app.clarifier.model
    app.lead.model = Scripted("lead")
    app.researcher.model = TimedResearcher("researcher")
    app.writer.model = Scripted("writer")


def _ctx(research, args="{}"):
    from agents.tool_context import ToolContext
    return ToolContext(context=research, tool_name="t", tool_call_id="t", tool_arguments=args)


def _research():
    return app.Research(hooks=app.ResearchHooks(app.AgentContexts(), lambda event: None))


def _invoke(tool, args: dict, research=None):
    research = research or _research()
    return asyncio.run(tool.on_invoke_tool(_ctx(research, json.dumps(args)), json.dumps(args)))


def test_all():
    install()
    client = TestClient(app.app)

    # 1. Vague: the Clarifier asks, no research runs
    r = client.post("/chat", json={"message": "Tell me about the shelter"}).json()
    assert set(r) == {"response", "session_id", "tool_calls"}, r.keys()
    assert "stay length" in r["response"], r["response"]
    assert [(c["agent"], c["name"]) for c in r["tool_calls"]] == [("Clarifier", "ask_user")], r["tool_calls"]
    sid = r["session_id"]

    # 2. The answer resumes the session: the Clarifier sees its own question and can't ask again
    t0 = time.monotonic()
    r = client.post("/chat", json={"message": "doing better on stay length for dogs", "session_id": sid}).json()
    elapsed = time.monotonic() - t0
    seen = CLARIFIER_INPUTS[-1]
    assert seen[0]["content"] == "Tell me about the shelter" and seen[1]["content"].startswith(app.CLARIFYING), seen
    calls = r["tool_calls"]
    assert calls[0]["agent"] == "Clarifier" and calls[0]["name"] == "submit_brief", calls[0]
    assert all("agent" in c for c in calls)
    dispatches = [c for c in calls if c["name"] == "dispatch_researchers"]
    assert len(dispatches) == 2, "first fan-out plus one follow-up"
    assert len(dispatches[0]["args"]["tasks"]) == 2
    agents = {c["agent"] for c in calls if c["name"] == "run_sql"}
    assert agents == {"Researcher 1", "Researcher 2", "Researcher 3"}, agents
    report = r["response"]
    assert "## Sources" in report and "999" not in report.split("## Sources")[0], "invented number not revised away"
    assert "all " in report.split("Citation check:")[1][:10], report.split("Citation check:")[1]
    # Parallel: some two researcher model calls were in flight at the same time
    overlaps = sum(1 for i, a in enumerate(INTERVALS) for b in INTERVALS[i + 1:] if a[0] < b[1] and b[0] < a[1])
    assert overlaps >= 2, f"researchers did not overlap: {INTERVALS}"
    print(f"clarify + research + write took {elapsed:.2f}s, {overlaps} overlapping researcher calls")

    # 3. Memory: a follow-up sees the earlier exchange, from the SDK session
    r = client.post("/chat", json={"message": "what about cats?", "session_id": sid}).json()
    seen = [m["content"] for m in CLARIFIER_INPUTS[-1]]
    assert "doing better on stay length for dogs" in seen and seen[-1] == "what about cats?", seen
    brief = r["tool_calls"][0]["args"]["brief"]
    assert "what about cats?" in brief and "stay length for dogs" in brief, brief
    # ...a different session starts empty
    client.post("/chat", json={"message": "what about cats?"})
    assert [m["content"] for m in CLARIFIER_INPUTS[-1]] == ["what about cats?"], "sessions leaked into each other"
    # ...and /clear forgets
    client.post(f"/clear?session_id={sid}")
    assert asyncio.run(app.session_for(sid).get_items()) == []

    # 4. Caps: a fourth round is refused; a one-task first round is refused
    research = _research()
    research.rounds = app.MAX_ROUNDS
    out = json.loads(_invoke(app.dispatch_researchers, {"tasks": ["x"]}, research))
    assert "Turn cap reached" in out["error"] and "DONE" in out["what_to_do"], out
    out = json.loads(_invoke(app.dispatch_researchers, {"tasks": ["only one"]}))
    assert "needs 2 to 4 tasks" in out["error"], out

    # 5. A failed researcher is reported, not hidden
    FAIL_NEXT_RESEARCHER["on"] = True
    r = client.post("/chat", json={"message": "Which dogs wait longest?"}).json()
    lead_reads = next(c["result"] for c in r["tool_calls"] if c["name"] == "dispatch_researchers")
    assert "FAILED: Researcher 1" in lead_reads and "Re-dispatch" in lead_reads, lead_reads[-300:]

    # 6. Tool errors come back as JSON with a next step
    for tool, args in [
        (app.run_sql, {"purpose": "x", "sql": "SELECT nope FROM stays"}),
        (app.run_sql, {"purpose": "x", "sql": "SELECT * FROM dogs"}),
        (app.run_sql, {"purpose": "x", "sql": "DELETE FROM stays"}),
        (app.run_sql, {"purpose": "x", "sql": "SELEC 1"}),
        (app.column_values, {"table": "stays", "column": "count(*)"}),
    ]:
        out = json.loads(_invoke(tool, args))
        assert out["error"] and out["what_to_do"], out
    # ...and a good query comes back as JSON with an ID to cite
    out = json.loads(_invoke(app.run_sql, {"purpose": "x", "sql": TASK_SQL["age"]}))
    assert out["query_id"] == "Q1" and out["cite_as"] == "[Q1]" and out["columns"][0] == "age_group", out

    # 7. The guardrail: a filter on a value the data never holds is stopped before it runs
    assert app.unknown_enum_values("SELECT 1 FROM stays WHERE outcome_type = 'Adopted'") == [("outcome_type", "Adopted")]
    assert app.unknown_enum_values("WHERE s.age_group IN ('senior (7y+)', 'Senior')") == [("age_group", "Senior")]
    assert app.unknown_enum_values(TASK_SQL["age"] + " AND intake_type <> 'Stray'") == []
    from agents import ToolInputGuardrailData
    bad = {"purpose": "x", "sql": "SELECT count(*) FROM stays WHERE outcome_type = 'Adopted'"}
    research = _research()
    data = ToolInputGuardrailData(context=_ctx(research, json.dumps(bad)), agent=app.researcher)
    verdict = asyncio.run(app.known_values_only.run(data))
    message = json.loads(verdict.behavior["message"])
    assert "'Adopted'" in message["error"] and "'Adoption'" in message["detail"], message
    assert research.hooks.tool_calls[-1]["rejected_by"] == "known_values_only", "rejected call not shown in tool_calls"

    # 8. Causal wording is caught in code and sent back for a rewrite; association wording is not
    assert app.causal_claims("The primary driver of lower adoption is owner reclaims [Q1].")
    assert app.causal_claims("Long stays are due to age.")
    assert app.causal_claims("Senior dogs are associated with longer stays [Q2]. Reclaims go with fewer adoptions.") == []

    # 9. A researcher's query budget is enforced in code: it is told what's left, then told to write up
    research = _research()
    app.current_agent.set("Researcher 1")
    left = [json.loads(asyncio.run(app._logged_query(_ctx(research, "{}"), "n", "SELECT count(*) AS n FROM stays")))
            for _ in range(app.RESEARCHER_MAX_QUERIES + 1)]
    assert [x.get("queries_left") for x in left[:-1]] == list(range(app.RESEARCHER_MAX_QUERIES - 1, -1, -1))
    assert "error" in left[-1] and "Write your findings now" in left[-1]["what_to_do"]
    app.current_agent.set("Lead")
    print(report)
    print("ok")


if __name__ == "__main__":
    test_all()
