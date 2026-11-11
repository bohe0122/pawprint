"""Pawprint: deep research over twelve years of animal shelter records.

The architecture from the Multi-Agent Architectures lecture's Deep Research slide, four agents:

  Clarifier   Reads your message with the conversation so far (an SDK Session). If it's too vague
              to measure, it asks you one question; otherwise it writes a self-contained research
              brief, so a follow-up like "what about cats?" becomes a full question.
  Lead        Splits the brief into 2-4 angles and dispatches that many Researchers in one turn.
              They run in parallel, each in a fresh context. It reads the findings and, when they
              leave a gap, sends a follow-up round, up to MAX_ROUNDS.
  Researcher  A Text2SQL agent over data/shelter.duckdb for one angle. Every query gets an ID.
  Writer      Runs once, after the research ends, and writes a report that cites a query for every
              number. Code checks each cited number against that query's result before it goes out.

The five Deep Research requirements, and where the code enforces each one:
  three agent shapes ........ clarifier, lead, researcher, writer below
  fan out in one turn ....... dispatch_researchers: one call, 2-4 tasks, run with asyncio.gather
  fresh contexts ............ run_researcher: a new Runner.run per task, seeing only the task
  back for more, capped ..... dispatch_researchers counts rounds; MAX_ROUNDS and a time budget stop it
  clarify vague questions ... the Clarifier's ask_user ends the run; it can ask only once in a row
  accurate citations ........ QueryLog numbers queries; shelter.check_citations verifies the report
"""

import asyncio
import contextvars
import json
import os
import re
import time
import uuid
from collections import OrderedDict
from typing import Literal
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
import duckdb
from agents import (
    Agent,
    ModelSettings,
    RunContextWrapper,
    Runner,
    SQLiteSession,
    StopAtTools,
    ToolGuardrailFunctionOutput,
    function_tool,
    set_tracing_disabled,
    tool_input_guardrail,
)
from agents.exceptions import MaxTurnsExceeded
from agents.extensions.models.litellm_model import LitellmModel
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import shelter
from context_panel import AgentContexts, PanelHooks, add_context_panel

load_dotenv(Path(__file__).parent / ".env")

# Traces upload to OpenAI by default, which needs an OpenAI key. We're on Gemini.
set_tracing_disabled(True)

# LiteLLM routes the call to Gemini on Vertex AI. api_key is unused: Vertex uses gcloud credentials
# locally and the service account on Cloud Run. To try it without gcloud, use a free Google AI Studio
# key instead: MODEL=gemini/gemini-3.5-flash and GEMINI_API_KEY=... in .env.
MODEL_NAME = os.environ.get("MODEL", "vertex_ai/gemini-3.5-flash")
VERTEX = MODEL_NAME.startswith("vertex_ai/")
MODEL = LitellmModel(model=MODEL_NAME, api_key=None if not VERTEX else "unused")
# A stalled model call would hang the page: give up after 90 s, and retry twice.
SETTINGS = ModelSettings(extra_args={"timeout": 90, "num_retries": 2, **({"vertex_location": "global"} if VERTEX else {})})

# --- Caps. Prompts ask for restraint; the code enforces it. Graders wait at most 5 minutes. ---
MAX_ROUNDS = 3  # research rounds in all: the first fan-out plus at most 2 follow-ups
FIRST_ROUND = (2, 4)  # the first fan-out sends this many researchers, no fewer, no more
FOLLOW_UP = (1, 3)  # a follow-up round, for a gap, sends this many
MAX_RESEARCHERS = 10  # in all, across rounds
RESEARCH_BUDGET_S = 100  # after this, no new rounds start: the Writer needs time too
RESEARCHER_TIMEOUT_S = 100  # and never more than RESEARCH_GRACE_S past the research budget
RESEARCH_GRACE_S = 30
REVISE_IF_UNDER_S = 200  # the Writer's one revision runs only if the answer is younger than this: graders wait 5 min
LEAD_MAX_TURNS = 8
RESEARCHER_MAX_TURNS = 10
RESEARCHER_MAX_QUERIES = 6  # per researcher; leaves turns to write the finding, so none hit the turn cap

# Conversation history: the SDK's SQLiteSession, as in Project 1's starter. On Cloud Run the file
# lives in the instance's memory, so the service runs one instance (see README).
SESSIONS_DB = os.environ.get("SESSIONS_DB", str(Path(__file__).parent / "conversations.db"))
HISTORY_ITEMS = 8  # the last 4 exchanges: what the Clarifier sees of the conversation
CLARIFYING = "(Clarifying question) "  # marks the Clarifier's own questions in the session


@dataclass
class Finding:
    researcher: str
    round: int
    task: str
    text: str
    failed: bool = False


@dataclass
class Research:
    """One run's state, shared by every agent and tool in it. The model never sees it."""

    hooks: "ResearchHooks"
    log: shelter.QueryLog = field(default_factory=shelter.QueryLog)
    rounds: int = 0
    researchers: int = 0
    findings: list[Finding] = field(default_factory=list)
    clarification: str | None = None  # set by the Clarifier's ask_user
    brief: str | None = None  # set by the Clarifier's submit_brief
    deadline: float = field(default_factory=lambda: time.monotonic() + RESEARCH_BUDGET_S)
    queries_by: dict[str, int] = field(default_factory=dict)  # researcher -> queries tried


# Which agent is running the current tool call. Each researcher sets it in its own asyncio task,
# so parallel researchers each see their own name.
current_agent: contextvars.ContextVar[str] = contextvars.ContextVar("current_agent", default="Lead")


class ResearchHooks(PanelHooks):
    """PanelHooks (the context panel and activity feed) plus a record of every tool call, with the
    agent that made it, for /chat's tool_calls."""

    def __init__(self, contexts: AgentContexts, emit):
        super().__init__(contexts, emit)
        self.tool_calls: list[dict] = []

    async def on_tool_end(self, context, agent, tool, result):
        try:
            args = json.loads(getattr(context, "tool_arguments", "") or "{}")
        except ValueError:
            args = getattr(context, "tool_arguments", "")
        self.tool_calls.append({"agent": agent.name, "name": tool.name, "args": args, "result": str(result)})


# --- Researcher tools: read the data ---


def tool_error(error: str, what_to_do: str, detail: str = "") -> str:
    """Errors go back to the model as JSON: what went wrong, and the next step that fixes it."""
    payload = {"error": error, "what_to_do": what_to_do}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload)


def sql_error(e: Exception) -> str:
    """Turn a failed query into guidance the researcher can act on. A failed query gets no ID,
    so there is nothing to cite from it."""
    detail = str(e).strip()[:500]
    if isinstance(e, ValueError):  # our own read-only checks
        return tool_error("Query not allowed.", f"{e} Rewrite it as a single SELECT and call run_sql again.")
    if isinstance(e, duckdb.BinderException):
        return tool_error(
            "A column name in the query doesn't exist in that table.",
            "Use a column from the schema in your instructions (the error lists close matches), "
            "or call column_values to check a column. Then run the corrected query.",
            detail,
        )
    if isinstance(e, duckdb.CatalogException):
        return tool_error(
            "A table or function in the query doesn't exist.",
            "The only tables are stays, recent_stays, recent_intakes and snapshot. Fix the name and run it again.",
            detail,
        )
    if isinstance(e, duckdb.ParserException):
        return tool_error("The SQL has a syntax error.", "Fix the syntax at the position shown in detail and run it again.", detail)
    if isinstance(e, duckdb.InterruptException):
        return tool_error(
            f"The query ran longer than {shelter.TIMEOUT_S} seconds and was stopped.",
            "Aggregate with GROUP BY instead of returning rows, add a WHERE filter, and avoid self-joins.",
        )
    return tool_error(
        f"The query failed ({type(e).__name__}).",
        "Read detail, fix the query, and run it again. If it fails twice, report this part as a limit.",
        detail,
    )


async def _logged_query(ctx: RunContextWrapper[Research], purpose: str, sql: str) -> str:
    agent = current_agent.get()
    if not sql or not sql.strip():
        return tool_error("The sql argument is empty.", "Pass one complete SELECT statement in sql.")
    # A query budget in code, not the prompt: a researcher that keeps probing runs out of turns and
    # loses everything it found. Tell it how many are left, and stop it before the cap.
    used = ctx.context.queries_by.get(agent, 0)
    if agent.startswith("Researcher") and used >= RESEARCHER_MAX_QUERIES:
        return tool_error(f"You've used all {RESEARCHER_MAX_QUERIES} queries for this task.",
                          "Don't call any more tools. Write your findings now from the results you already have, "
                          "and put what you couldn't check under Limits.")
    ctx.context.queries_by[agent] = used + 1
    left = RESEARCHER_MAX_QUERIES - used - 1
    try:
        # DuckDB blocks: run it in a thread so the other researchers keep going
        result = await asyncio.to_thread(shelter.query, sql)
    except Exception as e:
        # The model cannot see an exception. Return something it can reason about.
        return sql_error(e)
    query = ctx.context.log.add(agent, purpose, sql.strip(), result.text, len(result.rows))
    ctx.context.hooks.activity(agent, f"ran {query.id}: {purpose}")
    # JSON, like the run_sql in the Text2SQL lecture, plus the ID to cite
    payload = {"query_id": query.id, "cite_as": f"[{query.id}]", "columns": result.columns, "rows": result.rows}
    if agent.startswith("Researcher"):
        payload["queries_left"] = left
        if left <= 1:
            payload["note"] = "Last query or none left: prefer writing your findings now."
    if result.truncated:
        payload["truncated"] = f"Only the first {shelter.MAX_ROWS} rows are shown. Aggregate with GROUP BY instead."
    return json.dumps(payload)


# Comparisons against a quoted value: col = 'x', col != 'x', col IN ('x', 'y')
_EQUALS = re.compile(r"\b({cols})\s*(?:=|!=|<>)\s*'([^']*)'".format(cols="|".join(shelter.ENUM_COLUMNS)), re.I)
# The list may hold values with parentheses of their own, like 'senior (7y+)': skip those inside quotes
_IN = re.compile(r"\b({cols})\s+(?:NOT\s+)?IN\s*\(((?:'[^']*'|[^)'])*)\)".format(cols="|".join(shelter.ENUM_COLUMNS)), re.I)


def unknown_enum_values(sql: str) -> list[tuple[str, str]]:
    """(column, value) pairs where the query filters an enum column on a value the data never holds."""
    enums = shelter.enum_values()
    found = [(m.group(1).lower(), m.group(2)) for m in _EQUALS.finditer(sql)]
    for m in _IN.finditer(sql):
        found += [(m.group(1).lower(), v) for v in re.findall(r"'([^']*)'", m.group(2))]
    return [(col, val) for col, val in found if val not in enums.get(col, {val})]


@tool_input_guardrail
def known_values_only(data) -> ToolGuardrailFunctionOutput:
    """Reject a query that filters on a value the column doesn't hold, like outcome_type = 'Adopted'.
    It would run without error and match no rows, and the researcher would report a false zero.
    The researcher reads this message instead of a result, and tries again (Text2SQL lecture)."""
    try:
        args = json.loads(data.context.tool_arguments or "{}")
    except ValueError:
        return ToolGuardrailFunctionOutput.allow()
    bad = unknown_enum_values(args.get("sql", ""))
    if not bad:
        return ToolGuardrailFunctionOutput.allow()
    enums = shelter.enum_values()
    column, value = bad[0]
    valid = sorted(enums[column])
    message = tool_error(
        f"Not run: {column} never equals {value!r}, so this filter would silently match zero rows.",
        f"Use one of the exact values of {column} (spelling and case matter), then run the query again.",
        "Valid values: " + ", ".join(repr(v) for v in valid[:40]),
    )
    # A rejected call never reaches on_tool_end, so record it here: /chat's tool_calls shows it too
    hooks = getattr(data.context.context, "hooks", None)
    if hooks is not None:
        hooks.tool_calls.append({"agent": data.agent.name, "name": "run_sql", "args": args, "result": message,
                                 "rejected_by": "known_values_only"})
        hooks.activity(data.agent.name, f"guardrail stopped a filter on {column} = {value!r}")
    return ToolGuardrailFunctionOutput.reject_content(message)


@function_tool(tool_input_guardrails=[known_values_only])
async def run_sql(ctx: RunContextWrapper[Research], purpose: str, sql: str) -> str:
    """Run one read-only DuckDB query against the shelter tables. Returns JSON with a query_id
    (like "Q4"), the columns, and the rows. Cite it as [Q4] for every number you take from it. On
    failure it returns JSON with "error" and "what_to_do" and no query_id: nothing to cite.

    Args:
        purpose: A short plain-English label for what the query measures, shown to the reader next
            to its citation. Name the metric and the animals, e.g. 'median days to adoption by age
            group, dogs, 2013-2025' or 'monthly cat intakes, new system'. 4 to 12 words.
        sql: Exactly one SELECT or WITH ... SELECT statement in DuckDB SQL, no trailing semicolon
            needed. Tables: stays, recent_stays, recent_intakes, snapshot. Compute every number you
            will report in the query itself: counts with count(*), medians with
            median(days_in_shelter), shares with round(100.0 * count(*) FILTER (WHERE ...) / count(*), 1),
            years with year(outcome_date), months with month(intake_date). Always GROUP BY and add
            count(*) AS n so group sizes come back. At most 40 rows are returned, so aggregate
            instead of listing animals. Example:
            SELECT age_group, median(days_in_shelter) AS median_days, count(*) AS n FROM stays
            WHERE animal_type = 'Dog' AND outcome_type = 'Adoption' GROUP BY 1 ORDER BY 2
    """
    return await _logged_query(ctx, purpose, sql)


@function_tool
async def column_values(
    ctx: RunContextWrapper[Research], table: Literal["stays", "recent_stays", "recent_intakes"], column: str
) -> str:
    """List the 30 most common values of one column, with how many rows have each, so you can
    filter on the exact spelling the data uses. Use it before a WHERE on a text column you're
    unsure of: breed ('Pit Bull Mix' vs 'American Pit Bull Terrier'), primary_color, intake_type,
    outcome_type or outcome_status. Returns the same JSON as run_sql, with a query_id you can cite.

    Args:
        table: Which table to look in. stays is the old system (Oct 2013 to May 2025);
            recent_stays and recent_intakes are the new system (May 2025 on). Their columns differ.
        column: One column name exactly as written in the schema, e.g. 'breed', 'intake_condition',
            'outcome_subtype'. A bare name only: no table prefix, no expressions or functions.
    """
    if table not in shelter.DATASETS:
        return tool_error(
            f"There is no table named {table!r}.",
            "Use table='stays' for Oct 2013 to May 2025, or 'recent_stays' / 'recent_intakes' for May 2025 on.",
        )
    if not column.isidentifier():
        return tool_error(
            f"{column!r} is not a plain column name.",
            "Pass one column name from the schema, e.g. column='breed'. To compute something, use run_sql.",
        )
    sql = f"SELECT {column}, count(*) AS n FROM {table} GROUP BY 1 ORDER BY n DESC LIMIT 30"
    return await _logged_query(ctx, f"most common values of {table}.{column}", sql)


# --- The researcher: one angle, by querying the data ---

researcher = Agent(
    name="Researcher",
    instructions=f"""\
You are a data researcher on an animal shelter study. You get ONE task. Answer it by querying the
data with run_sql, then report what you found. You have no other knowledge source: if the data
can't answer part of the task, say so.

{shelter.SCHEMA}
How to work:
1. Run 2 to 6 queries. Use column_values first if you need exact spellings.
2. Let SQL do the math. Every rate, share, median, difference or ratio you will report must appear
   as a column in a query result: compute percentages as round(100.0 * x / n, 1), ratios with round(a / b, 2).
3. Report the group size n next to every rate or median, and skip groups with fewer than 30 animals.
4. Time in the shelter is skewed by a few very long stays: prefer median(days_in_shelter), and report
   averages only next to medians.
5. When you compare stay lengths, compare like with like: e.g. only outcome_type = 'Adoption' for "time
   to adoption", and the same species. If a difference could come from another factor (age, species,
   intake type, era), check it with one more query.
6. Keep the eras apart: stays (2013-10 to 2025-05) and recent_stays (2025-05 on) are different systems.

DuckDB patterns that work on this data (adapt them; don't copy blindly):

-- Median stay and group size, by group
SELECT age_group, median(days_in_shelter) AS median_days, count(*) AS n
FROM stays WHERE animal_type = 'Dog' AND outcome_type = 'Adoption' AND age_group IS NOT NULL
GROUP BY 1 ORDER BY median_days DESC;

-- Share of outcomes, as percentages, within each group
SELECT age_group, count(*) AS n,
       round(100.0 * count(*) FILTER (WHERE outcome_type = 'Adoption') / count(*), 1) AS adoption_pct,
       round(100.0 * count(*) FILTER (WHERE outcome_type = 'Euthanasia') / count(*), 1) AS euthanasia_pct
FROM stays WHERE animal_type = 'Cat' AND age_group IS NOT NULL GROUP BY 1;

-- A trend over years, one era at a time
SELECT year(outcome_date) AS year, count(*) AS n,
       round(100.0 * count(*) FILTER (WHERE outcome_type = 'Adoption') / count(*), 1) AS adoption_pct
FROM stays WHERE animal_type IN ('Dog', 'Cat') GROUP BY 1 ORDER BY 1;

-- A ratio between two groups, computed in SQL so it can be cited
SELECT round(median(days_in_shelter) FILTER (WHERE is_pit_bull_type)
           / median(days_in_shelter) FILTER (WHERE NOT is_pit_bull_type), 2) AS ratio
FROM stays WHERE animal_type = 'Dog' AND outcome_type = 'Adoption' AND age_group = 'adult (1-6y)';

You have 6 queries for the task; each result says how many are left (queries_left). Spend them on
the task, not on testing SQL behavior: for whole years of age use floor(age_years), and if a query
errors, fix it from the error's detail. Answer the task with 2-4 queries when you can.

Reply with at most 8 bullets of findings and nothing else. Every bullet that states a number ends with
the cite_as of the query whose result shows that number, e.g. "Seniors waited a median 31 days (n=2,210) [Q3]".
Copy numbers as the result shows them (rounding is fine). Never cite an ID you didn't get back.
End with a bullet starting "Limits:" saying what this data could not show for your task.
If a tool returns JSON with an "error", do what its "what_to_do" says, then carry on.""",
    model=MODEL,
    model_settings=SETTINGS,
    tools=[run_sql, column_values],
)


async def run_researcher(research: Research, number: int, round_: int, task: str) -> Finding:
    """One researcher, one task, a fresh context: it sees its instructions and the task, nothing else."""
    name = f"Researcher {number}"
    current_agent.set(name)  # this task's own copy of the context var
    agent = researcher.clone(name=name)
    research.hooks.activity(name, f"task (round {round_}): {task}")
    try:
        result = await asyncio.wait_for(
            Runner.run(agent, f"Task: {task}", context=research, hooks=research.hooks, max_turns=RESEARCHER_MAX_TURNS),
            timeout=min(RESEARCHER_TIMEOUT_S, max(30, research.deadline + RESEARCH_GRACE_S - time.monotonic())),
        )
        text, failed = str(result.final_output), False
    except asyncio.TimeoutError:
        text, failed = "ran out of time before finishing", True
    except MaxTurnsExceeded:
        text, failed = f"hit its cap of {RESEARCHER_MAX_TURNS} turns before finishing", True
    except Exception as e:  # a model or network error: one researcher failing mustn't sink the rest
        text, failed = f"failed with {type(e).__name__}: {str(e)[:200]}", True
    if failed:
        # Said plainly, so the Lead re-dispatches or reports the gap instead of quietly filling it
        # ("Hidden sub-agent errors", Multi-Agent Architectures lecture).
        text = f"- FAILED: {name} {text}. Nothing from this task can be cited."
        research.hooks.activity(name, "failed")
    finding = Finding(name, round_, task, text, failed)
    research.findings.append(finding)
    return finding


# --- Lead tools: dispatch, or ask ---


@function_tool
async def dispatch_researchers(ctx: RunContextWrapper[Research], tasks: list[str]) -> str:
    """Send one researcher per task. They all run at once, in parallel, each starting fresh with
    only its task, and this returns when every one has reported back.

    Args:
        tasks: A list of research tasks, one string per researcher. The first call needs 2 to 4
            tasks; a later call that fills a gap needs 1 to 3. A researcher sees ONLY its own task
            string: not the user's question, not the conversation, not the other tasks. So each
            task must name the metric, the animals, the table or era, and the comparison, and spell
            out anything the user said earlier. Good: 'In stays, compare median days to adoption
            for adult dogs (1-6y) that are pit bull type vs not, and check whether the gap holds
            for both Stray and Owner Surrender intakes.' Too vague: 'look at breeds'.
    """
    research = ctx.context
    tasks = [t.strip() for t in tasks if t and t.strip()]
    low, high = FIRST_ROUND if research.rounds == 0 else FOLLOW_UP
    finish = "Stop dispatching. Reply DONE followed by your notes for the writer, including what is still missing."
    if research.rounds >= MAX_ROUNDS:
        return tool_error(f"Turn cap reached: {MAX_ROUNDS} research rounds is the limit.", finish)
    if research.rounds > 0 and time.monotonic() > research.deadline:
        return tool_error("Out of time for more research rounds.", finish)
    if not low <= len(tasks) <= high:
        kind = "The first round" if research.rounds == 0 else "A follow-up round"
        return tool_error(
            f"Not dispatched: {kind} needs {low} to {high} tasks, and you sent {len(tasks)}.",
            f"Call dispatch_researchers again with {low} to {high} self-contained tasks in one list.",
        )
    room = MAX_RESEARCHERS - research.researchers
    if room <= 0:
        return tool_error(f"No researchers left: all {MAX_RESEARCHERS} have been used.", finish)
    tasks = tasks[:room]

    research.rounds += 1
    round_ = research.rounds
    numbers = range(research.researchers + 1, research.researchers + len(tasks) + 1)
    research.researchers += len(tasks)
    research.hooks.activity(
        "Lead", f"round {round_}: " + ("dispatching 1 researcher" if len(tasks) == 1 else f"dispatching {len(tasks)} researchers in parallel")
    )
    # The fan-out: every researcher starts now, and we wait for all of them.
    findings = await asyncio.gather(*(run_researcher(research, n, round_, t) for n, t in zip(numbers, tasks)))

    left = MAX_ROUNDS - research.rounds
    report = "\n\n".join(f"## {f.researcher}\nTask: {f.task}\n\n{f.text}" for f in findings)
    failed = [f.researcher for f in findings if f.failed]
    warning = f" FAILED: {', '.join(failed)}. Re-dispatch those tasks or tell the writer they're missing." if failed else ""
    return f"{report}\n\n(Round {round_} of at most {MAX_ROUNDS}. {left} follow-up round(s) left.{warning})"


@function_tool
def ask_user(ctx: RunContextWrapper[Research], question: str) -> str:
    """Ask the user ONE short clarifying question instead of starting research. This ends your turn;
    their answer comes back as the next message. Use it only when, even with the conversation so
    far, you can't tell what to measure.

    Args:
        question: One sentence to the user that offers 2 or 3 concrete readings they can pick from,
            in plain words, e.g. 'By "doing better", do you mean more adoptions, shorter stays, or
            fewer euthanasias?' No preamble, no more than one question.
    """
    ctx.context.clarification = question.strip()
    return question.strip()


@function_tool
def submit_brief(ctx: RunContextWrapper[Research], brief: str) -> str:
    """Hand the research team a brief, which starts the research. This ends your turn.

    Args:
        brief: 2 to 5 plain sentences that stand on their own: the team never sees the user or the
            conversation. Say what to find out, for which animals, over which years or era, and what
            to compare, and carry over anything from earlier turns that the new message relies on.
            End with the parts the records can't answer, if any. Example: 'How long do cats wait to
            be adopted, by age group (kittens, adults, seniors), across the old system (2013-2025)?
            The user asked the same about dogs just before, so compare cats with that. Personality
            is not in the records.'
    """
    ctx.context.brief = brief.strip()
    return brief.strip()


CLARIFIER_INSTRUCTIONS = f"""\
You are the front desk of a research team that answers questions about animal shelter stays and
outcomes with data. You don't research. You read the user's latest message, in the light of the
conversation so far, and do exactly one of two things:

- submit_brief: the message can be researched. Write the brief.
- ask_user: you can't tell what to measure, even with the conversation. Ask one question.

{shelter.SCHEMA}
When to ask: no outcome, animal group or comparison can be pinned down ("which pet should I get?",
"tell me about the shelter", "is it getting better?", "what about cats?" with nothing before it), or
the message isn't about shelter animals at all (then say in one line what you can research, and ask
what they'd like to know). Don't ask when a reasonable researcher could start, even if the question is
broad: "what makes stays longer?" is fine. If only a side part needs something the records can't
hold, brief what they can answer and name the rest as out of scope. If the MAIN point needs data the
records don't hold (another city or shelter, life after adoption, temperament), don't brief a
different question in its place: ask_user, saying in one sentence that the records can't show that,
and offer the closest question they can answer (for "do adopted dogs stay with their families?":
"I can measure how many come back to this shelter after adoption. Want that?").

Follow-ups: after a question about dogs' stay lengths, "what about cats?" means the same analysis for
cats, and "why?" or "since 2020?" refers to the last answer. Write that full meaning into the brief.
If you asked a question last turn, the user's message is their answer: combine it with the question
it answers."""

clarifier = Agent(
    name="Clarifier",
    instructions=CLARIFIER_INSTRUCTIONS,
    model=MODEL,
    # Every turn must call one of its two tools (tool_choice="required"), and either one ends the run,
    # like the Critic's approve tool in the lecture's handoff variation
    model_settings=SETTINGS.resolve(ModelSettings(tool_choice="required")),
    tools=[submit_brief, ask_user],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_brief", "ask_user"]),
)
# Right after the Clarifier asked: it may not ask again, so the user never gets two questions in a row.
clarifier_must_brief = clarifier.clone(
    instructions=CLARIFIER_INSTRUCTIONS + "\n\nYou asked a clarifying question last turn and the user has "
    "answered it. Don't ask another: take the most reasonable reading and submit_brief, stating that reading.",
    tools=[submit_brief],
    tool_use_behavior=StopAtTools(stop_at_tool_names=["submit_brief"]),
)


# --- The lead: plans, fans out, checks for gaps ---

LEAD_INSTRUCTIONS = f"""\
You lead a research team that answers questions about animal shelter outcomes with data. You get a
research brief. You never query the data yourself: researchers do. You decide what they look at,
and when there's enough.

{shelter.SCHEMA}
1. FAN OUT. Split the brief into 2-4 independent angles and call dispatch_researchers ONCE with all
   of them, so they run in parallel. Each task stands alone: the researcher sees nothing but that
   task. Name the metric (median days in shelter, adoption rate, euthanasia rate, intake counts),
   the animals (species, outcome), the table/era, and the comparison.
2. CHECK FOR GAPS. Read every finding. A gap is: part of the brief nobody answered; a difference that
   could come from another factor nobody checked (e.g. a breed gap that might be an age or
   intake-type gap); two findings that disagree; a "Limits" a further query could fix; a researcher
   marked FAILED. If there's a gap, call dispatch_researchers again with 1-3 tasks for just that
   gap. At most 2 follow-up rounds. Don't repeat work that's done, and never fill a gap yourself.
   NOT a gap: more detail on something already answered (finer age bands, a year-by-year or
   age-by-age list), or a side topic the brief didn't ask about. Each round costs the user about a
   minute, so when every part of the brief has an answer, finish.
3. FINISH. When the brief is covered, or you're out of rounds, reply "DONE" followed by up to 4
   short notes for the writer: which findings matter most, how they connect, and which parts of the
   brief the data could not answer, including any task that FAILED. A writer, who sees only the
   brief, findings and your notes, writes the report.
If a tool returns JSON with an "error", follow its "what_to_do"."""

lead = Agent(
    name="Lead",
    instructions=LEAD_INSTRUCTIONS,
    model=MODEL,
    model_settings=SETTINGS,
    tools=[dispatch_researchers],
)

# --- The writer: one call, after the research ends ---

writer = Agent(
    name="Writer",
    instructions="""\
You write the final report for someone who works with or adopts from animal shelters (a shelter
volunteer, staffer, or adopter). Use ONLY the findings you're given: you have no other knowledge,
and you never compute a new number. The numbers come from one large US city shelter's records.

You get the user's message and the research brief written from it. Answer the brief: it spells out
what a short follow-up like "what about cats?" means.

Format, in markdown:
Keep it short: about 350 words before the limits section, at most 3 sections between "## Answer" and
"## What this data can't tell you", and never a list of more than 5 rows. Summarize a breakdown by
its pattern and its extremes ("medians rise from 7 days at age 0 to 22 at age 8") instead of listing
every row. For a follow-up, compare with the earlier answer in one sentence; don't repeat it.
Describe a pattern only as the numbers show it: check the order of the values before calling it
rising, falling or U-shaped.

- "## Answer": one short paragraph that directly answers the question.
- One "## " section per part of the question, in plain, specific sentences.
- "## What this data can't tell you": the parts the findings couldn't answer, the researchers'
  "Limits", any research task marked FAILED, and that these are one shelter's records, so other
  shelters may differ.

Numbers: "(n=...)" is always the size of the group a rate or median is taken over, never the count
of one outcome inside it: "52.1% were adopted (n=83,185)", or "43,350 of 83,185 were adopted".
Only when the question asks about something the records can't measure directly (life after adoption,
temperament) and the findings use a stand-in for it, say so in the Answer's first sentence and never
state the unmeasured thing as fact. Otherwise don't add caveats to the first sentence.
When a check rules out one explanation, name the check and what it changed, not that a pattern is
"real" or "not an artifact".
Compare like with like: the same outcome filter (adopted only, or all outcomes), the same definitions
and the same kind of number across groups and eras. If two findings differ in any of these, don't put
them side by side as a comparison; if you must mention both, say how they differ.

Citations: every sentence that states a number ends with the query ID(s) from the finding it came
from, e.g. "Senior dogs waited a median 31 days (n=2,210) [Q3]." Use only IDs that appear in the
findings, and only for numbers that appear in that query's finding. A sentence with no number needs
no ID. Do not add a sources list: one is added for you.

Say "associated with", not "causes": this is observational data. Never write "driver", "drives",
"because of", "due to", "leads to", "results in" or "explains" about a pattern in the data. Don't
overstate: no "significantly" (nothing here is a significance test), "substantially", "massive",
"dramatic", "remarkably", "profoundly", "pivotal", "it is worth noting".""",
    model=MODEL,
    model_settings=SETTINGS,
)


def _writer_input(message: str, research: Research, lead_notes: str) -> str:
    findings = "\n\n---\n\n".join(f"{f.researcher} (round {f.round})\nTask: {f.task}\n\n{f.text}" for f in research.findings)
    catalog = "\n".join(f"[{q.id}] {q.purpose}" for q in research.log.queries.values())
    return (
        f"User's message: {message}\n\nResearch brief: {research.brief}\n\nLead's notes:\n{lead_notes or '(none)'}\n\n"
        f"Findings:\n\n{findings or '(No research was done: the question was outside what the data holds.)'}\n\n"
        f"Queries the researchers ran (cite only these IDs):\n{catalog or '(none)'}"
    )


# Causal wording the data can't support. Checked in code, because a prompt rule alone gets ignored.
CAUSAL = re.compile(r"[^.\n]*\b(?:primary |main |key )?(?:drivers?|drives|driven by|because of|due to|leads? to|"
                    r"results? in|caused? by|causes)\b[^.\n]*\.?", re.IGNORECASE)


def causal_claims(report: str) -> list[str]:
    """Sentences that state a cause, which observational shelter records can't show.
    The limits section may say what the data can't show is "due to" something, so it isn't checked."""
    claims = report.split("## What this data can't tell you")[0]
    return [m.group(0).strip() for m in CAUSAL.finditer(claims)]


def _citation_problems(check: dict, causal: list[str] = ()) -> str:
    lines = [f"- [{q}] is not a query that was run. Remove it or replace it with the right ID." for q in check["unknown"]]
    lines += [f"- \"{c}\" states a cause. This data only shows association: reword it with "
              "\"associated with\" or \"goes with\"." for c in causal]
    lines += [
        f"- The number {u['number']:g} in \"{u['sentence']}\" is not in the result of {', '.join(u['cites'])}. "
        "Cite the query whose finding states that number, or drop the number."
        for u in check["unverified"]
    ]
    return "\n".join(lines)


def _sources(report: str, research: Research, check: dict) -> tuple[str, list[dict]]:
    """The sources section appended to the report, and the same as data for the page."""
    snapshot = shelter.run_query("SELECT strftime(data_through, '%Y-%m-%d') FROM snapshot")[0].splitlines()[1]
    cited = [research.log.queries[q] for q in check["cited"] if q in research.log.queries]
    sources = []
    lines = [
        "## Sources",
        f"Austin Animal Center intake and outcome records, data.austintexas.gov, through {snapshot}. "
        "Each [Q#] is a query a researcher ran:",
        "",
    ]
    for q in cited:
        tables = [t for t in shelter.DATASETS if re.search(rf"\b{t}\b", q.sql)]
        links = "; ".join(shelter.DATASETS[t] for t in tables)
        lines += [f"**[{q.id}]** {q.purpose}, by {q.agent}. Data: {links}", "```sql", q.sql, "```", ""]
        sources.append({"id": q.id, "agent": q.agent, "purpose": q.purpose, "sql": q.sql, "result": q.result,
                        "rows": q.rows, "datasets": [shelter.DATASETS[t] for t in tables]})
    bad = len(check["unverified"]) + len(check["unknown"])
    if bad:
        lines.append(f"Citation check: {check['numbers_checked']} cited numbers checked against their query results; "
                     f"{bad} could not be matched and are flagged here: "
                     + "; ".join(f"{u['number']:g} ({', '.join(u['cites'])})" for u in check["unverified"])
                     + "".join(f"; [{q}] does not exist" for q in check["unknown"]))
    else:
        lines.append(f"Citation check: all {check['numbers_checked']} cited numbers match the results of the queries they cite.")
    return "\n".join(lines), sources


# --- One message in, a clarifying question or a cited report out ---

def _answer_summary(report: str, limit: int = 900) -> str:
    """The report's opening answer, without citations: what the session remembers of it."""
    body = report.split("## Sources")[0]
    match = re.search(r"##\s*Answer\s*\n+(.*?)(?=\n##\s|\Z)", body, re.S)
    text = (match.group(1) if match else body).strip()
    text = re.sub(r"\s*\[Q\d+\]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "..."


def session_for(session_id: str) -> SQLiteSession:
    return SQLiteSession(session_id, SESSIONS_DB)


@dataclass
class Answer:
    kind: str  # "clarify" or "report"
    markdown: str
    sources: list = field(default_factory=list)
    check: dict = field(default_factory=dict)


async def answer(session_id: str, message: str, hooks: ResearchHooks) -> Answer:
    research = Research(hooks=hooks)
    session = session_for(session_id)
    history = await session.get_items(limit=HISTORY_ITEMS)

    # 1. Clarify: a question for the user, or a self-contained brief for the team. The Clarifier sees
    # the conversation; nothing after it does. If it asked last turn, it can't ask again.
    asked_last = bool(history) and str(history[-1].get("content", "")).startswith(CLARIFYING)
    clarify_agent = clarifier_must_brief if asked_last else clarifier
    user_item = {"role": "user", "content": message}
    result = await Runner.run(clarify_agent, [*history, user_item], context=research, hooks=hooks, max_turns=3)
    if research.clarification and not research.brief:
        await session.add_items([user_item, {"role": "assistant", "content": CLARIFYING + research.clarification}])
        return Answer("clarify", research.clarification)
    if not research.brief:  # it answered in text instead of calling a tool: use that as the brief
        research.brief = str(result.final_output).strip() or message

    # 2. Research: plan, fan out, check for gaps. The Lead sees only the brief.
    lead_notes = ""
    try:
        result = await Runner.run(lead, f"Research brief: {research.brief}", context=research, hooks=hooks,
                                  max_turns=LEAD_MAX_TURNS)
        lead_notes = str(result.final_output).removeprefix("DONE").strip()
    except MaxTurnsExceeded:
        lead_notes = "The Lead hit its turn cap; write from the findings so far."

    # 3. Write: outside the loop. The Writer sees the message, the brief, the findings and the Lead's notes.
    prompt = _writer_input(message, research, lead_notes)
    report = str((await Runner.run(writer, prompt, hooks=hooks, max_turns=2)).final_output)

    # 4. Check every cited number against its query's result. One revision if anything doesn't match.
    check = shelter.check_citations(report, research.log)
    causal = causal_claims(report)
    started = research.deadline - RESEARCH_BUDGET_S
    if (check["unknown"] or check["unverified"] or causal) and time.monotonic() - started < REVISE_IF_UNDER_S:
        hooks.activity("Writer", "revising: " + ("some citations didn't match their query results"
                                                   if check["unknown"] or check["unverified"] else "rewording a causal claim"))
        fix = f"{prompt}\n\nYour draft:\n\n{report}\n\nFix these problems and return the whole report:\n{_citation_problems(check, causal)}"
        report = str((await Runner.run(writer, fix, hooks=hooks, max_turns=2)).final_output)
        check = shelter.check_citations(report, research.log)

    sources_md, sources = _sources(report, research, check)
    # The session remembers the exchange, so the next message can build on this answer
    await session.add_items([user_item, {"role": "assistant", "content": _answer_summary(report)}])
    return Answer("report", f"{report.strip()}\n\n{sources_md}", sources, check)


# --- FastAPI App ---

HERE = Path(__file__).parent

# run_id -> every agent's context, in order. In memory, single process. The context panel reads it.
sessions: OrderedDict[str, AgentContexts] = OrderedDict()

app = FastAPI()
add_context_panel(app, sessions)


def new_run(emit) -> tuple[str, ResearchHooks]:
    run_id = str(uuid.uuid4())
    sessions[run_id] = AgentContexts()
    while len(sessions) > 100:
        sessions.popitem(last=False)
    return run_id, ResearchHooks(sessions[run_id], emit)


@app.get("/")
def index():
    # no-cache: browsers otherwise keep serving an old page after a redeploy
    return FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    session_id: str
    tool_calls: list[dict]


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Project 1's shape. A vague question gets a clarifying question back: reply with the same
    session_id to continue, and follow-ups in the same session build on earlier answers. tool_calls
    lists every agent's calls, each with an agent field."""
    session_id = request.session_id or str(uuid.uuid4())
    _, hooks = new_run(lambda event: None)
    try:
        result = await answer(session_id, request.message, hooks)
        response = result.markdown
    except Exception as e:
        # Auth, quota, a model that won't stop: show it in the chat, not as a 500.
        response = f"Research failed: {type(e).__name__}: {str(e)[:300]}"
    return ChatResponse(response=response, session_id=session_id, tool_calls=hooks.tool_calls)


@app.post("/clear")
async def clear(session_id: str | None = None):
    if session_id:
        await session_for(session_id).clear_session()
    return {"status": "ok"}


@app.get("/run")
async def run(question: str, session_id: str):
    """The page's endpoint: the same research as /chat, streamed as Server-Sent Events so the page
    can show each agent's steps as they happen."""
    queue: asyncio.Queue = asyncio.Queue()
    run_id, hooks = new_run(queue.put_nowait)

    async def work():
        try:
            result = await answer(session_id, question, hooks)
            queue.put_nowait({"type": result.kind, "markdown": result.markdown, "sources": result.sources,
                              "check": result.check, "tool_calls": len(hooks.tool_calls)})
        except Exception as e:
            queue.put_nowait({"type": "error", "message": f"{type(e).__name__}: {str(e)[:500]}"})

    task = asyncio.create_task(work())

    async def events():
        try:
            yield f"data: {json.dumps({'type': 'start', 'run_id': run_id})}\n\n"
            while True:
                event = await queue.get()
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event["type"] in ("clarify", "report", "error"):
                    break
        finally:
            task.cancel()  # the page closed the stream: stop spending tokens

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/stats")
def stats():
    """Headline numbers for the page's header, straight from the data."""
    con = shelter._connection()
    old = con.execute("SELECT count(*), min(intake_date)::DATE FROM stays").fetchone()
    new = con.execute("SELECT count(*) FROM recent_stays").fetchone()[0]
    through = con.execute("SELECT data_through::DATE FROM snapshot").fetchone()[0]
    return {"stays": old[0] + new, "since": str(old[1]), "through": str(through)}


if __name__ == "__main__":
    # Cloud Run sets PORT and needs 0.0.0.0. Locally: http://localhost:8000
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0" if "PORT" in os.environ else "127.0.0.1", port=port)
