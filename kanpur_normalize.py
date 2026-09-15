"""
Kanpur grievance dump normalisation.

Two responsibilities, deliberately separated:

  load_definitions()  -- pulls the three master tabs from the published sheet
  normalize_dump()    -- turns a raw portal export into a tidy analysis frame

The analysis category is DERIVED from `Sub Category` via the master table.
The dump's own `Category` column is preserved as `category_raw` for audit but
is never used for analysis -- Malba tickets arrive labelled "Engineering".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pandas as pd

# ---------------------------------------------------------------- definitions

SHEET_PUB = (
    "https://docs.google.com/spreadsheets/d/e/"
    "2PACX-1vTF0TbAXN6O1-Te6vCMlE9r-BbKb7Z-TrFVm2FMPx-ioorWRJFXM4mjqSy-VbxlSh4nCBlF6PtnMbyv"
    "/pub?gid={gid}&single=true&output=csv"
)

GID_COLUMNS = 0
GID_STATUS = 907869929
GID_CATEGORY = 553718957

# Raw portal sub-category string -> master sub-category label.
# Empty as of 2026-09-09: the master sheet now carries the portal's exact
# wording, so `_norm_key` alone resolves every row. Only add an entry here if
# the portal changes a label and the sheet can't be updated to match.
SUBCATEGORY_ALIASES: dict[str, str] = {}

EXPECTED_COLUMNS = [
    "Comp. No.", "Category", "Sub Category", "Zone & Ward",
    "Complainer Name & Mob. No.", "Concern Person Name & Mob. No.",
    "Date & Time", "Date of Closure", "Description", "Status",
]

DATE_FORMAT = "%d %b %Y %I:%M %p"


@dataclass
class Definitions:
    """Master tables from the definitions sheet."""
    columns: list[str]
    statuses: list[str]
    subcategory_to_category: dict[str, str]   # normalised key -> category
    subcategory_label: dict[str, str]         # normalised key -> master label

    @property
    def categories(self) -> list[str]:
        seen: list[str] = []
        for cat in self.subcategory_to_category.values():
            if cat not in seen:
                seen.append(cat)
        return seen


def _norm_key(value: object) -> str:
    """Aggressive key for joining category strings.

    Casefolds, strips accents, and drops every non-alphanumeric character, so
    'Removal of Debris' and 'Removal of debris' collide -- but genuinely
    different wording still does not, which is the point.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _parse_category_frame(cat: pd.DataFrame) -> tuple[dict, dict]:
    """Turn a Category/Sub category(/Portal label) table into lookup dicts."""
    cat = cat.copy()
    cat.columns = [str(c).strip() for c in cat.columns]
    cat = cat.rename(columns={"Sub category": "Sub Category"})
    # Category is merged-cell style: only the first row of each block is filled.
    cat["Category"] = cat["Category"].ffill()
    cat = cat.dropna(subset=["Sub Category"])
    cat["Sub Category"] = cat["Sub Category"].astype(str).str.strip()
    cat = cat[cat["Sub Category"] != ""]

    # Optional third column: the exact label the portal emits. The portal uses
    # several labels for the same sub-category ('Dustbin' / 'Dustbins not
    # cleaned'), so one display label can have many portal labels -- one row
    # each. Detected by name, else by position.
    portal_col = next(
        (c for c in cat.columns
         if re.search(r"portal|raw|export|alias|as in dump", str(c), re.I)),
        None,
    )
    if portal_col is None and len(cat.columns) > 2:
        third = cat.columns[2]
        if not str(third).startswith("Unnamed") and cat[third].notna().any():
            portal_col = third

    sub_to_cat, sub_label = {}, {}
    for _, row in cat.iterrows():
        category = str(row["Category"]).strip()
        display = row["Sub Category"]
        join_values = [display]
        if portal_col is not None and pd.notna(row.get(portal_col)):
            raw = str(row[portal_col]).strip()
            if raw:
                # Allow several variants in one cell, separated by | or ;
                join_values = [v.strip() for v in re.split(r"[|;]", raw) if v.strip()]
        for value in join_values:
            key = _norm_key(value)
            if key:
                sub_to_cat[key] = category
                sub_label[key] = display

    return sub_to_cat, sub_label


def load_definitions(sheet_pub: str = SHEET_PUB) -> Definitions:
    """Read the three master tabs from the published sheet.

    Needs outbound access to docs.google.com. On a restricted office network
    this raises, and callers should fall back to load_definitions_file() or
    fallback_definitions().
    """
    columns = (
        pd.read_csv(sheet_pub.format(gid=GID_COLUMNS))
        .iloc[:, 0].dropna().astype(str).str.strip().tolist()
    )
    statuses = (
        pd.read_csv(sheet_pub.format(gid=GID_STATUS))
        .iloc[:, 0].dropna().astype(str).str.strip().tolist()
    )
    sub_to_cat, sub_label = _parse_category_frame(
        pd.read_csv(sheet_pub.format(gid=GID_CATEGORY))
    )
    return Definitions(columns, statuses, sub_to_cat, sub_label)


DEFINITIONS_FILENAMES = ("definitions.csv", "definitions.xlsx")


def load_definitions_file(path) -> Definitions:
    """Read definitions from a local CSV/XLSX with the same shape as the tab.

    For machines that cannot reach the published sheet: download the Category
    tab as CSV, drop it next to app.py as definitions.csv, and the app uses it
    instead. Statuses stay at the standard three.
    """
    path = str(path)
    if path.lower().endswith((".xlsx", ".xlsm", ".xls")):
        cat = pd.read_excel(path)
    else:
        cat = pd.read_csv(path)
    sub_to_cat, sub_label = _parse_category_frame(cat)
    if not sub_to_cat:
        raise ValueError(f"No usable Category/Sub category rows found in {path}")
    return Definitions(
        list(EXPECTED_COLUMNS), list(FALLBACK_STATUSES), sub_to_cat, sub_label
    )


def load_definitions_bytes(data: bytes, filename: str) -> Definitions:
    """Same as load_definitions_file, for an uploaded definitions file."""
    import io
    buf = io.BytesIO(data)
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        cat = pd.read_excel(buf)
    else:
        cat = pd.read_csv(buf)
    sub_to_cat, sub_label = _parse_category_frame(cat)
    if not sub_to_cat:
        raise ValueError("No usable Category/Sub category rows in the upload")
    return Definitions(
        list(EXPECTED_COLUMNS), list(FALLBACK_STATUSES), sub_to_cat, sub_label
    )


def suggest_match(value: str, defs: Definitions, cutoff: float = 0.72) -> str | None:
    """Closest known sub-category label for an unmapped value, or None.

    Display-only: this is shown to the user so typo variants are easy to spot.
    It is NEVER applied automatically -- a wrong auto-match would silently
    misattribute tickets between categories, which is the failure this whole
    mapping design exists to prevent.
    """
    import difflib

    keys = list(defs.subcategory_label)
    hit = difflib.get_close_matches(_norm_key(value), keys, n=1, cutoff=cutoff)
    return defs.subcategory_label[hit[0]] if hit else None


def distinct_subcategories(df: pd.DataFrame) -> pd.DataFrame:
    """Every Sub Category value in a raw export, with counts.

    Use this against the largest export available to build the mapping sheet
    from the portal's actual vocabulary instead of by hand.
    """
    col = "Sub Category" if "Sub Category" in df.columns else df.columns[2]
    out = (
        df[col].dropna().astype(str).str.strip()
        .value_counts().rename_axis("Sub Category").reset_index(name="Tickets")
    )
    if "Category" in df.columns:
        first = (
            df.assign(**{col: df[col].astype(str).str.strip()})
            .groupby(col)["Category"].first()
        )
        out["Category in export"] = out["Sub Category"].map(first)
    return out


# ------------------------------------------------------------------ splitting

_MOBILE_RE = re.compile(r"^\+?\d[\d\s\-]{7,}$")


def split_name_mobile(value: object) -> tuple[str | None, str | None]:
    """'Er. Minakshi Agrawal & 9411062529' -> ('Er. Minakshi Agrawal', '9411062529').

    Splits on the LAST '&' and only accepts the right side as a mobile number
    if it actually looks like one -- so a name containing '&' stays intact and
    a missing number yields (name, None) rather than a mangled pair.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None, None
    if "&" not in text:
        return text or None, None
    left, right = text.rsplit("&", 1)
    left, right = left.strip(), right.strip()
    if _MOBILE_RE.match(right):
        return (left or None), re.sub(r"[\s\-]", "", right)
    return text, None


def split_zone_ward(value: object) -> tuple[str | None, str | None, int | None]:
    """'Zone 4 & Mcrobertganj-15' -> ('Zone 4', 'Mcrobertganj', 15).

    Ward number is taken from the LAST hyphen so hyphenated ward names survive.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None, None, None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None, None, None

    if "&" in text:
        zone, ward = (part.strip() for part in text.split("&", 1))
    elif re.match(r"(?i)^zone\b", text):
        zone, ward = text, None      # 'Zone 5' with no ward attached
    else:
        zone, ward = None, text

    ward_no = None
    if ward and "-" in ward:
        head, tail = ward.rsplit("-", 1)
        if tail.strip().isdigit():
            ward, ward_no = head.strip(), int(tail.strip())

    return (zone or None), (ward or None), ward_no


# ---------------------------------------------------------------- main loader

@dataclass
class LoadReport:
    """Everything that needs a human decision, surfaced rather than swallowed."""
    rows_in: int = 0
    rows_out: int = 0
    unmapped_subcategories: dict[str, int] = field(default_factory=dict)
    unknown_statuses: dict[str, int] = field(default_factory=dict)
    unparsed_opened: int = 0
    unparsed_closed: int = 0
    closed_before_opened: int = 0
    missing_columns: list[str] = field(default_factory=list)
    found_columns: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (
            self.unmapped_subcategories
            or self.unknown_statuses
            or self.missing_columns
            or self.unparsed_opened
            or self.closed_before_opened
        )

    def render(self) -> str:
        lines = [f"{self.rows_in} rows in, {self.rows_out} rows out"]
        if self.missing_columns:
            lines.append(f"MISSING COLUMNS: {', '.join(self.missing_columns)}")
            lines.append(f"columns found instead: {', '.join(self.found_columns) or '(none)'}")
        for label, mapping in (
            ("UNMAPPED sub-category", self.unmapped_subcategories),
            ("UNKNOWN status", self.unknown_statuses),
        ):
            for value, count in sorted(mapping.items(), key=lambda kv: -kv[1]):
                lines.append(f"{label}: {value!r} ({count} rows)")
        if self.unparsed_opened:
            lines.append(f"unparseable 'Date & Time': {self.unparsed_opened}")
        if self.unparsed_closed:
            lines.append(
                f"blank/unparseable 'Date of Closure': {self.unparsed_closed} "
                "(expected for Pending)"
            )
        if self.closed_before_opened:
            lines.append(f"closure BEFORE opening: {self.closed_before_opened}")
        if self.is_clean:
            lines.append("clean")
        return "\n".join(lines)


def _clean_header(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()


def _header_row_index(raw: pd.DataFrame, max_scan: int = 12) -> int:
    """Find which row holds the column headers.

    The portal sometimes emits a banner row ('Complaints List ::Online
    Services') above the header and sometimes does not, so the header is on
    row 1 or row 2 depending on the export. Rather than assume either, score
    the first few rows by how many expected column names they contain and
    take the best. Falls back to row 0 if nothing scores, which is the
    no-banner case.
    """
    wanted = {_norm_key(c) for c in EXPECTED_COLUMNS}
    best_row, best_score = 0, 0
    for i in range(min(max_scan, len(raw))):
        cells = [_norm_key(_clean_header(v)) for v in raw.iloc[i].tolist()]
        score = sum(1 for c in cells if c and c in wanted)
        if score > best_score:
            best_row, best_score = i, score
    # Two matches is enough to beat a banner row, which matches none.
    return best_row if best_score >= 2 else 0


def _frame_from_raw(raw: pd.DataFrame) -> pd.DataFrame:
    """Turn a header-less read into a tidy frame with real column names."""
    idx = _header_row_index(raw)
    header = [_clean_header(v) for v in raw.iloc[idx].tolist()]
    df = raw.iloc[idx + 1:].copy()
    df.columns = header
    # Drop the phantom leading column (its header is a non-breaking space) and
    # any unnamed or all-empty trailing columns.
    keep = [
        c for i, c in enumerate(df.columns)
        if c and not str(c).lower().startswith("unnamed")
    ]
    df = df.loc[:, keep]
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    return df.dropna(how="all").reset_index(drop=True)


def read_raw(path_or_url: str) -> pd.DataFrame:
    """Read a portal export from a path or URL.

    Handles both shapes the portal produces: with a banner row above the
    header, and without one.
    """
    if str(path_or_url).lower().endswith((".xlsx", ".xlsm", ".xls")):
        raw = pd.read_excel(path_or_url, header=None, dtype=object)
    else:
        raw = pd.read_csv(path_or_url, header=None, dtype=object)
    return _frame_from_raw(raw)


def read_upload(data: bytes, filename: str) -> pd.DataFrame:
    """Same as read_raw, but for an in-memory upload (Streamlit file_uploader)."""
    import io
    buf = io.BytesIO(data)
    if filename.lower().endswith((".xlsx", ".xlsm", ".xls")):
        raw = pd.read_excel(buf, header=None, dtype=object)
    else:
        raw = pd.read_csv(buf, header=None, dtype=object)
    return _frame_from_raw(raw)


# Snapshot of the master tables, matching the sheet as published 2026-09-10.
# Used only if neither the sheet nor a local definitions file is available.
# Superseded by the published
# sheet is unreachable. Keep in sync with the sheet; the app flags when it
# falls back to this.
FALLBACK_CATEGORY_ROWS = [
    ("Sanitation", "Burning of garbage in open space"),
    ("Sanitation", "Dustbins not cleaned"),
    ("Sanitation", "Garbage dump"),
    ("Sanitation", "Garbage Lifting"),
    ("Sanitation", "Debris Removal/Construction Material"),
    ("Sanitation", "Garbage not cleaned"),
    ("Sanitation", "Sweeping not done"),
    ("Engineering", "Pot hole fill up/Repairs to the damage surface"),
    ("Engineering", "Repairing"),
    ("Engineering", "Disposal of removed silt on the Road"),
    ("Malba", "Removal of debris"),
]
FALLBACK_STATUSES = ["Pending", "Rejected", "Completed"]


def fallback_definitions() -> Definitions:
    return Definitions(
        columns=list(EXPECTED_COLUMNS),
        statuses=list(FALLBACK_STATUSES),
        subcategory_to_category={_norm_key(s): c for c, s in FALLBACK_CATEGORY_ROWS},
        subcategory_label={_norm_key(s): s for c, s in FALLBACK_CATEGORY_ROWS},
    )


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Column `name`, or an all-empty Series of the right length if absent.

    df.get() returns None for a missing column, and None flowing into
    pd.to_datetime yields a scalar NaT rather than a Series -- which then
    fails on .isna(). Returning a real Series keeps a missing column as a
    reportable condition instead of a crash.
    """
    if name in df.columns:
        return df[name]
    return pd.Series(pd.NA, index=df.index, dtype="object")


def normalize_dump(
    df: pd.DataFrame,
    defs: Definitions,
    aliases: dict[str, str] | None = None,
) -> tuple[pd.DataFrame, LoadReport]:
    aliases = SUBCATEGORY_ALIASES if aliases is None else aliases
    alias_by_key = {_norm_key(k): v for k, v in aliases.items()}

    report = LoadReport(rows_in=len(df))
    report.found_columns = [str(c) for c in df.columns]
    report.missing_columns = [c for c in EXPECTED_COLUMNS if c not in df.columns]

    out = pd.DataFrame(index=df.index)
    out["comp_no"] = _col(df, "Comp. No.")
    out["category_raw"] = _col(df, "Category")
    out["sub_category_raw"] = _col(df, "Sub Category")

    # --- derived analysis category ---------------------------------------
    keys = out["sub_category_raw"].map(_norm_key)
    keys = keys.map(lambda k: _norm_key(alias_by_key[k]) if k in alias_by_key else k)

    out["sub_category"] = keys.map(defs.subcategory_label)
    out["category"] = keys.map(defs.subcategory_to_category)

    unmapped = out["category"].isna() & out["sub_category_raw"].notna()
    if unmapped.any():
        report.unmapped_subcategories = (
            out.loc[unmapped, "sub_category_raw"].value_counts().to_dict()
        )
    out["category"] = out["category"].fillna("Unmapped")
    out["sub_category"] = out["sub_category"].fillna(out["sub_category_raw"])
    out["recategorised"] = (
        out["category"].ne(out["category_raw"]) & out["category"].ne("Unmapped")
    )

    # --- zone / ward ------------------------------------------------------
    zw = _col(df, "Zone & Ward").map(split_zone_ward)
    out["zone"] = [t[0] for t in zw]
    out["ward_name"] = [t[1] for t in zw]
    out["ward_no"] = pd.array([t[2] for t in zw], dtype="Int64")

    # --- people -----------------------------------------------------------
    for src, name_col, mob_col in (
        ("Complainer Name & Mob. No.", "complainant_name", "complainant_mobile"),
        ("Concern Person Name & Mob. No.", "officer_name", "officer_mobile"),
    ):
        parts = _col(df, src).map(split_name_mobile)
        out[name_col] = [t[0] for t in parts]
        out[mob_col] = [t[1] for t in parts]

    # --- dates ------------------------------------------------------------
    opened = pd.to_datetime(_col(df, "Date & Time"), format=DATE_FORMAT, errors="coerce")
    closed = pd.to_datetime(_col(df, "Date of Closure"), format=DATE_FORMAT, errors="coerce")
    out["opened_at"] = opened
    out["closed_at"] = closed
    report.unparsed_opened = int(opened.isna().sum())
    report.unparsed_closed = int(closed.isna().sum())

    out["resolution_hours"] = (closed - opened).dt.total_seconds() / 3600
    out.loc[out["resolution_hours"] < 0, "resolution_hours"] = pd.NA
    report.closed_before_opened = int(((closed - opened).dt.total_seconds() < 0).sum())

    # --- status -----------------------------------------------------------
    status = _col(df, "Status").astype("string").str.strip()
    canonical = {_norm_key(s): s for s in defs.statuses}
    out["status"] = status.map(lambda s: canonical.get(_norm_key(s)))
    unknown = out["status"].isna() & status.notna()
    if unknown.any():
        report.unknown_statuses = status[unknown].value_counts().to_dict()
    out["status"] = out["status"].fillna(status)

    out["description"] = _col(df, "Description")
    report.rows_out = len(out)
    return out.reset_index(drop=True), report
