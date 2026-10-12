"""
Kanpur Nagar Nigam -- Grievance Dashboard (NCAP DSP)

Standalone Streamlit app. Portal exports arrive as XLSX/CSV uploads; category
definitions come from a local file, the published sheet, or a built-in
snapshot (in that order of preference).

Layout follows the other DSP city dashboards: sidebar navigation between
views, and one tab per analysis category inside each drill-down.

Run:  streamlit run app.py
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from kanpur_normalize import (
    DEFINITIONS_FILENAMES,
    SURVEYOR_FILENAMES,
    attribute_surveyor,
    load_surveyors_bytes,
    load_surveyors_file,
    zone_digits,
    SHEET_PUB,
    Definitions,
    distinct_subcategories,
    fallback_definitions,
    load_definitions,
    load_definitions_bytes,
    load_definitions_file,
    normalize_dump,
    read_upload,
    suggest_match,
)

CITY = "Kanpur"

# Bump on every deploy. Shown in the sidebar so a stale build on Streamlit
# Cloud is visible immediately instead of being mistaken for a code bug.
APP_VERSION = "2026-10-12a"

st.set_page_config(
    page_title=f"{CITY} DSP Dashboard",
    page_icon="🗂️",
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    APP_DIR = Path(__file__).resolve().parent
except NameError:  # exec'd or embedded rather than run as a script
    APP_DIR = Path.cwd()

# ==========================================
# CONFIGURATION
# ==========================================

STATUS_COLUMNS = ["PENDING", "REJECTED", "CLOSED"]
UNRESOLVED_STATUSES = ["PENDING"]   # selects rows for the pendency views;
                                    # no separate column, it duplicates PENDING
STATUS_RANGE = ["#F59E0B", "#9CA3AF", "#10B981"]   # pending / rejected / closed

CATEGORY_ORDER = ["Sanitation", "Engineering", "Malba", "Unmapped"]
CATEGORY_ICONS = {
    "Sanitation": "🧹",
    "Engineering": "🏗️",
    "Malba": "🚜",
    "Unmapped": "❓",
}

CATEGORY_COLOURS = {
    "Sanitation": "#3B71CA",
    "Engineering": "#E4A11B",
    "Malba": "#8B5E3C",
    "Unmapped": "#9CA3AF",
}

AGE_BUCKETS = ["< 1 Month", "1-6 Months", "6-12 Months", "> 1 Year"]

CATEGORY20 = [
    "#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c", "#98df8a",
    "#d62728", "#ff9896", "#9467bd", "#c5b0d5", "#8c564b", "#c49c94",
    "#e377c2", "#f7b6d2", "#7f7f7f", "#c7c7c5", "#bcbd22", "#dbdb8d",
    "#17becf", "#9edae5",
]

VIEWS = [
    "Main Category Summary",
    "Subcategory Drill-Down",
    "Zone-wise Drill-Down",
    "Ward-wise Drill-Down",
    "Officer Leaderboard",
    "Surveyor Performance",
    "Age-wise Pendency",
    "Monthly Trend Analysis",
    "Custom Date Range Analysis",
    "Ticket Inspector",
    "Data Quality",
]


def _full_width() -> dict:
    """Streamlit renamed the full-width flag in 1.49.

    Before 1.49, `width` is an integer pixel count and `use_container_width`
    is the flag. From 1.49 on, `width="stretch"` is the flag and
    `use_container_width` warns on every call. Pick per installed version so
    the app runs clean on both.
    """
    try:
        major, minor = (int(part) for part in st.__version__.split(".")[:2])
    except (ValueError, AttributeError):
        return {"use_container_width": True}
    if (major, minor) >= (1, 49):
        return {"width": "stretch"}
    return {"use_container_width": True}


FULL_WIDTH = _full_width()


# ==========================================
# DEFINITIONS LOADING
# ==========================================

@st.cache_data(ttl=300, show_spinner="Loading category definitions…")
def cached_definitions() -> tuple[Definitions, str, str | None, str]:
    """Load the category mapping, in order of preference.

    1. A definitions.csv / definitions.xlsx sitting next to app.py. Wins when
       present, because on a restricted network it is the only reliable source
       and an explicit local file should not be silently overridden.
    2. The published Google Sheet. Needs outbound docs.google.com access.
    3. The built-in snapshot, which goes stale the moment the sheet changes.

    Returns (definitions, source label, error or None, timestamp).
    """
    stamp = pd.Timestamp.now().strftime("%d %b %Y %H:%M:%S")

    for name in DEFINITIONS_FILENAMES:
        local = APP_DIR / name
        if local.exists():
            try:
                return load_definitions_file(local), f"local file `{name}`", None, stamp
            except Exception as exc:
                return (
                    fallback_definitions(),
                    "built-in snapshot",
                    f"Could not read {name} — {type(exc).__name__}: {exc}",
                    stamp,
                )

    try:
        return load_definitions(SHEET_PUB), "published Google Sheet", None, stamp
    except Exception as exc:
        return (
            fallback_definitions(),
            "built-in snapshot",
            f"Could not reach the published sheet — {type(exc).__name__}: {exc}",
            stamp,
        )


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    """Add the derived columns the views work from.

    Column names and status buckets deliberately match the other city
    dashboards so figures are comparable across them.
    """
    df = df.copy()

    bucket = {"Completed": "CLOSED", "Pending": "PENDING", "Rejected": "REJECTED"}
    df["StatusBucket"] = df["status"].map(bucket).fillna("PENDING")

    df["MainCategory"] = df["category"]
    df["Subcategory_Clean"] = df["sub_category"].fillna(df["sub_category_raw"])

    df["ClosureTimeDays"] = df["resolution_hours"] / 24
    df.loc[df["ClosureTimeDays"] < 0, "ClosureTimeDays"] = None

    now = pd.Timestamp(datetime.now())
    df["AgeDays"] = (now - df["opened_at"]).dt.days

    def age_bucket(row):
        if row["StatusBucket"] == "CLOSED":
            return "Closed"
        days = row["AgeDays"]
        if pd.isna(days):
            return "Unknown"
        if days < 30:
            return AGE_BUCKETS[0]
        if days <= 180:
            return AGE_BUCKETS[1]
        if days <= 365:
            return AGE_BUCKETS[2]
        return AGE_BUCKETS[3]

    df["AgeBucket"] = df.apply(age_bucket, axis=1)

    df["Zone"] = df["zone"].fillna("(not recorded)")
    df["Ward"] = [
        f"{no} - {name}" if pd.notna(no) and pd.notna(name)
        else (name if pd.notna(name) else "(not recorded)")
        for no, name in zip(df["ward_no"], df["ward_name"])
    ]
    df["Officer"] = (
        df["officer_name"].fillna("Unassigned").replace("", "Unassigned")
    )

    df["ZoneNo"] = df["zone"].map(zone_digits)
    df["DateOnly"] = df["opened_at"].dt.date
    df["ISOWeek"] = df["opened_at"].dt.strftime("%G-W%V")
    fy_start = df["opened_at"].dt.year.where(df["opened_at"].dt.month >= 4,
                                             df["opened_at"].dt.year - 1)
    fq = df["opened_at"].dt.month.map(
        {4: "Q1", 5: "Q1", 6: "Q1", 7: "Q2", 8: "Q2", 9: "Q2",
         10: "Q3", 11: "Q3", 12: "Q3", 1: "Q4", 2: "Q4", 3: "Q4"}
    )
    df["FYQuarter"] = (
        "FY " + fy_start.astype("Int64").astype(str) + "-"
        + (fy_start + 1).astype("Int64").astype(str).str[-2:] + " " + fq
    )
    df["MonthKey"] = df["opened_at"].dt.to_period("M").astype(str)
    df["ClosedMonthKey"] = df["closed_at"].dt.to_period("M").astype(str)
    return df


@st.cache_data(show_spinner="Parsing export…")
def cached_parse(data: bytes, filename: str, defs_key: tuple):
    """Cached on the uploaded bytes AND the active category mapping.

    defs_key must NOT be renamed with a leading underscore: Streamlit skips
    hashing any argument whose name starts with '_', which would cache the
    parsed frame on the file alone and keep serving the old category mapping
    after the definitions sheet changed.
    """
    defs, _, _, _ = cached_definitions()
    raw = read_upload(data, filename)
    df, report = normalize_dump(raw, defs)
    return enrich(df), report, distinct_subcategories(raw)


# ==========================================
# DISPLAY HELPERS
# ==========================================

def ordered_categories(df: pd.DataFrame) -> list[str]:
    present = df["MainCategory"].dropna().unique().tolist()
    return [c for c in CATEGORY_ORDER if c in present] + sorted(
        c for c in present if c not in CATEGORY_ORDER
    )


def category_tab_labels(categories: list[str]) -> list[str]:
    return [f"{CATEGORY_ICONS.get(c, '📁')} {c}" for c in categories]


def definitions_table(d: Definitions) -> pd.DataFrame:
    return (
        pd.DataFrame(
            [
                {"Category": d.subcategory_to_category[k],
                 "Sub category": d.subcategory_label[k]}
                for k in d.subcategory_to_category
            ]
        )
        .sort_values(["Category", "Sub category"])
        .reset_index(drop=True)
    )


def display_with_fixed_footer(df: pd.DataFrame, caption: str | None = None):
    """Table with its grand-total row pinned below, as in the other cities."""
    if df.empty:
        st.warning("⚠️ No data available to display.")
        return
    body, total = df.iloc[:-1], df.iloc[[-1]]

    config = {}
    if "% Closure" in df.columns:
        config["% Closure"] = st.column_config.NumberColumn(format="%.1f%%")
    if "Avg Closure Time (Days)" in df.columns:
        config["Avg Closure Time (Days)"] = st.column_config.NumberColumn(format="%.1f")

    st.dataframe(body, column_config=config, **FULL_WIDTH)
    st.markdown("⬇️ **Grand Total**")
    st.dataframe(total, column_config=config, **FULL_WIDTH)
    if caption:
        st.caption(caption)


def generate_pivot_summary(
    df: pd.DataFrame, group_col: str, label_suffix: str = "Total",
    show_avg_time: bool = False,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    summary = df.groupby([group_col, "StatusBucket"]).size().unstack(fill_value=0)
    for col in STATUS_COLUMNS:
        if col not in summary.columns:
            summary[col] = 0
    summary = summary[STATUS_COLUMNS]

    summary["Grand Total"] = summary[STATUS_COLUMNS].sum(axis=1)
    summary["% Closure"] = (
        summary["CLOSED"] / summary["Grand Total"] * 100
    ).fillna(0).round(1)

    if show_avg_time:
        summary["Avg Closure Time (Days)"] = (
            df.groupby(group_col)["ClosureTimeDays"].mean().round(1)
        )

    totals = {
        c: summary[c].sum()
        for c in STATUS_COLUMNS + ["Grand Total"]
    }
    totals["% Closure"] = round(
        totals["CLOSED"] / totals["Grand Total"] * 100 if totals["Grand Total"] else 0, 1
    )
    if show_avg_time:
        mean_days = df["ClosureTimeDays"].mean()
        totals["Avg Closure Time (Days)"] = (
            None if pd.isna(mean_days) else round(mean_days, 1)
        )

    order = STATUS_COLUMNS + ["Grand Total", "% Closure"]
    if show_avg_time:
        order.append("Avg Closure Time (Days)")

    total_row = pd.DataFrame([totals], index=[f"**{label_suffix}**"])
    return pd.concat([summary, total_row])[order]


def generate_aging_summary(df: pd.DataFrame, group_col: str) -> pd.DataFrame:
    if df.empty or "AgeBucket" not in df.columns:
        return pd.DataFrame()
    summary = df.groupby([group_col, "AgeBucket"]).size().unstack(fill_value=0)
    for c in AGE_BUCKETS:
        if c not in summary.columns:
            summary[c] = 0
    summary = summary[AGE_BUCKETS]
    summary["Total Unresolved"] = summary.sum(axis=1)
    return summary.sort_values("Total Unresolved", ascending=False)


def _no_data_chart(title: str, height: int):
    return (
        alt.Chart(pd.DataFrame({"x": [0]})).mark_text()
        .encode(text=alt.value("No Data"))
        .properties(title=title, height=height)
    )


LABEL_MIN_PCT = 4        # below this a slice is too thin to label legibly


def _doughnut(counts: pd.DataFrame, field: str, color, title: str, height: int):
    """Doughnut with the share printed on each slice.

    Arc and label share one base so their stacking order is identical --
    building them as separate charts lets Altair order them differently and
    the labels drift onto the wrong slices.
    """
    total = counts["Count"].sum()
    counts = counts.copy()
    counts["Pct"] = counts["Count"] / total * 100 if total else 0
    counts["Percent"] = counts["Pct"].round(1).astype(str) + "%"

    base = alt.Chart(counts).encode(
        theta=alt.Theta(field="Count", type="quantitative", stack=True),
        color=color,
        order=alt.Order("Count:Q", sort="descending"),
        tooltip=[field, "Count", "Percent"],
    )
    arc = base.mark_arc(innerRadius=45, outerRadius=78)
    # Labels sit just outside the ring and take the slice's own colour, which
    # reads on both the light and dark Streamlit themes.
    label = base.mark_text(radius=95, fontSize=11, fontWeight="bold").encode(
        text=alt.Text("Percent:N"),
    ).transform_filter(alt.datum.Pct >= LABEL_MIN_PCT)
    return (arc + label).properties(title=title, height=height)


def draw_doughnut(df: pd.DataFrame, title: str, show_legend: bool = True, height: int = 250):
    if df.empty:
        return _no_data_chart(title, height)
    counts = df["StatusBucket"].value_counts().reset_index()
    counts.columns = ["Status", "Count"]

    legend = alt.Legend(orient="right", title=None) if show_legend else None
    color = alt.Color(
        field="Status", type="nominal",
        scale=alt.Scale(domain=STATUS_COLUMNS, range=STATUS_RANGE),
        legend=legend,
    )
    return _doughnut(counts, "Status", color, title, height)


def draw_contribution_doughnut(
    df: pd.DataFrame, category_col: str, title: str,
    show_legend: bool = True, height: int = 250, color_scale=None,
):
    if df.empty:
        return _no_data_chart(title, height)
    counts = df[category_col].value_counts().reset_index()
    counts.columns = ["Category", "Count"]

    legend = alt.Legend(orient="right", title=None) if show_legend else None
    color = (
        alt.Color(field="Category", type="nominal", scale=color_scale, legend=legend)
        if color_scale
        else alt.Color(field="Category", type="nominal", legend=legend)
    )
    return _doughnut(counts, "Category", color, title, height)


def colour_legend(labels: list[str], colours: list[str]) -> str:
    html = ("<div style='display:flex;flex-wrap:wrap;justify-content:center;"
            "gap:15px;margin-bottom:10px;font-size:14px;'>")
    for label, colour in zip(labels, colours):
        html += (f"<div><span style='color:{colour};font-size:16px;'>●</span> "
                 f"{label}</div>")
    return html + "</div>"


def status_legend_row() -> str:
    return (
        "<div style='text-align:center;'>🟠 <b>PENDING</b> &nbsp;|&nbsp; "
        "⚪ <b>REJECTED</b> &nbsp;|&nbsp; 🟢 <b>CLOSED</b></div>"
    )


def date_range_filter(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Optional 'raised between' filter, matching the other cities' pattern."""
    valid = df["opened_at"].dropna()
    if valid.empty:
        return df

    st.markdown("##### 📅 View Settings")
    c1, c2 = st.columns([1, 2])
    with c1:
        use_dates = st.checkbox("Filter by Date Range", key=f"{key}_check")
    picked = None
    with c2:
        if use_dates:
            lo, hi = valid.min().date(), valid.max().date()
            picked = st.date_input(
                "Select Date Range", value=(lo, hi),
                min_value=lo, max_value=hi, key=f"{key}_dates",
            )
    st.markdown("---")

    if use_dates and isinstance(picked, tuple) and len(picked) == 2:
        start, end = picked
        st.caption(
            f"Showing tickets raised from **{start:%d %b %Y}** to **{end:%d %b %Y}**"
        )
        return df[df["opened_at"].dt.date.between(start, end)]
    return df


def download_csv(df: pd.DataFrame, label: str, filename: str):
    st.download_button(
        label, df.to_csv(index=False).encode("utf-8"),
        file_name=filename, mime="text/csv",
    )


# ==========================================
# SIDEBAR & DATA LOAD
# ==========================================

st.title(f"📊 {CITY} DSP Dashboard")
st.markdown("---")

defs, defs_source, defs_error, defs_stamp = cached_definitions()

st.sidebar.header("📂 Data Source")
upload = st.sidebar.file_uploader(
    "Upload Data (XLSX or CSV)",
    type=["xlsx", "xls", "csv"],
    help="Complaints List export from the portal. The banner row and blank "
         "first column are handled automatically.",
)

with st.sidebar.expander("⚙️ Category definitions"):
    if st.button("↻ Reload definitions"):
        # Clear everything: the parsed frame is derived from the definitions,
        # so refreshing one without the other shows a new mapping over old
        # numbers.
        st.cache_data.clear()
        st.rerun()
    defs_upload = st.file_uploader(
        "Definitions override", type=["csv", "xlsx"],
        help="Category tab exported as CSV. Use this when the machine cannot "
             "reach the published sheet.",
    )
    if defs_upload is not None:
        try:
            st.session_state["defs_override"] = load_definitions_bytes(
                defs_upload.getvalue(), defs_upload.name
            )
        except Exception as exc:
            st.error(f"Definitions file unreadable — {exc}")

    sv_upload = st.file_uploader(
        "Surveyor mapping override", type=["csv", "xlsx"],
        help="Surveyors tab exported as CSV: a Name column and a Zone column.",
    )
    if sv_upload is not None:
        try:
            st.session_state["surveyor_override"] = load_surveyors_bytes(
                sv_upload.getvalue(), sv_upload.name
            )
        except Exception as exc:
            st.error(f"Surveyor mapping unreadable — {exc}")

if "defs_override" in st.session_state:
    defs, defs_source, defs_error = (
        st.session_state["defs_override"], "uploaded definitions file", None,
    )

# A local surveyors file, then an upload, each override what the sheet gave.
for _sv_name in SURVEYOR_FILENAMES:
    _sv_path = APP_DIR / _sv_name
    if _sv_path.exists():
        try:
            defs.surveyor_assignments = load_surveyors_file(_sv_path)
        except Exception:
            pass
        break
if "surveyor_override" in st.session_state:
    defs.surveyor_assignments = st.session_state["surveyor_override"]

n_subs = len(defs.subcategory_to_category)
if defs_error:
    st.error(
        f"**Not reading your sheet — using the built-in snapshot "
        f"({n_subs} sub-categories).** The snapshot goes stale as soon as the "
        f"sheet changes, so anything added there will show as Unmapped. "
        f"Fix it by downloading the Category tab as CSV and saving it as "
        f"`definitions.csv` next to `app.py`, or upload it in the sidebar.\n\n"
        f"`{defs_error}`"
    )
else:
    st.caption(
        f"Definitions: **{n_subs} sub-categories** across "
        f"{len(defs.categories)} categories, from the {defs_source}, "
        f"loaded {defs_stamp}."
    )

st.sidebar.caption(f"Build {APP_VERSION}")
st.sidebar.markdown("---")
st.sidebar.header("🧭 Navigation")

if "current_view" not in st.session_state:
    st.session_state.current_view = VIEWS[0]

for _view in VIEWS:
    _kind = "primary" if st.session_state.current_view == _view else "secondary"
    if st.sidebar.button(_view, type=_kind, **FULL_WIDTH):
        st.session_state.current_view = _view
        st.rerun()

if upload is None:
    st.info("⬅️ Upload a portal export in the sidebar to begin.")
    st.subheader("Categories in use")
    st.dataframe(definitions_table(defs), hide_index=True, **FULL_WIDTH)
    st.stop()

# The surveyor mapping must be part of the cache key too, or editing it leaves
# the parsed frame -- and every attributed figure -- stale.
defs_key = (
    tuple(sorted(defs.subcategory_to_category.items())),
    tuple(defs.statuses),
    tuple(sorted(
        (a["zone"], a["name"], str(a["start"]), str(a["end"]))
        for a in defs.surveyor_assignments
    )),
)
try:
    df_all, report, distinct_subs = cached_parse(
        upload.getvalue(), upload.name, defs_key
    )
except Exception as exc:
    st.error(
        f"❌ Could not parse **{upload.name}**.\n\n"
        f"`{type(exc).__name__}: {exc}`\n\n"
        f"Running build **{APP_VERSION}**. If this says "
        f"`'NaTType' object has no attribute 'isna'`, the deployment is on an "
        f"older `kanpur_normalize.py` — that error cannot occur in this build, "
        f"which reports missing columns instead of failing."
    )
    st.stop()

if df_all.empty:
    st.error("❌ The file parsed successfully but contains no data rows.")
    st.stop()

if report.missing_columns:
    st.error(
        "**This export is missing columns the dashboard needs**, so parts of "
        "it will be blank. Either the portal changed its column names, or the "
        "header row was not found.\n\n"
        f"**Missing:** {', '.join(report.missing_columns)}\n\n"
        f"**Found instead:** {', '.join(report.found_columns) or '(none)'}"
    )

# Tickets whose sub-category is not in the definitions are excluded from every
# analysis view, so no figure is built on a category we could not determine.
# df_full keeps them for the Data Quality view, which is where they belong.
df_full = df_all
df_all = df_all[df_all["MainCategory"] != "Unmapped"]

if report.unmapped_subcategories:
    excluded = sum(report.unmapped_subcategories.values())
    st.warning(
        f"⚠️ **{excluded} of {len(df_full)} tickets excluded.** Their "
        f"sub-categories are missing from the definitions, so they cannot be "
        f"attributed to a category. Every figure below is out of "
        f"**{len(df_all)}** tickets, not {len(df_full)}. Add the missing "
        f"sub-categories in the *Data Quality* view to bring them in."
    )

if df_all.empty:
    st.error(
        "❌ Every ticket in this export has a sub-category missing from the "
        "definitions, so there is nothing left to analyse. See *Data Quality* "
        "for the list of unmapped values."
    )
    st.stop()

categories = ordered_categories(df_all)
view = st.session_state.current_view


# ==========================================
# MAIN CATEGORY SUMMARY
# ==========================================
if view == "Main Category Summary":
    st.subheader("📈 Main Category Summary")
    df = date_range_filter(df_all, "main")

    summary = generate_pivot_summary(df, "MainCategory", "TOTAL", show_avg_time=True)
    if summary.empty:
        st.warning("⚠️ No tickets in this range.")
    else:
        totals = summary.iloc[-1]

        st.markdown("##### 🎯 Individual Status Breakdown")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🟠 Pending", int(totals["PENDING"]))
        c2.metric("⚪ Rejected", int(totals["REJECTED"]))
        c3.metric("🟢 Closed", int(totals["CLOSED"]))
        c4.metric("📋 Grand Total", int(totals["Grand Total"]))

        c1, c2 = st.columns(2)
        c1.metric("✅ Closure Rate", f"{totals['% Closure']:.1f}%")
        avg_days = totals.get("Avg Closure Time (Days)")
        c2.metric(
            "⏱️ Avg Closure Time",
            "—" if avg_days is None or pd.isna(avg_days) else f"{avg_days:.1f} days",
        )

        st.markdown("---")
        st.markdown("##### 🥧 Ticket Share by Category")

        cat_scale = alt.Scale(
            domain=categories,
            range=[CATEGORY_COLOURS.get(c, "#9CA3AF") for c in categories],
        )
        st.markdown(
            colour_legend(
                categories,
                [CATEGORY_COLOURS.get(c, "#9CA3AF") for c in categories],
            ),
            unsafe_allow_html=True,
        )

        share1, share2 = st.columns(2)
        with share1:
            st.altair_chart(
                draw_contribution_doughnut(
                    df, "MainCategory", "Total Raised",
                    show_legend=False, height=280, color_scale=cat_scale,
                ),
                **FULL_WIDTH,
            )
        with share2:
            st.altair_chart(
                draw_contribution_doughnut(
                    df[df["StatusBucket"] == "CLOSED"], "MainCategory",
                    "Total Closed", show_legend=False, height=280,
                    color_scale=cat_scale,
                ),
                **FULL_WIDTH,
            )
        st.caption(
            "Left: share of all tickets raised. Right: share of those closed — "
            "a category larger on the right than the left is being cleared "
            "faster than the rest."
        )

        st.markdown("---")
        st.markdown("##### 🍩 Status Split by Category")
        st.markdown(status_legend_row(), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)

        cols = st.columns(max(len(categories), 1))
        for col, cat in zip(cols, categories):
            with col:
                st.altair_chart(
                    draw_doughnut(
                        df[df["MainCategory"] == cat],
                        f"{CATEGORY_ICONS.get(cat, '📁')} {cat}",
                        show_legend=False, height=260,
                    ),
                    **FULL_WIDTH,
                )

        st.markdown("<br>", unsafe_allow_html=True)
        display_with_fixed_footer(summary)

        st.markdown("##### 📊 Citywide & Zone-wise Snapshot")
        display_with_fixed_footer(
            generate_pivot_summary(df, "Zone", "ALL ZONES TOTAL", show_avg_time=True)
        )


# ==========================================
# SUBCATEGORY DRILL-DOWN
# ==========================================
elif view == "Subcategory Drill-Down":
    st.subheader("🔍 Subcategory Drill-Down")
    df = date_range_filter(df_all, "sub")

    if not categories:
        st.warning("⚠️ No valid categories found.")
    else:
        for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
            with tab:
                sub_df = df[df["MainCategory"] == cat]
                if sub_df.empty:
                    st.info(f"No tickets found for {cat} in this date range.")
                    continue

                subs = sorted(sub_df["Subcategory_Clean"].dropna().unique().tolist())
                colours = [CATEGORY20[i % len(CATEGORY20)] for i in range(len(subs))]
                scale = alt.Scale(domain=subs, range=colours)

                st.markdown(f"###### 📊 Subcategory Contribution ({cat})")
                st.markdown(colour_legend(subs, colours), unsafe_allow_html=True)

                c1, c2 = st.columns(2)
                with c1:
                    st.altair_chart(
                        draw_contribution_doughnut(
                            sub_df, "Subcategory_Clean", "Total Raised",
                            show_legend=False, color_scale=scale,
                        ),
                        **FULL_WIDTH,
                    )
                with c2:
                    st.altair_chart(
                        draw_contribution_doughnut(
                            sub_df[sub_df["StatusBucket"] == "CLOSED"],
                            "Subcategory_Clean", "Total Closed",
                            show_legend=False, color_scale=scale,
                        ),
                        **FULL_WIDTH,
                    )

                st.markdown("<br>", unsafe_allow_html=True)
                display_with_fixed_footer(
                    generate_pivot_summary(
                        sub_df, "Subcategory_Clean", f"{cat} Total", show_avg_time=True
                    )
                )


# ==========================================
# ZONE-WISE DRILL-DOWN
# ==========================================
elif view == "Zone-wise Drill-Down":
    st.subheader("🗺️ Zone-wise Drill-Down")
    df = date_range_filter(df_all, "zone")

    for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
        with tab:
            cat_df = df[df["MainCategory"] == cat]
            if cat_df.empty:
                st.info(f"No {cat} tickets in this date range.")
                continue

            st.markdown(f"###### 🍩 Zone-wise Status Breakdown ({cat})")
            st.markdown(status_legend_row(), unsafe_allow_html=True)
            st.markdown("<br>", unsafe_allow_html=True)

            zones = sorted(cat_df["Zone"].dropna().unique().tolist())
            for i in range(0, len(zones), 4):
                row = st.columns(4)
                for j in range(4):
                    if i + j < len(zones):
                        zone = zones[i + j]
                        with row[j]:
                            st.altair_chart(
                                draw_doughnut(
                                    cat_df[cat_df["Zone"] == zone], zone,
                                    show_legend=False, height=260,
                                ),
                                **FULL_WIDTH,
                            )

            st.markdown("<br>", unsafe_allow_html=True)
            display_with_fixed_footer(
                generate_pivot_summary(
                    cat_df, "Zone", f"{cat} — ALL ZONES", show_avg_time=True
                )
            )

            st.markdown("###### 📋 Subcategory Detail by Zone")
            zone_pick = st.selectbox("Select Zone", zones, key=f"zone_pick_{cat}")
            zone_df = cat_df[cat_df["Zone"] == zone_pick]
            if zone_df.empty:
                st.warning("No data for this zone in the selected range.")
            else:
                display_with_fixed_footer(
                    generate_pivot_summary(
                        zone_df, "Subcategory_Clean",
                        f"{cat} — {zone_pick} Total", show_avg_time=True,
                    )
                )


# ==========================================
# WARD-WISE DRILL-DOWN
# ==========================================
elif view == "Ward-wise Drill-Down":
    st.subheader("🏘️ Ward-wise Drill-Down")
    df = date_range_filter(df_all, "ward")

    for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
        with tab:
            cat_df = df[df["MainCategory"] == cat]
            if cat_df.empty:
                st.info(f"No {cat} tickets in this date range.")
                continue

            zone_options = ["All zones"] + sorted(
                cat_df["Zone"].dropna().unique().tolist()
            )
            zone_pick = st.selectbox(
                "Filter by Zone", zone_options, key=f"ward_zone_{cat}"
            )
            ward_df = (
                cat_df if zone_pick == "All zones"
                else cat_df[cat_df["Zone"] == zone_pick]
            )

            ward_summary = generate_pivot_summary(
                ward_df, "Ward", f"{cat} — {zone_pick}", show_avg_time=True
            )
            display_with_fixed_footer(ward_summary)

            body = ward_summary.iloc[:-1]
            if len(body) > 1:
                st.markdown("###### 📉 Closure Rate by Ward")
                chart_df = body.reset_index()
                chart_df = chart_df.rename(columns={chart_df.columns[0]: "Ward"})
                st.altair_chart(
                    alt.Chart(chart_df).mark_bar().encode(
                        x=alt.X("% Closure:Q", title="% Closure"),
                        y=alt.Y("Ward:N", sort="-x", title=None),
                        color=alt.Color(
                            "% Closure:Q",
                            scale=alt.Scale(scheme="redyellowgreen"),
                            legend=None,
                        ),
                        tooltip=["Ward", "Grand Total", "CLOSED", "% Closure"],
                    ).properties(height=max(220, 22 * len(chart_df))),
                    **FULL_WIDTH,
                )


# ==========================================
# OFFICER LEADERBOARD
# ==========================================
elif view == "Officer Leaderboard":
    st.subheader("🏆 Officer Leaderboard & Pendency Tracking")
    st.caption(
        "Officers come from the export's *Concern Person* column — Kanpur "
        "needs no separate roster sheet."
    )

    unresolved = df_all[df_all["StatusBucket"].isin(UNRESOLVED_STATUSES)]
    unassigned = unresolved[unresolved["Officer"] == "Unassigned"]
    if not unassigned.empty:
        st.error(
            f"⚠️ **{len(unassigned)} unresolved tickets** have no officer "
            f"recorded and are excluded from the leaderboard."
        )
        download_csv(
            unassigned, "⬇️ Download Unassigned Tickets (CSV)",
            f"kanpur_unassigned_{datetime.now():%Y%m%d}.csv",
        )

    st.markdown("### 🥇 Top & Bottom 5 by Pendency")
    st.caption("Ranked by number of currently unresolved tickets.")

    for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
        with tab:
            cat_df = unresolved[
                (unresolved["MainCategory"] == cat)
                & (unresolved["Officer"] != "Unassigned")
            ]
            if cat_df.empty:
                st.info(f"No unresolved {cat} tickets with a named officer.")
            else:
                counts = (
                    cat_df.groupby("Officer").size()
                    .reset_index(name="Total Unresolved Tickets")
                    .sort_values("Total Unresolved Tickets")
                    .reset_index(drop=True)
                )
                c1, c2 = st.columns(2)
                with c1:
                    st.success("🌟 Top 5 (Least Pendency)")
                    top = counts.head(5).copy()
                    top.index = top.index + 1
                    st.dataframe(top, **FULL_WIDTH)
                with c2:
                    st.error("⚠️ Bottom 5 (Highest Pendency)")
                    bottom = (
                        counts.tail(5)
                        .sort_values("Total Unresolved Tickets", ascending=False)
                        .reset_index(drop=True)
                    )
                    bottom.index = bottom.index + 1
                    st.dataframe(bottom, **FULL_WIDTH)

            st.markdown("###### 📋 Full Officer Table")
            named = df_all[
                (df_all["MainCategory"] == cat) & (df_all["Officer"] != "Unassigned")
            ]
            display_with_fixed_footer(
                generate_pivot_summary(
                    named, "Officer", f"{cat} Total", show_avg_time=True
                )
            )


# ==========================================
# SURVEYOR PERFORMANCE
# ==========================================
elif view == "Surveyor Performance":
    st.subheader("🧭 Surveyor Performance")

    assignments = defs.surveyor_assignments
    if not assignments:
        st.error(
            "No zone-to-surveyor mapping is loaded. Publish the **Surveyors** "
            "tab as CSV, or drop a `surveyors.csv` (Name, Zone) beside "
            "`app.py`, or upload one under *Category definitions* in the sidebar."
        )
        st.stop()

    st.caption(
        "Kanpur's surveyors share one portal login, so tickets are attributed "
        "by **zone**, not by who raised them. Treat this as ticket output per "
        "zone-owner, not as an attendance record — a day spent surveying with "
        "nothing found looks the same as a day off, and cover for a colleague "
        "lands on the wrong name."
    )

    with st.expander("Zone → surveyor assignments in use"):
        st.dataframe(
            pd.DataFrame([{
                "Zone": a["zone"],
                "Surveyor": a["name"],
                "From": "—" if a["start"] is None else f"{a['start']:%d %b %Y}",
                "To": "current" if a["end"] is None else f"{a['end']:%d %b %Y}",
            } for a in sorted(assignments, key=lambda a: (int(a["zone"]),
                              a["start"] or pd.Timestamp.min))],),
            hide_index=True, **FULL_WIDTH,
        )
        st.caption(
            "Tickets are credited to whoever held the zone **on the day the "
            "ticket was raised**, so a handover leaves earlier months "
            "untouched. To reassign a zone, add a row with an *Effective "
            "from* date rather than editing the existing row."
        )

    # --- who raised the ticket -------------------------------------------
    complainants = (
        df_all["complainant_name"].fillna("(blank)").value_counts()
    )
    dsp_like = [c for c in complainants.index if "dsp" in str(c).lower()]
    st.markdown("##### 👤 Whose tickets to count")
    picked_complainants = st.multiselect(
        "Complainant (the shared login raises the team's tickets; citizens "
        "raise their own)",
        complainants.index.tolist(),
        default=dsp_like or complainants.index.tolist(),
        format_func=lambda c: f"{c} ({complainants[c]})",
    )
    if not dsp_like:
        st.warning(
            "⚠️ No complainant name contains 'DSP', so every ticket is "
            "selected by default — including citizen complaints, which would "
            "inflate the counts. Pick the survey login explicitly."
        )
    if not picked_complainants:
        st.info("Select at least one complainant.")
        st.stop()

    sv = df_all[df_all["complainant_name"].fillna("(blank)").isin(picked_complainants)].copy()
    sv["Surveyor"] = attribute_surveyor(sv["ZoneNo"], sv["opened_at"], assignments)

    unattributed = sv[sv["Surveyor"].isna()]
    sv = sv.dropna(subset=["Surveyor"])

    if not unattributed.empty:
        zones_missing = sorted(
            unattributed["zone"].fillna("(not recorded)").unique().tolist()
        )
        st.warning(
            f"⚠️ {len(unattributed)} tickets have no surveyor for their zone "
            f"on the date they were raised, and are excluded. Zones involved: "
            f"{', '.join(zones_missing)}. This can mean the zone is missing "
            f"from the sheet, or that an *Effective from* date starts after "
            f"the ticket."
        )

    if sv.empty:
        st.warning("⚠️ No tickets could be attributed to a surveyor.")
        st.stop()

    def period_summary(frame: pd.DataFrame, key: str) -> pd.DataFrame:
        out = (
            frame.dropna(subset=[key])
            .groupby(["Surveyor", key])
            .agg(**{
                "Days active": ("DateOnly", "nunique"),
                "Tickets": ("comp_no", "size"),
                "Wards visited": ("Ward", "nunique"),
                "Zones visited": ("zone", "nunique"),
            })
            .reset_index()
            .rename(columns={key: key})
        )
        out["Tickets/day"] = (out["Tickets"] / out["Days active"]).round(1)
        return out.sort_values([key, "Tickets"], ascending=[False, False])

    tabs = st.tabs(["📅 Daily", "🗓️ Weekly", "📆 Monthly", "📊 FY Quarter"])

    # ---- Daily ----------------------------------------------------------
    with tabs[0]:
        daily = period_summary(sv, "DateOnly").rename(columns={"DateOnly": "Date"})
        if daily.empty:
            st.info("No dated tickets to summarise.")
        else:
            st.dataframe(daily, hide_index=True, **FULL_WIDTH)
            download_csv(daily, "⬇️ Download daily summary (CSV)", "kanpur_surveyor_daily.csv")

            st.markdown("###### 📍 Zone & ward detail by day")
            detail = (
                sv.dropna(subset=["DateOnly"])
                .groupby(["DateOnly", "Surveyor", "zone", "Ward"])
                .agg(Tickets=("comp_no", "size"))
                .reset_index()
                .rename(columns={"DateOnly": "Date", "zone": "Zone"})
                .sort_values(["Date", "Surveyor", "Tickets"], ascending=[False, True, False])
            )
            pick_day = st.selectbox(
                "Show detail for", ["All days"] + [
                    str(d) for d in sorted(detail["Date"].unique(), reverse=True)
                ],
            )
            shown = detail if pick_day == "All days" else detail[
                detail["Date"].astype(str) == pick_day
            ]
            st.dataframe(shown, hide_index=True, **FULL_WIDTH)
            download_csv(shown, "⬇️ Download ward detail (CSV)", "kanpur_surveyor_daily_detail.csv")

    # ---- Weekly / Monthly / Quarterly -----------------------------------
    for tab, key, label in (
        (tabs[1], "ISOWeek", "Week"),
        (tabs[2], "MonthKey", "Month"),
        (tabs[3], "FYQuarter", "Quarter"),
    ):
        with tab:
            table = period_summary(sv, key).rename(columns={key: label})
            if table.empty:
                st.info(f"No dated tickets to summarise by {label.lower()}.")
                continue
            st.dataframe(table, hide_index=True, **FULL_WIDTH)

            st.altair_chart(
                alt.Chart(table).mark_bar().encode(
                    x=alt.X(f"{label}:N", title=None),
                    y=alt.Y("Tickets:Q"),
                    color=alt.Color("Surveyor:N", legend=alt.Legend(orient="top", title=None)),
                    xOffset="Surveyor:N",
                    tooltip=[label, "Surveyor", "Days active", "Tickets",
                             "Wards visited", "Zones visited"],
                ).properties(height=320),
                **FULL_WIDTH,
            )
            download_csv(
                table, f"⬇️ Download {label.lower()} summary (CSV)",
                f"kanpur_surveyor_{label.lower()}.csv",
            )



# ==========================================
# AGE-WISE PENDENCY
# ==========================================
elif view == "Age-wise Pendency":
    st.subheader("⏳ Age-wise Pendency Analysis")
    st.caption(
        f"Ages measured against today ({datetime.now():%d %b %Y}). Only "
        f"unresolved tickets are counted."
    )

    unresolved = df_all[df_all["StatusBucket"].isin(UNRESOLVED_STATUSES)]
    if unresolved.empty:
        st.success("✅ No unresolved tickets in this export.")
    else:
        for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
            with tab:
                cat_df = unresolved[unresolved["MainCategory"] == cat]
                if cat_df.empty:
                    st.info(f"No unresolved {cat} tickets.")
                    continue

                counts = cat_df["AgeBucket"].value_counts()
                for col, bucket in zip(st.columns(len(AGE_BUCKETS)), AGE_BUCKETS):
                    col.metric(bucket, int(counts.get(bucket, 0)))

                st.markdown("###### 📊 Pendency Age by Zone")
                st.dataframe(generate_aging_summary(cat_df, "Zone"), **FULL_WIDTH)

                st.markdown("###### 📋 Pendency Age by Subcategory")
                st.dataframe(
                    generate_aging_summary(cat_df, "Subcategory_Clean"), **FULL_WIDTH
                )

                st.markdown("###### 🕰️ 10 Oldest Open Tickets")
                st.dataframe(
                    cat_df.nsmallest(10, "opened_at")[
                        ["comp_no", "Subcategory_Clean", "Zone", "Ward",
                         "Officer", "opened_at", "AgeDays"]
                    ],
                    hide_index=True, **FULL_WIDTH,
                )


# ==========================================
# MONTHLY TREND ANALYSIS
# ==========================================
elif view == "Monthly Trend Analysis":
    st.subheader("📅 Monthly Trend Analysis")

    raised = (
        df_all.dropna(subset=["opened_at"]).groupby("MonthKey").size().rename("Raised")
    )
    closed = (
        df_all[(df_all["StatusBucket"] == "CLOSED") & df_all["closed_at"].notna()]
        .groupby("ClosedMonthKey").size().rename("Closed")
    )
    trend = pd.concat([raised, closed], axis=1).fillna(0).sort_index()

    if trend.empty:
        st.warning("⚠️ No dated tickets to trend.")
    else:
        trend_long = (
            trend.reset_index()
            .rename(columns={trend.index.name or "index": "Month"})
            .melt("Month", var_name="Measure", value_name="Tickets")
        )
        st.altair_chart(
            alt.Chart(trend_long).mark_line(point=True).encode(
                x=alt.X("Month:N", title=None),
                y=alt.Y("Tickets:Q"),
                color=alt.Color(
                    "Measure:N",
                    scale=alt.Scale(domain=["Raised", "Closed"],
                                    range=["#3B71CA", "#10B981"]),
                    legend=alt.Legend(orient="top", title=None),
                ),
                tooltip=["Month", "Measure", "Tickets"],
            ).properties(height=320),
            **FULL_WIDTH,
        )
        st.caption(
            "*Raised* counts by the month the ticket was registered; *Closed* "
            "by the month it was closed, so the two need not balance."
        )
        st.dataframe(trend.astype(int), **FULL_WIDTH)

        st.markdown("##### 📆 Month-wise Detail by Category")
        for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
            with tab:
                cat_df = df_all[
                    (df_all["MainCategory"] == cat) & df_all["opened_at"].notna()
                ]
                if cat_df.empty:
                    st.info(f"No dated {cat} tickets.")
                else:
                    display_with_fixed_footer(
                        generate_pivot_summary(
                            cat_df, "MonthKey", f"{cat} Total", show_avg_time=True
                        )
                    )


# ==========================================
# CUSTOM DATE RANGE ANALYSIS
# ==========================================
elif view == "Custom Date Range Analysis":
    st.subheader("📆 Custom Date Range Analysis")

    valid = df_all["opened_at"].dropna()
    if valid.empty:
        st.warning("⚠️ No dated tickets in this export.")
    else:
        lo, hi = valid.min().date(), valid.max().date()
        picked = st.date_input(
            "Select Date Range", value=(lo, hi), min_value=lo, max_value=hi,
            key="custom_dates",
        )
        if not (isinstance(picked, tuple) and len(picked) == 2):
            st.info("Pick a start and end date.")
        else:
            start, end = picked
            raised_df = df_all[df_all["opened_at"].dt.date.between(start, end)]
            closed_df = df_all[
                df_all["closed_at"].notna()
                & df_all["closed_at"].dt.date.between(start, end)
            ]
            same_window = raised_df[
                (raised_df["StatusBucket"] == "CLOSED")
                & raised_df["closed_at"].notna()
                & raised_df["closed_at"].dt.date.between(start, end)
            ]

            c1, c2, c3 = st.columns(3)
            c1.metric("📥 Raised in range", len(raised_df))
            c2.metric("📤 Closed in range", len(closed_df))
            c3.metric("⚡ Raised & closed in range", len(same_window))
            st.caption(
                "*Closed in range* counts closures that happened in the window "
                "regardless of when the ticket was raised, so it can exceed "
                "*Raised in range*."
            )

            st.markdown("---")
            for tab, cat in zip(st.tabs(category_tab_labels(categories)), categories):
                with tab:
                    cat_df = raised_df[raised_df["MainCategory"] == cat]
                    if cat_df.empty:
                        st.info(f"No {cat} tickets raised in this range.")
                    else:
                        display_with_fixed_footer(
                            generate_pivot_summary(
                                cat_df, "Subcategory_Clean",
                                f"{cat} Total", show_avg_time=True,
                            )
                        )
            download_csv(
                raised_df, "⬇️ Download tickets raised in range (CSV)",
                f"kanpur_{start:%Y%m%d}_{end:%Y%m%d}.csv",
            )


# ==========================================
# TICKET INSPECTOR
# ==========================================
elif view == "Ticket Inspector":
    st.subheader("🔎 Ticket Inspector (Deep Dive)")

    c1, c2, c3 = st.columns(3)
    with c1:
        f_cat = st.selectbox("Category", ["All"] + categories, key="insp_cat")
    with c2:
        pool = df_all if f_cat == "All" else df_all[df_all["MainCategory"] == f_cat]
        f_sub = st.selectbox(
            "Subcategory",
            ["All"] + sorted(pool["Subcategory_Clean"].dropna().unique().tolist()),
            key="insp_sub",
        )
    with c3:
        f_status = st.selectbox("Status", ["All"] + STATUS_COLUMNS, key="insp_status")

    c4, c5 = st.columns(2)
    with c4:
        f_zone = st.selectbox(
            "Zone", ["All"] + sorted(df_all["Zone"].dropna().unique().tolist()),
            key="insp_zone",
        )
    with c5:
        search = st.text_input(
            "Search ticket no. / description / officer", key="insp_search"
        )

    view_df = df_all.copy()
    if f_cat != "All":
        view_df = view_df[view_df["MainCategory"] == f_cat]
    if f_sub != "All":
        view_df = view_df[view_df["Subcategory_Clean"] == f_sub]
    if f_status != "All":
        view_df = view_df[view_df["StatusBucket"] == f_status]
    if f_zone != "All":
        view_df = view_df[view_df["Zone"] == f_zone]
    if search:
        needle = search.strip().lower()
        haystack = (
            view_df["comp_no"].astype(str).str.lower() + " "
            + view_df["description"].astype(str).str.lower() + " "
            + view_df["Officer"].astype(str).str.lower()
        )
        view_df = view_df[haystack.str.contains(needle, na=False, regex=False)]

    st.markdown(f"**{len(view_df):,} tickets** match these filters.")
    st.dataframe(
        view_df[[
            "comp_no", "MainCategory", "Subcategory_Clean", "StatusBucket",
            "Zone", "Ward", "Officer", "opened_at", "closed_at",
            "ClosureTimeDays", "AgeBucket", "description",
        ]],
        hide_index=True, **FULL_WIDTH,
    )
    download_csv(view_df, "⬇️ Download filtered tickets (CSV)", "kanpur_filtered.csv")


# ==========================================
# DATA QUALITY
# ==========================================
elif view == "Data Quality":
    st.subheader("🧪 Data Quality")

    if report.is_clean:
        st.success("✅ No data quality issues in this export.")
    st.code(report.render(), language=None)

    if report.unmapped_subcategories:
        st.markdown("##### ❓ Unmapped Sub-categories")
        st.markdown(
            "These tickets are **excluded from every other view** — they "
            "cannot be attributed to a category, so counting them would "
            "distort the totals. They remain in the cleaned-data download "
            "below. Compare the export values against what the app "
            "actually loaded — if a value looks present in your sheet but "
            "missing here, the app is reading an older copy."
        )
        unmapped_tbl = pd.DataFrame(
            sorted(report.unmapped_subcategories.items(), key=lambda kv: -kv[1]),
            columns=["Sub Category", "Tickets"],
        )
        unmapped_tbl["Closest sheet entry"] = unmapped_tbl["Sub Category"].map(
            lambda v: suggest_match(v, defs) or "—"
        )
        a, b = st.columns([3, 2])
        with a:
            st.markdown("**Unmapped, from the export**")
            st.dataframe(unmapped_tbl, hide_index=True, **FULL_WIDTH)
            st.caption(
                "*Closest sheet entry* is a similarity hint only — nothing is "
                "auto-matched. A wrong guess would move tickets between "
                "categories without telling you."
            )
        with b:
            st.markdown("**Loaded from the definitions source**")
            st.dataframe(definitions_table(defs), hide_index=True, **FULL_WIDTH)

    st.markdown("##### 📚 Build the mapping from the portal's own vocabulary")
    st.caption(
        "Every Sub Category value in this export, with counts. Download and "
        "paste into the definitions sheet, then fill in the Category column."
    )
    st.dataframe(distinct_subs, hide_index=True, **FULL_WIDTH)
    download_csv(
        distinct_subs, "⬇️ Download distinct sub-categories (CSV)",
        "kanpur_subcategories_in_export.csv",
    )

    recat = df_full[df_full["recategorised"]]
    if not recat.empty:
        st.markdown(f"##### 🔄 Reclassified Tickets ({len(recat)})")
        st.caption(
            "These arrived under a different Category in the export and were "
            "reassigned using the Sub Category mapping — the portal reports "
            "Malba tickets as Engineering."
        )
        st.dataframe(
            recat[["comp_no", "category_raw", "MainCategory",
                   "Subcategory_Clean", "Zone", "Ward", "StatusBucket"]],
            hide_index=True, **FULL_WIDTH,
        )

    st.markdown("##### 💾 Cleaned Data")
    download_csv(
        df_full, "⬇️ Download cleaned data (CSV)", "kanpur_grievances_cleaned.csv"
    )
