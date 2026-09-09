# Kanpur Grievance Dashboard (NCAP DSP)

Standalone Streamlit dashboard for Kanpur Nagar Nigam air-quality grievance
data. Portal exports are uploaded through the app; category definitions are
read live from the published definitions sheet.

## Run

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then upload a **Complaints List** export (`.xlsx` or `.csv`) in the sidebar.

## Files

| File | Purpose |
|---|---|
| `app.py` | Streamlit UI: navigation, views, charts |
| `kanpur_normalize.py` | Export parsing, category derivation, validation |
| `definitions.csv` | Optional local copy of the Category tab (see below) |
| `requirements.txt` | Pinned minimums |

## Views

Sidebar navigation, matching the other DSP city dashboards. Each drill-down
has one tab per analysis category (Sanitation / Engineering / Malba, plus
Unmapped when present).

| View | Contents |
|---|---|
| Main Category Summary | Status metrics, per-category doughnuts, category and zone pivots |
| Subcategory Drill-Down | Raised vs closed contribution doughnuts, subcategory pivot |
| Zone-wise Drill-Down | Zone doughnuts (4 per row), zone pivot, subcategory detail per zone |
| Ward-wise Drill-Down | Ward pivot with a zone filter, closure rate by ward |
| Officer Leaderboard | Top/bottom 5 by pendency, full officer pivot |
| Age-wise Pendency | Age buckets, pendency by zone and subcategory, 10 oldest open |
| Monthly Trend Analysis | Raised vs closed by month, month pivot per category |
| Custom Date Range Analysis | Raised / closed / same-window counts for a chosen range |
| Ticket Inspector | Filterable ticket table with search and CSV export |
| Data Quality | Validation report, unmapped diagnostics, reclassified tickets |

Status buckets (`PENDING` / `REJECTED` / `CLOSED`), the `% Closure` and
`Avg Closure Time (Days)` columns, and the pinned grand-total footer follow
the other cities so figures are directly comparable. `Completed` in the
export maps to `CLOSED`.

Officers come from the export's *Concern Person* column, so unlike Prayagraj
no roster sheet is needed. Surveyor-performance and quarterly-FY views are
not included — the Kanpur export carries no surveyor column.

## How categories work

The export's own `Category` column is **not** used for analysis. Kanpur has
three analysis categories — Sanitation, Engineering and **Malba** — but the
portal only emits two, reporting Malba tickets as Engineering. The analysis
category is therefore derived from `Sub Category` via the definitions sheet:

- `Category` — derived, used everywhere in the dashboard
- `category_raw` — as the portal sent it, kept for audit
- `recategorised` — True where the two differ (see the reclassified-tickets panel)

Sub-category matching ignores case, whitespace and punctuation, so
`Removal of Debris` in the export matches `Removal of debris` in the sheet.
Anything not in the sheet lands in `Unmapped` and is reported in the
data-quality panel rather than silently absorbed.

To add a sub-category, add a row to the definitions sheet — no code change.
Definitions are cached for five minutes; **↻ Reload definitions** in the
sidebar clears it immediately.

The portal uses more than one label for the same sub-category (`Dustbin not
cleaned` / `Dustbins not cleaned`). Two ways to handle that:

- add each variant as its own row (they then appear separately in charts), or
- add a third column named `Portal label` holding the exact portal string,
  keeping column B as the display name — several rows can share a display
  name, or one cell can hold variants separated by `|`

## Where definitions come from

Checked in this order, and the source is always named on screen:

1. `definitions.csv` or `definitions.xlsx` next to `app.py`
2. the published Google Sheet (needs outbound `docs.google.com` access)
3. the built-in snapshot in `kanpur_normalize.py`

**On a restricted office network** the sheet fetch fails and the app falls
back to the snapshot, which goes stale as soon as the sheet changes — every
new mapping then shows as Unmapped. If that happens, download the Category
tab as CSV, save it beside `app.py` as `definitions.csv`, and the app will
use it. There is also a *Definitions override* uploader in the sidebar for a
one-session fix.

## Export quirks handled

- Row 1 is a title banner; the real header is row 2
- A phantom first column whose header is a non-breaking space
- `Zone 4 & Mcrobertganj-15` → zone / ward name / ward number
- `Er. Minakshi Agrawal & 9411062529` → name / mobile, splitting on the last
  `&` so names containing `&` survive
- `17 Aug 2026 10:35 AM` datetime format
- Blank `Date of Closure` on Pending and Rejected tickets

## Known limits

- Each upload is a standalone snapshot; there is no cross-upload history, so
  month-on-month trends need a cumulative export
- `Median age of pending` is measured from the latest ticket in the uploaded
  file, not from today
- If the definitions sheet is unreachable, a built-in snapshot is used and a
  warning is shown; update `FALLBACK_CATEGORY_ROWS` if the sheet changes
