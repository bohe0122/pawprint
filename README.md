# Pawprint: why do some shelter animals wait so long for a home?

Pawprint is a deep research agent over twelve years of animal shelter records. You ask a question about
shelter stays and outcomes; a Lead agent plans the research, Researcher agents query the data in parallel,
and a Writer agent produces a report in which every number links to the SQL query that produced it.

**Who it's for.** Shelter volunteers, staff and rescue coordinators deciding which animals need extra help
(a foster push, a featured listing, a fee waiver), and adopters curious about who gets overlooked. They have
questions like "which dogs wait longest, and why?" that the shelter's records can answer, but not without
writing SQL against 190,000 rows.

**The question it explores.** What factors are associated with longer shelter stays and different adoption
outcomes? How do age, animal type, breed, intake condition and seasonality relate to adoption rates and time
spent in shelters? How have these patterns changed over time?

## How it works

Four agents, following the Deep Research architecture from the Multi-Agent Architectures lecture
(Clarifier → Lead → Researchers → Writer):

```
 you ──► Clarifier ──(vague?)──► one clarifying question ──► you answer, same session
         (sees the conversation: SDK SQLiteSession)
           │ research brief: self-contained, follow-ups spelled out
           ▼
          Lead ─ round 1: dispatch_researchers([2-4 tasks]) ─┬─► Researcher 1 ─┐  each in a fresh context,
           │                                                 ├─► Researcher 2 ─┤  a Text2SQL agent over
           │                                                 └─► Researcher 3 ─┘  DuckDB; every query gets an ID
           │◄──────────────────── findings, each number tagged [Q#] ◄──────────┘
           ├─ gap? round 2..3: dispatch_researchers([1-3 tasks]) ─► ...
           ▼
         Writer ──► report ──► citation check ──(mismatch?)──► one rewrite ──► report + sources
```

| Agent | Job | Pattern (lecture) | Tools |
|---|---|---|---|
| **Clarifier** | Reads your message with the conversation so far; asks one question if it's too vague, otherwise writes a self-contained brief | Gatekeeper / disambiguation | `ask_user`, `submit_brief` (one is required each turn, and either ends the run) |
| **Lead** | Splits the brief into angles, fans out, reads findings, sends more for gaps | Critic, with a fan-out | `dispatch_researchers` |
| **Researcher** | Answers one angle with SQL, in a fresh context | Generator; Text2SQL agent | `run_sql`, `column_values` |
| **Writer** | Runs once after the research and writes the cited report | Agents in sequence | none |

### The Deep Research requirements, and where the code enforces each

| Requirement | How Pawprint meets it | Where |
|---|---|---|
| At least three agent shapes with different jobs | Four: **Clarifier** (sees the user and the conversation, nothing else), **Lead** (plans and judges, no data access), **Researcher** (SQL for one angle), **Writer** (report from findings only, no tools) | `app.py`: `clarifier`, `lead`, `researcher`, `writer` |
| The Lead fans out to several Researchers in one turn, in parallel, in fresh contexts | The Lead's only tool, `dispatch_researchers(tasks)`, takes a list of tasks in **one** call and starts every researcher at once with `asyncio.gather`, the lecture's fan-out. The first round must have 2 to 4 tasks, or the call is refused. Each researcher is a new `Runner.run` that sees only its instructions and its task. DuckDB runs in a thread with a cursor per call, so researchers don't block each other. | `dispatch_researchers`, `run_researcher` |
| The Lead goes back for more when findings leave a gap, up to a turn cap | The Lead reads the findings against an explicit gap list (an unanswered part, an unchecked confounder, findings that disagree, a failed researcher) and can dispatch 1-3 follow-up tasks. The cap is in code, not the prompt (the lecture's "prompt-only limits" pitfall): **3 rounds**, **10 researchers**, a **100 s research budget** (no new round after it; a running researcher gets at most 30 s more), `max_turns=8` for the Lead, **6 queries per researcher** (each result says how many are left, and a 7th is refused with "write your findings now", so researchers finish instead of hitting their turn cap), and the Writer's revision is skipped once an answer is 200 s old, so answers stay well inside the graders' 5 minutes. More detail on something already answered is not a gap. | `MAX_ROUNDS`, `MAX_RESEARCHERS`, `RESEARCH_BUDGET_S` |
| Vague input questions require clarification | The Clarifier must call `ask_user` or `submit_brief` (`tool_choice="required"`), and `StopAtTools` ends its run at either. A question goes back to the user and is saved in the session. On the next message the Clarifier sees its own question and the answer, and runs without `ask_user`, so it never asks twice in a row. | `clarifier`, `clarifier_must_brief` |
| The output is accurately cited | Every query a researcher runs is logged with an ID (`Q1`, `Q2`, ...) and returned with it. Researchers compute every rate and ratio in SQL so each reported number appears in a result. After the Writer drafts, **code checks every number in every citing sentence against the cited query's actual result**; mismatches and nonexistent IDs go back to the Writer for one rewrite, and anything still unmatched is flagged in the report. The same rewrite also rewords causal claims ("driver", "due to", "leads to"), which code finds too, since a prompt rule alone was ignored in testing. The report ends with a Sources section: each cited query's SQL, who ran it, and the dataset it reads. | `shelter.QueryLog`, `shelter.check_citations`, `causal_claims`, `_sources` |

### Showing its work

`POST /chat` keeps Project 1's shape exactly: `{"response", "session_id", "tool_calls"}`. Every tool call
from every agent is in `tool_calls`, each with an `agent` field:

```json
{"agent": "Researcher 2", "name": "run_sql",
 "args": {"purpose": "median days to adoption by age group, dogs", "sql": "SELECT ..."},
 "result": "{\"query_id\": \"Q3\", \"cite_as\": \"[Q3]\", \"columns\": [\"age_group\", \"median_days\", \"n\"], \"rows\": [[\"senior (7y+)\", 20.86, 2895], ...]}"}
```

The first call of every answer is the Clarifier's, so the brief it wrote is in `tool_calls` too.

A vague question returns the clarifying question as `response`; send the answer with the same `session_id`.

### It remembers the conversation

Conversation history uses the Agents SDK's own `SQLiteSession`, as Project 1's starter did. Each
`session_id` keeps its exchanges: your messages, the Clarifier's questions, and a short summary of each
report. The Clarifier reads the last four exchanges, so a follow-up such as "what about cats?" or "has that
changed since 2020?" becomes a full brief ("How long do cats wait to be adopted, by age group... The user
asked the same about dogs just before"). Nothing after the Clarifier sees the conversation: the Lead gets
the brief, and each researcher gets one task. Sessions are separate, `POST /clear` forgets one, and the
page's "start a new conversation" button calls it.

### Tools follow the tool-calling lecture's practices

- **Results are JSON.** `run_sql` returns `query_id`, `cite_as`, `columns` and `rows` (at most 40, with a
  note when more were cut), like the `run_sql` in the Text2SQL lecture.
- **Errors are JSON with a next step**, never a bare exception or stack trace:

  ```json
  {"error": "A column name in the query doesn't exist in that table.",
   "what_to_do": "Use a column from the schema in your instructions (the error lists close matches), or call column_values to check a column. Then run the corrected query.",
   "detail": "Binder Error: Referenced column \"nope\" not found ... Candidate bindings: \"intake_date\", ..."}
  ```

  Unknown columns, unknown tables, syntax errors, write attempts, slow queries, a round over the cap and
  a wrong number of tasks each get their own `what_to_do`.
- **A tool input guardrail** (`known_values_only`, the SDK's `@tool_input_guardrail`) stops a query that
  filters an enum column on a value the data never holds, such as `outcome_type = 'Adopted'` instead of
  `'Adoption'`. That query would run fine and return zero rows; instead the researcher gets the list of
  valid values and tries again. Rejected calls still appear in `tool_calls`, marked `rejected_by`.
- **Every parameter is described** with what to pass and an example, and `column_values.table` is an
  enum of the three tables. The researcher's instructions carry four DuckDB few-shot queries (medians,
  percentages with `FILTER`, yearly trends, ratios), each tested against the data.
- **The database is locked down** as in the lecture: read-only, `enable_external_access` off,
  configuration locked, one statement per call, a 20 s timeout, and a row cap.
- **A failed researcher is reported, not hidden.** A timeout, turn cap or model error becomes a
  `FAILED` finding; the Lead is told to re-dispatch it or pass it on, and the Writer lists it under
  "What this data can't tell you".

The web page uses `GET /run`, the same research streamed as server-sent events, so you can watch it happen:
a research board fills in a lane per agent as queries run, citation tags in the report open the exact SQL
and rows behind each number, and "Show what each agent saw" opens the starter's context panel with every
agent's full context.

## Data

**Source:** Austin Animal Center, the largest no-kill shelter in the US, via the City of Austin open data
portal. The shelter changed record systems on 2025-05-05, so the data comes in two eras:

| File | Dataset | Rows | Covers |
|---|---|---|---|
| `old_intakes.csv` | [Intakes 10/01/2013–05/05/2025](https://data.austintexas.gov/d/wter-evkm) | 173,812 | old system |
| `old_outcomes.csv` | [Outcomes 10/01/2013–05/05/2025](https://data.austintexas.gov/d/9t4d-g238) | 173,775 | old system |
| `recent_intakes.csv` | [Intakes, current](https://data.austintexas.gov/d/pyqf-r2dc) | 19,322 | new system, refreshed hourly |
| `recent_outcomes.csv` | [Outcomes, current](https://data.austintexas.gov/d/gsvs-ypi7) | 19,700 | new system, refreshed hourly |

**Why the model doesn't already know it:** the model may know general beliefs about shelters, but not this
shelter's stay lengths by age, breed or intake type, and nothing after its training cutoff. The current
datasets run to the day of the snapshot (2026-11-07).

**Offline build step.** `uv run build_db.py` downloads the four CSVs (if missing) and builds
`data/shelter.duckdb` (8.7 MB, committed, shipped with the deploy):

- `stays` (172,138 rows): each old-system outcome joined to the intake before it (`merge_asof` per
  animal; an animal can come back many times), with `days_in_shelter`, `age_years` parsed from "2 years",
  `age_group`, `is_pit_bull_type`, `primary_color` and more computed once.
- `recent_stays` (19,700 rows): new-system outcomes, with its 28 outcome labels mapped onto the old
  system's groups so the eras can be compared, joined to intakes for the intake source and ZIP.
- `recent_intakes` (19,322 rows): new-system intakes, including animals still in the shelter.

`uv run build_db.py --download` refreshes to today's data.

### How the pairing was checked

Run against `data/shelter.duckdb`:

- **Animals that came back:** 13,332 animals have more than one stay. Each outcome is paired with that
  animal's latest intake before it, and no intake is used twice (0 duplicates). When two outcomes claim
  the same intake (a status corrected later), the later one is kept.
- **Missing data:** outcomes with no earlier intake are dropped, and so are negative stays; no row in
  `stays` or `recent_stays` has a missing `days_in_shelter`. Missing ages give a NULL `age_group`
  (2 dog adoptions), which is left out of age comparisons.
- **Very long stays:** 587 stays (0.3%) are over a year, up to 6.8 years, most likely an intake record
  missing between two visits. The agents report medians, which these few rows barely move.
- **The 2025 switch:** `stays` ends 2025-05-05 07:22 and `recent_stays` starts 2025-05-05 10:08, so the
  eras don't overlap. The new system's 28 outcome labels are mapped onto the old groups, but the two
  eras are reported side by side and labelled, never joined into one trend line.
- **Animals carried over the switch:** `recent_stays` includes animals that arrived before 2025-05-05.
  Only the long-stayers were still there, so their stays are long (adult dogs: median 161.5 days vs 30 for
  dogs that arrived after). The agents are told to measure new-system stays on arrivals after the switch.
- **The sample answers below** were recomputed with plain SQL: baby / adult / senior dog adoptions take a
  median 6.93 / 12.05 / 20.86 days (n = 19,537 / 24,719 / 2,895), and dog and cat adoptions rose from 40.2%
  (2014) to 64.9% (2024).

### What the data can't answer, and the agent says so

- **Temperament, behavior, adopter identity, fees, photos, or life after adoption**: not in the records.
- **Other shelters**: one shelter only; reports say other shelters may differ.
- **Following an animal across the 2025 switch**: old and new IDs differ.
- **Causes**: the data is observational. Reports say "associated with".
- **Breed and color** are what staff recorded by eye; the city noted reporting gaps during the 2025 switch.

These limits are in every agent's instructions; researchers end their findings with a `Limits:` bullet,
and every report ends with a "What this data can't tell you" section.

## Sample queries for the grader

Expected answers are computed directly from `data/shelter.duckdb` (snapshot through 2026-11-07).

**1. What factors are associated with longer shelter stays for dogs?**
Expected: age, breed type and intake type all matter. Median days to adoption, dogs: babies 6.9
(n=19,537), adults 12.1 (n=24,719), seniors 20.9 (n=2,895). Among adult dogs, pit bull-type dogs waited a
median 38.3 days (n=5,411) vs 9.1 for other breeds (n=19,308), about four times as long. By intake type:
Public Assist 25.5, Abandoned 17.9, Owner Surrender 9.3, Stray 9.0.
*Table:* `stays` (`animal_type='Dog' AND outcome_type='Adoption'`).

**2. How do senior cats fare compared with kittens and adult cats?**
Expected: seniors have the worst outcomes, but kittens wait longest. Euthanasia: seniors 12.7% (n=3,799)
vs adults 5.1% (n=19,512) and kittens 2.3% (n=45,254). Adoption: kittens 57.7%, seniors 41.3%, adults
39.7%. Median days to adoption: kittens 28.2, seniors 27.1, adults 17.8. Kittens wait longer than adults,
likely because they stay in foster until they are old enough to be adopted; the agent should flag that
reason as a likely explanation the data only hints at (via the `Foster` outcome subtype), not a fact.
*Table:* `stays` (`animal_type='Cat'`, grouped by `age_group`).

**3. How have adoption and euthanasia rates changed since 2014?**
Expected: for dogs and cats, adoptions rose from 40.2% of outcomes in 2014 to 64.9% in 2024, and
euthanasia fell from 6.0% to 2.3% (low point 1.5% in 2018). In the new system, 2026 so far shows 49.4%
adoptions and 3.5% euthanasia; the agent should note the 2025 system switch makes the eras not directly
comparable.
*Tables:* `stays` and `recent_stays`, grouped by `year(outcome_date)`.

**A vague one, to see clarification:** "Is the shelter doing better?" The Lead should ask what "better"
means (more adoptions, shorter stays, fewer euthanasias) before any research runs.

**A follow-up, to see memory:** after query 1, ask "What about cats?" in the same session. The agent
should run the same stay-length analysis for cats (median days to adoption: kittens 28.2, adults 17.8,
seniors 27.1, from `stays`) without asking what you mean.

### No tools vs. the agent

The same model (Gemini 3.5 Flash) with no tools and no system prompt, next to Pawprint's answer.
Regenerate with `uv run compare.py`.

<!-- COMPARISON:START -->
| Query | Same model, no tools, no system prompt | Pawprint agent |
|---|---|---|
| 1. What factors are associated with longer shelter stays for dogs? | Reducing the **Length of Stay (LOS)** in animal shelters is a primary goal for animal welfare organizations. A longer stay not only strains shelter resources but also increases the risk of a dog experiencing behavioral deterioration, stress-related illness, and depression.  | Longer shelter stays for dogs are primarily associated with being a pit bull type (especially adults and seniors), having a name, and entering the shelter with behavioral issues or being born in care. Conversely, being unnamed, being a puppy fast-tracked for partner transfers, or entering with aged or sick status is associated with shorter stays. Physical characteristics like sex and black coat color show almost no... |
| 2. How do senior cats fare compared with kittens and adult cats? | When comparing **senior cats (aged 11+)** with **kittens (0–1 year)** and **adult cats (1–10 years)**, they fare differently depending on what you are looking for in a companion.  | Senior cats experience the highest euthanasia and Return to Owner (RTO) rates across both eras, but they also have shorter adoption stays than kittens. While kittens wait the longest to be adopted, senior cats take longer to be transferred to other facilities. |
| 3. How have adoption and euthanasia rates changed since 2014? | To understand how companion animal adoption and euthanasia rates have changed since 2014, it is helpful to look at the timeline in three distinct phases: the steady progress of the pre-pandemic years (2014–2019), the unprecedented anomaly of the COVID-19 pandemic (2020), and the post-pandemic shelter crisis (2021–present).  | Over the decade preceding the 2025 record system change, dog and cat adoption rates generally rose while euthanasia rates declined [Q9, Q14]. Under the new system from 2025 to 2026, adoption rates for both species experienced slight declines, while euthanasia rates decreased for dogs and remained relatively stable for cats [Q2, Q11]. These trends are heavily associated with shifts in competing outcomes, such as... |

Full answers, with every tool call, are in [docs/sample_answers.md](docs/sample_answers.md).
<!-- COMPARISON:END -->

## Run it

```bash
gcloud auth application-default login   # Vertex AI credentials for Gemini
uv run build_db.py                      # only if data/shelter.duckdb is missing
uv run app.py                           # http://localhost:8000
```

```bash
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "How do senior cats fare compared with kittens and adult cats?"}' | jq .response -r
```

**No credentials?** `uv run tests/demo_server.py` serves the real page with scripted stand-ins for the
agents, to click through the flow; or put a free Google AI Studio key in `.env` (see `.env.example`) to run
the real agents without gcloud.

**Tests.** `uv run python -m pytest tests -q` runs the whole pipeline offline, with a scripted stand-in
for Gemini against the real data and tools: the Clarifier asks about a vague question and can't ask
twice in a row; a follow-up is briefed with the session's history, and sessions don't leak; researchers
overlap in time; a follow-up round runs; the round cap and the 2-4 first-round rule hold; a failed
researcher is reported; `/chat` keeps its shape with an `agent` on every tool call; tool errors are JSON
with a next step; the guardrail stops `outcome_type = 'Adopted'`; and an invented number is caught and
rewritten. `uv run shelter.py` checks that the SQL
tool blocks writes, multiple statements and file reads.

## Deploy (Cloud Run, continuous deploy from GitHub)

Follows the course guide *Deploying to Cloud Run from GitHub*: Cloud Run → *Connect repository* →
*Continuous deploy from a repository* → Developer Connect → this repo, branch `main`.

- **Build type:** buildpack, entrypoint `uvicorn app:app --host 0.0.0.0 --port $PORT`.
  The database `data/shelter.duckdb` (about 9 MB) is committed, so it ships with every build.
- **Authentication:** *Require authentication* → Identity-Aware Proxy, principal `columbia.edu`.
- **Request timeout:** 600 s (a report takes 1-3 minutes; the agent's own caps keep it under 5).
- **Memory:** 1 GiB.
- **Maximum instances: 1.** Conversation history lives in a SQLite file on the instance, and Cloud Run's
  filesystem is in memory and per instance, so every message in a session must reach the same instance.
  With more than one instance, follow-ups can lose their context.
  Even with one instance, Cloud Run can restart or replace it (after an idle spell or a new deploy),
  and that wipes the history: a follow-up then starts a fresh conversation. Within one sitting the
  instance stays up, and follow-ups on the same `session_id` were tested on the deployed service.
  Keeping history across restarts would need outside storage such as Firestore.
- Gemini runs on Vertex AI in the same project, so no key is needed: the service's default account
  authenticates the calls.

## Files

| File | What it does |
|---|---|
| `app.py` | The four agents, their tools and guardrail, the caps, the session memory, `/chat`, `/run`, `/stats` |
| `shelter.py` | The schema every agent sees, the read-only SQL runner, the query log, the citation check |
| `build_db.py` | Offline build: four CSVs → `data/shelter.duckdb` |
| `index.html` | The page: question, clarification, live research board, cited report |
| `context_panel.py`, `context-panel.js` | The starter's per-agent context view, unchanged |
| `compare.py` | Fills in the no-tools vs. agent comparison above |
| `tests/test_pipeline.py` | Offline end-to-end test with a scripted model |
| `tests/demo_server.py` | The page with scripted agents, no credentials needed |
