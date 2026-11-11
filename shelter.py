"""The agents' window onto data/shelter.duckdb: what the tables hold, a read-only SQL runner that
numbers every query so reports can cite it, and a check that a report's numbers really come
from the queries it cites.
"""

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

DB = Path(__file__).parent / "data" / "shelter.duckdb"
MAX_ROWS = 40  # rows a query returns to the model. Aggregate in SQL instead of reading rows.
TIMEOUT_S = 20

DATASETS = {
    "stays": "https://data.austintexas.gov/d/9t4d-g238 (outcomes) joined to https://data.austintexas.gov/d/wter-evkm (intakes)",
    "recent_stays": "https://data.austintexas.gov/d/gsvs-ypi7 (outcomes) joined to https://data.austintexas.gov/d/pyqf-r2dc (intakes)",
    "recent_intakes": "https://data.austintexas.gov/d/pyqf-r2dc",
}

# Everything an agent needs to write correct SQL, in one place. Both the Lead and every Researcher
# get it in their instructions, so a fresh-context Researcher never has to rediscover it.
SCHEMA = """\
DATA: Austin Animal Center (Austin, TX), the largest no-kill shelter in the US. DuckDB, 3 tables.
The shelter switched record systems on 2025-05-05, so there are two eras that do NOT share animal IDs.

TABLE stays  -- old system, one row per stay, intakes 2013-10-01 to 2025-05-04 (~172,000 rows)
  animal_id VARCHAR ('A664235'), has_name BOOLEAN (named at intake or outcome)
  intake_date, outcome_date TIMESTAMP, days_in_shelter DOUBLE (outcome minus intake, fractional days)
  intake_type VARCHAR: Stray | Owner Surrender | Public Assist | Wildlife | Abandoned | Euthanasia Request
  intake_condition VARCHAR: Normal | Injured | Sick | Nursing | Aged | Neonatal | Feral | Pregnant | ...
  animal_type VARCHAR: Dog | Cat | Other | Bird | Livestock
  sex VARCHAR: Male | Female | Unknown; fixed_at_intake, fixed_at_outcome BOOLEAN (spayed/neutered)
  age_years DOUBLE (age at intake); age_group VARCHAR: 'baby (<1y)' | 'adult (1-6y)' | 'senior (7y+)'
  breed VARCHAR ('Pit Bull Mix', 'Domestic Shorthair Mix', 'Labrador Retriever/Pit Bull')
  is_mix BOOLEAN; is_pit_bull_type BOOLEAN (breed mentions Pit Bull or Staffordshire)
  color VARCHAR ('Black/White'); primary_color VARCHAR (first color: 'Black', 'Brown Tabby')
  is_black BOOLEAN (primary color starts with Black)
  outcome_type VARCHAR: Adoption | Transfer | Return to Owner | Euthanasia | Died | Rto-Adopt | Disposal | Missing | Relocate | Stolen | Lost
  outcome_subtype VARCHAR: Partner | Foster | SCRP | Suffering | Rabies Risk | Aggressive | Snr | Offsite | ...
  found_location VARCHAR (free text, e.g. '7201 Levander Loop in Austin (TX)')

TABLE recent_stays  -- new system, one row per outcome, outcomes 2025-05-05 to the snapshot date (~19,700 rows)
  animal_id VARCHAR (numeric string), intake_date, outcome_date TIMESTAMP
  days_in_shelter DOUBLE (whole days, reported by the shelter)
  outcome_status VARCHAR (the new system's 28 raw labels, e.g. 'Adopted Altered', 'Transfer Out')
  outcome_type VARCHAR (grouped like the old system): Adoption | Transfer | Return to Owner | Euthanasia | Died | Wildlife release | Other
  euthanasia_reason VARCHAR
  animal_type VARCHAR: Dog | Cat | Bird | Other (puppies and kittens folded in); type_detail VARCHAR (raw: Puppy, Kitten, Rabbit, ...)
  has_name, fixed_at_outcome BOOLEAN; sex VARCHAR; age_years DOUBLE (from date of birth); age_group as above
  breed VARCHAR (primary breed only, e.g. 'American Pit Bull Terrier', 'Mixed Breed'), is_pit_bull_type BOOLEAN
  primary_color, secondary_color VARCHAR (capitalization varies: 'BLUE TIGER'), is_black BOOLEAN
  intake_source VARCHAR (raw), intake_group VARCHAR: Stray | Owner Surrender | Community cat program | Returned after adoption | Born in care | Other
  found_zip VARCHAR (5-digit ZIP where found)
  -- 933 rows have intake_date before 2025-05-05: animals already in the shelter at the switch, carried over.
  -- ~7% of rows have no matched intake, so intake_source/intake_group/found_zip are NULL there.

TABLE recent_intakes  -- new system, one row per intake, 2025-05-04 to the snapshot date (~19,300 rows)
  animal_id, intake_date, intake_source, intake_group, animal_type, type_detail, has_name, sex,
  fixed_at_intake, age_years, age_group, breed, is_pit_bull_type, primary_color, secondary_color, is_black,
  intake_health VARCHAR (84% NULL: rarely filled in), found_zip
  -- includes animals still in the shelter, so use it for intake counts, not outcomes.

TABLE snapshot (data_through TIMESTAMP): the newest record. Treat it as "today" for this data.

KNOWN LIMITS (say so when they matter):
- Old and new IDs differ, so an animal can't be followed across the 2025-05-05 switch.
- Breed and color are what staff recorded by eye; 'breed' is a guess for mixed animals.
- No data on temperament, behavior, adopter identity, adoption fees, photos, or what happened after adoption.
- Only Austin Animal Center: no other shelters or cities.
- The city noted reporting gaps during the 2025 system switch; treat May-Aug 2025 counts with care.
- Fewer than 1% of old-system outcomes had no matching intake and were dropped.
- fixed_at_outcome is mostly a result of the outcome, not a predictor: the shelter spays/neuters animals
  before adoption, while owners reclaiming a pet often take it home unfixed. Don't present
  "left fixed" as a reason animals got adopted; use fixed_at_intake to compare animals as they arrived.
- Outcomes compete: a group with many Return to Owner outcomes has a lower adoption *rate* even if the
  animals still in the adoption pool are adopted just as readily. Say so when comparing adoption rates.
- recent_stays includes animals that entered before 2025-05-05 and left after it (carried over). Their
  stays are long by construction (only the long-stayers were still there), so for stay lengths in the
  new system use intake_date >= '2025-05-05' and say so; report carried-over animals separately.
- Adoption means outcome_type = 'Adoption' only. 'Rto-Adopt' (old system) is an owner taking back its
  own pet through the adoption desk: count it with Return to Owner, never as an adoption.
- No outcome after leaving: a return to THIS shelter is the only signal. An animal that never came
  back may still have been rehomed, lost, or taken elsewhere.
- Partial years: stays covers only Oct-Dec of 2013 and Jan-May 4 of 2025; recent_stays covers 2025 from
  May 5, and the current year only up to the snapshot. Start yearly trends at 2014, and label partial
  years as partial whenever you report them.
- 0.3% of stays exceed a year (likely a missed intake between two visits); prefer medians to means.
"""


@dataclass
class Query:
    id: str
    agent: str
    purpose: str
    sql: str
    result: str
    rows: int


@dataclass
class QueryLog:
    """Every query one research run made, numbered Q1, Q2, ... in the order they ran."""

    queries: dict[str, Query] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, agent: str, purpose: str, sql: str, result: str, rows: int) -> Query:
        with self._lock:
            qid = f"Q{len(self.queries) + 1}"
            query = Query(qid, agent, purpose, sql, result, rows)
            self.queries[qid] = query
            return query


_con = None


def _connection():
    """One read-only connection per process. It can't write, and can't read files outside the database."""
    global _con
    if _con is None:
        # As in the Text2SQL lecture: read-only, no file or network access, settings locked
        _con = duckdb.connect(
            str(DB), read_only=True, config={"enable_external_access": False, "lock_configuration": True}
        )
    return _con


def _format(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".") if abs(value) < 1e15 else str(value)
    return str(value)


def _json_value(value):
    """A cell as JSON: floats rounded the way the text table shows them, dates as strings."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return round(value, 2)
    return str(value)


@dataclass
class Result:
    columns: list[str]
    rows: list[list]  # at most MAX_ROWS, JSON-ready
    truncated: bool
    text: str  # the same rows as a pipe table: what the page shows and the citation check reads


def query(sql: str) -> Result:
    """Run one read-only SELECT. Raises ValueError for anything that isn't a single SELECT, and
    duckdb errors as they come."""
    statement = sql.strip().rstrip(";").strip()
    if ";" in statement:
        raise ValueError("Send one statement at a time.")
    if not re.match(r"(?is)^\s*(select|with)\b", statement):
        raise ValueError("Only SELECT (or WITH ... SELECT) queries are allowed.")

    cursor = _connection().cursor()  # a cursor per call, so parallel researchers don't share one
    timer = threading.Timer(TIMEOUT_S, cursor.interrupt)
    timer.start()
    try:
        relation = cursor.execute(statement)
        columns = [d[0] for d in relation.description]
        rows = relation.fetchmany(MAX_ROWS + 1)
    finally:
        timer.cancel()
        cursor.close()

    shown = rows[:MAX_ROWS]
    truncated = len(rows) > MAX_ROWS
    lines = [" | ".join(columns)] + [" | ".join(_format(v) for v in row) for row in shown]
    note = f"\n(first {MAX_ROWS} rows shown: aggregate or add LIMIT)" if truncated else ""
    return Result(columns, [[_json_value(v) for v in row] for row in shown], truncated, "\n".join(lines) + note)


def run_query(sql: str) -> tuple[str, int]:
    """query(), as (text table, row count)."""
    result = query(sql)
    return result.text, len(result.rows)


# --- Enum values, for the guardrail: a filter on a value the data doesn't hold matches no rows ---

# Text columns with a short, fixed set of values. A typo in one of them ('Adopted' for 'Adoption')
# returns zero rows without any error, so run_sql's guardrail checks them before the query runs.
ENUM_COLUMNS = [
    "animal_type", "outcome_type", "intake_type", "intake_condition", "age_group", "sex",
    "intake_group", "outcome_status", "type_detail", "outcome_subtype",
]
_enums: dict[str, set[str]] | None = None


def enum_values() -> dict[str, set[str]]:
    """Every value each enum column holds, across all tables that have it."""
    global _enums
    if _enums is None:
        con = _connection()
        _enums = {}
        for column in ENUM_COLUMNS:
            tables = [r[0] for r in con.execute(
                "SELECT table_name FROM information_schema.columns WHERE column_name = ?", [column]).fetchall()]
            values = set()
            for table in tables:
                values |= {r[0] for r in con.execute(f"SELECT DISTINCT {column} FROM {table}").fetchall() if r[0] is not None}
            _enums[column] = values
    return _enums


# --- Citation check ---

CITE = re.compile(r"\[(Q\d+)\]")
NUMBER = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> list[float]:
    values = []
    for raw in NUMBER.findall(text):
        try:
            values.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    return values


def _matches(claimed: float, found: list[float]) -> bool:
    """A number in the report matches a result when it is the same value after rounding, or the same
    share written as a percentage (0.42 in the result, 42% in the report)."""
    for value in found:
        for candidate in (value, value * 100):
            tolerance = max(0.51 if abs(claimed) >= 1 else 0.051, abs(candidate) * 0.005)
            if abs(candidate - claimed) <= tolerance:
                return True
    return False


def check_citations(report: str, log: QueryLog) -> dict:
    """For every sentence that cites [Q#]: does each cited query exist, and does each number the
    sentence states appear in a cited query's result? Years and the query numbers themselves are skipped."""
    sentences = re.split(r"(?<=[.!?])\s+|\n+", report)
    unknown, unverified, checked = set(), [], 0
    for sentence in sentences:
        cited = CITE.findall(sentence)
        if not cited:
            continue
        unknown |= {q for q in cited if q not in log.queries}
        found = [n for q in cited if q in log.queries for n in _numbers(log.queries[q].result)]
        text = CITE.sub("", sentence)
        for claimed in _numbers(text):
            if 1990 <= claimed <= 2030 and claimed == int(claimed):
                continue  # a year
            checked += 1
            if not _matches(claimed, found):
                unverified.append({"number": claimed, "sentence": sentence.strip()[:200], "cites": cited})
    return {
        "cited": sorted(set(CITE.findall(report)), key=lambda q: int(q[1:])),
        "unknown": sorted(unknown),
        "numbers_checked": checked,
        "unverified": unverified,
    }


if __name__ == "__main__":
    # Smoke test: uv run shelter.py
    text, n = run_query(
        "SELECT is_black, median(days_in_shelter) AS median_days, count(*) AS n FROM stays "
        "WHERE animal_type='Dog' AND outcome_type='Adoption' AND age_group='adult (1-6y)' GROUP BY 1 ORDER BY 1"
    )
    print(text)
    for bad in ["DELETE FROM stays", "SELECT 1; SELECT 2", "SELECT * FROM read_csv('/etc/passwd')"]:
        try:
            run_query(bad)
            print("NOT BLOCKED:", bad)
        except Exception as e:
            print("blocked:", bad, "->", type(e).__name__)
