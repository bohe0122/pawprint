"""Offline build step: turn Austin Animal Center's four CSV exports into shelter.duckdb.

Run it once:  uv run build_db.py             (downloads any CSV missing from data/raw/)
Refresh:      uv run build_db.py --download  (fetches today's CSVs, then rebuilds)

Inputs, in data/raw/ (from data.austintexas.gov):
  old_intakes.csv      Intakes,  Oct 2013 - May 2025, old system       (dataset wter-evkm)
  old_outcomes.csv     Outcomes, Oct 2013 - May 2025, old system       (dataset 9t4d-g238)
  recent_intakes.csv   Intakes,  May 2025 - today, ShelterBuddy system (dataset pyqf-r2dc)
  recent_outcomes.csv  Outcomes, May 2025 - today, ShelterBuddy system (dataset gsvs-ypi7)

Output: data/shelter.duckdb with three tables the agents query:
  stays           one row per stay in the old system: an outcome joined to the intake before it
  recent_stays    one row per outcome in the new system, joined to its intake where possible
  recent_intakes  one row per intake in the new system, including animals still in the shelter

The work an agent would otherwise redo, badly, in every query happens here once: parsing
"2 years" into 2.0, computing days in shelter, joining intakes to outcomes, and mapping the new
system's 28 outcome labels onto the old system's 6 groups.
"""

import re
import sys
import urllib.request
from pathlib import Path

import duckdb
import pandas as pd

HERE = Path(__file__).parent
RAW = HERE / "data" / "raw"
DB = HERE / "data" / "shelter.duckdb"

DOWNLOADS = {
    "old_intakes.csv": "https://data.austintexas.gov/api/views/wter-evkm/rows.csv?accessType=DOWNLOAD",
    "old_outcomes.csv": "https://data.austintexas.gov/api/v3/views/9t4d-g238/export.csv?accessType=DOWNLOAD",
    "recent_intakes.csv": "https://data.austintexas.gov/api/views/pyqf-r2dc/rows.csv?accessType=DOWNLOAD",
    "recent_outcomes.csv": "https://data.austintexas.gov/api/v3/views/gsvs-ypi7/export.csv?accessType=DOWNLOAD",
}


def download(force: bool) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    for name, url in DOWNLOADS.items():
        path = RAW / name
        if force or not path.exists():
            print(f"downloading {name} ...")
            urllib.request.urlretrieve(url, path)


# --- Shared helpers ---


def age_in_years(text) -> float | None:
    """'2 years' -> 2.0, '3 months' -> 0.25, '1 week' -> 0.019. Negative or unreadable -> None."""
    if not isinstance(text, str):
        return None
    m = re.match(r"(-?\d+)\s+(year|month|week|day)", text)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n < 0:
        return None
    return round(n / {"year": 1, "month": 12, "week": 52.18, "day": 365.25}[unit], 3)


def age_group(years) -> str | None:
    if years is None or pd.isna(years):
        return None
    if years < 1:
        return "baby (<1y)"
    if years < 7:
        return "adult (1-6y)"
    return "senior (7y+)"


def is_pit_bull_type(breed) -> bool:
    """Pit bull and the breeds shelters group with it."""
    return isinstance(breed, str) and bool(re.search(r"pit bull|staffordshire", breed, re.I))


def is_black(color) -> bool:
    """True when the first (primary) color is black: 'Black', 'Black/White', 'Black Brindle'."""
    return isinstance(color, str) and color.split("/")[0].strip().lower().startswith("black")


def austin_zip(address) -> str | None:
    m = re.search(r"\b(7\d{4})\b", address) if isinstance(address, str) else None
    return m.group(1) if m else None


# --- Old system: Oct 2013 - May 2025 ---


def build_stays() -> pd.DataFrame:
    intakes = pd.read_csv(RAW / "old_intakes.csv")
    outcomes = pd.read_csv(RAW / "old_outcomes.csv")
    intakes["intake_date"] = pd.to_datetime(intakes["DateTime"], format="%m/%d/%Y %I:%M:%S %p")
    # '2013-12-02T00:00:00-05:00': keep local wall-clock time, drop the offset
    outcomes["outcome_date"] = pd.to_datetime(outcomes["DateTime"].str[:19])

    # An animal can come back many times. Pair each outcome with the latest intake before it.
    intakes = intakes.sort_values("intake_date")
    outcomes = outcomes.sort_values("outcome_date")
    df = pd.merge_asof(
        outcomes,
        intakes,
        left_on="outcome_date",
        right_on="intake_date",
        by="Animal ID",
        direction="backward",
        suffixes=("_out", "_in"),
    )
    total = len(df)
    df = df[df["intake_date"].notna()].copy()
    # Two outcomes can claim one intake (a status corrected later). Keep the last.
    df = df.sort_values("outcome_date").drop_duplicates(["Animal ID", "intake_date"], keep="last")
    df["days_in_shelter"] = (df["outcome_date"] - df["intake_date"]).dt.total_seconds() / 86400
    df = df[df["days_in_shelter"] >= 0]
    print(f"stays: {len(df):,} of {total:,} outcomes matched to an intake")

    years = df["Age upon Intake"].map(age_in_years)
    breed = df["Breed_in"].fillna(df["Breed_out"])
    color = df["Color_in"].fillna(df["Color_out"])
    sex_in = df["Sex upon Intake"].fillna("Unknown")
    return pd.DataFrame({
        "animal_id": df["Animal ID"],
        "has_name": df["Name_in"].notna() | df["Name_out"].notna(),
        "intake_date": df["intake_date"],
        "outcome_date": df["outcome_date"],
        "days_in_shelter": df["days_in_shelter"].round(2),
        "intake_type": df["Intake Type"],
        "intake_condition": df["Intake Condition"],
        "animal_type": df["Animal Type_in"].fillna(df["Animal Type_out"]),
        "sex": sex_in.str.extract(r"(Male|Female)", expand=False).fillna("Unknown"),
        "fixed_at_intake": sex_in.str.contains("Neutered|Spayed"),
        "fixed_at_outcome": df["Sex upon Outcome"].fillna("").str.contains("Neutered|Spayed"),
        "age_years": years,
        "age_group": years.map(age_group),
        "breed": breed,
        "is_mix": breed.fillna("").str.contains(r"Mix|/", regex=True),
        "is_pit_bull_type": breed.map(is_pit_bull_type),
        "color": color,
        "primary_color": color.fillna("").str.split("/").str[0].str.strip(),
        "is_black": color.map(is_black),
        "outcome_type": df["Outcome Type"],
        "outcome_subtype": df["Outcome Subtype"],
        "found_location": df["Found Location"],
    })


# --- New system (ShelterBuddy): May 2025 - today ---

# The new system's outcome labels, grouped like the old system's Outcome Type.
OUTCOME_GROUPS = {
    "Adoption": ["Adopted", "Adopted Altered", "Adopted Unaltered", "Adopted Offsite", "Adopted Offsite(Altered)",
                 "Adopted Offsite(Unaltered)", "Adopted to Finder"],
    "Transfer": ["Transfer Out"],
    "Return to Owner": ["Reclaimed", "Returned To Owner", "Redemption (Offsite)", "DOA - Released to Owner"],
    "Euthanasia": ["Euthanized", "Euthanasia - RTO"],
    "Died": ["Unassisted Death", "Unassisted Death - In Transit", "Unassisted Death - In Foster", "DOA",
             "DOA - Final Disposition", "Interred", "Cremated", "Cremation - Communal"],
    "Wildlife release": ["Return to Habitat"],
}
OUTCOME_GROUP = {label: group for group, labels in OUTCOME_GROUPS.items() for label in labels}

# The new system splits Kitten from Cat and Puppy from Dog. Fold them back to match the old one.
SPECIES = {"Kitten": "Cat", "Cat": "Cat", "Puppy": "Dog", "Dog": "Dog"}


def species(kind) -> str:
    if kind in SPECIES:
        return SPECIES[kind]
    if isinstance(kind, str) and "Bird" in kind:
        return "Bird"
    return "Other"


INTAKE_GROUPS = {
    "Stray": ["Stray", "ACO - Impound", "Abandoned", "Humane Officer", "Community Animal - Field Services",
              "Community Animal - General Public"],
    "Owner Surrender": ["Owner Surrender", "ACO - Owner Surrender", "Ambulance - Owner Surrender",
                        "Humane Officer Surrendered"],
    "Community cat program": ["Community Animal - Program"],
    "Returned after adoption": ["Returns"],
    "Born in care": ["Shelter Offspring", "Foster Offspring", "Cruelty Case Offspring"],
}
INTAKE_GROUP = {label: group for group, labels in INTAKE_GROUPS.items() for label in labels}


def build_recent_intakes() -> pd.DataFrame:
    df = pd.read_csv(RAW / "recent_intakes.csv")
    intake_date = pd.to_datetime(df["Source Date"], format="%m/%d/%Y %I:%M:%S %p")
    dob = pd.to_datetime(df["Date of Birth"], format="%m/%d/%Y %I:%M:%S %p", errors="coerce")
    years = ((intake_date - dob).dt.days / 365.25).round(3).where(lambda s: s >= 0)
    color = df["Primary Color"]
    return pd.DataFrame({
        "animal_id": df["Animal ID"].astype(str),
        "intake_date": intake_date,
        "intake_source": df["Source Name"],
        "intake_group": df["Source Name"].map(INTAKE_GROUP).fillna("Other"),
        "animal_type": df["Type"].map(species),
        "type_detail": df["Type"],
        "has_name": df["Name"].notna(),
        "sex": df["Sex"].fillna("Unknown"),
        "fixed_at_intake": df["IsPreviouslySpayedNeutered"].fillna(False).astype(bool),
        "age_years": years,
        "age_group": years.map(age_group),
        "breed": df["Primary Breed"],
        "is_pit_bull_type": df["Primary Breed"].map(is_pit_bull_type),
        "primary_color": color,
        "secondary_color": df["Secondary Color"],
        "is_black": color.map(is_black),
        "intake_health": df["Intake Health Condition"],
        "found_zip": df["Found Address"].map(austin_zip),
    }).sort_values("intake_date")


def build_recent_stays(recent_intakes: pd.DataFrame) -> pd.DataFrame:
    df = pd.read_csv(RAW / "recent_outcomes.csv")
    outcome_date = pd.to_datetime(df["Outcome Date"], format="%Y %b %d %I:%M:%S %p", errors="coerce")
    intake_date = pd.to_datetime(df["Intake Date"], format="%Y %b %d %I:%M:%S %p", errors="coerce")
    dob = pd.to_datetime(df["Date of Birth"], format="%Y %b %d %I:%M:%S %p", errors="coerce")
    days = pd.to_numeric(df["Days in Shelter"].astype(str).str.replace(",", ""), errors="coerce")
    years = ((intake_date - dob).dt.days / 365.25).round(3).where(lambda s: s >= 0)
    color = df["Primary Color"]
    out = pd.DataFrame({
        "animal_id": df["Animal ID"].astype(str),
        "intake_date": intake_date,
        "outcome_date": outcome_date,
        "days_in_shelter": days,
        "outcome_status": df["Outcome Status"],
        "outcome_type": df["Outcome Status"].map(OUTCOME_GROUP).fillna("Other"),
        "euthanasia_reason": df["Euthanasia Reason"],
        "animal_type": df["Type"].map(species),
        "type_detail": df["Type"],
        "has_name": df["Name"].notna(),
        "sex": df["Sex"].fillna("Unknown"),
        "fixed_at_outcome": df["Spayed/Neutered"].eq("Yes"),
        "age_years": years,
        "age_group": years.map(age_group),
        "breed": df["Primary Breed"],
        "is_pit_bull_type": df["Primary Breed"].map(is_pit_bull_type),
        "primary_color": color,
        "secondary_color": df["Secondary Color"],
        "is_black": color.map(is_black),
    })
    # Bring in how the animal arrived: the intake with the same animal ID nearest the stay's start.
    left = out[out["intake_date"].notna()].sort_values("intake_date")
    right = recent_intakes[["animal_id", "intake_date", "intake_source", "intake_group", "found_zip"]].rename(
        columns={"intake_date": "matched_intake_date"}
    )
    joined = pd.merge_asof(
        left, right.sort_values("matched_intake_date"), left_on="intake_date", right_on="matched_intake_date",
        by="animal_id", direction="nearest", tolerance=pd.Timedelta(days=2),
    ).drop(columns="matched_intake_date")
    rest = out[out["intake_date"].isna()]
    joined = pd.concat([joined, rest], ignore_index=True)
    print(f"recent_stays: {len(joined):,} outcomes, {joined['intake_source'].notna().mean():.0%} matched to an intake")
    return joined


def main():
    download(force="--download" in sys.argv)
    stays = build_stays()
    recent_intakes = build_recent_intakes()
    recent_stays = build_recent_stays(recent_intakes)
    print(f"recent_intakes: {len(recent_intakes):,}")

    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    for name, frame in [("stays", stays), ("recent_stays", recent_stays), ("recent_intakes", recent_intakes)]:
        # Nullable dtypes, so a missing value lands in DuckDB as NULL rather than NaN
        frame = frame.convert_dtypes()
        con.register("frame", frame)
        con.execute(f"CREATE TABLE {name} AS SELECT * FROM frame")
        con.unregister("frame")
    # The newest record: what "today" means for this snapshot
    snapshot = con.execute(
        "SELECT max(d) FROM (SELECT max(outcome_date) d FROM recent_stays UNION ALL SELECT max(intake_date) FROM recent_intakes)"
    ).fetchone()[0]
    con.execute("CREATE TABLE snapshot AS SELECT ?::TIMESTAMP AS data_through", [snapshot])
    con.close()
    print(f"wrote {DB} ({DB.stat().st_size / 1e6:.1f} MB), data through {snapshot}")


if __name__ == "__main__":
    main()
