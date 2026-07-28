"""
Fanatics Collectibles - HR Metrics (V1)
Streamlit-in-Snowflake app.

Surfaces the four validated V1 HR metrics on the new Snowflake HR database:
  1. Active FTE Headcount (Total + by CLT Leader)
  2. Active Contractors (Total)
  3. Terminations YTD (Total + Voluntary/Involuntary split + by month)
  4. Monthly HC Trend (Regular FTE snapshot on the 1st of each month, live restatement)

Data is read through a RESTRICTED CALLER'S RIGHTS connection so queries run as the
viewer's (unmasked) default role, not the app owner's masked role. Caller's rights
only works in the SiS container runtime and errors in local dev; a fallback owner
connection is used locally for UI/import validation only (numbers will be masked).

See PLAN.md for metric definitions, prerequisites (caller grants), and tie-out targets.
"""

from datetime import date
import pandas as pd
import altair as alt
import streamlit as st

st.set_page_config(
    page_title="Fanatics Collectibles - HR Metrics (V1)",
    page_icon=":material/groups:",
    layout="wide",
)

CHART_HEIGHT = 320
YTD_START = "2026-01-01"  # Terminations YTD / Monthly trend anchor per requirements

# Termination categories carry two distinct "unknown" states out of the snapshot query:
#   'UNCLASSIFIED' - the reason matched FAN_HR_ADP_TERM_REASONS_LKP but no pattern in
#                    fan_term_reasons, i.e. a new/unmapped ADP reason code. Fix by
#                    adding a WHEN clause to that CTE.
#   NULL           - neither lookup matched at all, i.e. the join found nothing.
# They are kept separate everywhere because they need different remediation.
CAT_VOL = "Voluntary"
CAT_INVOL = "Involuntary"
CAT_UNMAPPED = "Unclassified: unmapped reason"
CAT_NO_MATCH = "Unclassified: no lookup match"
CAT_ORDER = [CAT_VOL, CAT_INVOL, CAT_UNMAPPED, CAT_NO_MATCH]

# Real categories take categorical slots 1-2; both unknown buckets are neutral greys so
# they read as "no data" rather than as a peer category. Grey steps differ in lightness
# only, so the axis label / legend / tooltip text always carries the distinction.
CAT_COLORS = ["#2a78d6", "#eb6834", "#52514e", "#898781"]


def category_labels(s: pd.Series) -> pd.Series:
    """Map raw termination_category values to display labels, preserving the
    UNCLASSIFIED-vs-NULL distinction that a plain fillna() would collapse."""
    return s.map(
        {
            "VOLUNTARY": CAT_VOL,
            "INVOLUNTARY": CAT_INVOL,
            "UNCLASSIFIED": CAT_UNMAPPED,
        }
    ).fillna(CAT_NO_MATCH)

# =============================================================================
# Connection (restricted caller's rights)
# =============================================================================
# MUST be created at the top of the script (auth token is valid ~2 min) and not
# behind if/else branches. Caller's rights errors in local dev / warehouse runtime,
# so we fall back to an owner connection locally (masked data, UI validation only).


conn = st.connection("snowflake-callers-rights")
CONN = conn
IS_CALLERS = True


# =============================================================================
# SQL
# =============================================================================

# Query A - current snapshot: the provided base CTE + CLT-leader name resolution.
# One row per person.
SNAPSHOT_QUERY = """
WITH latest_assignment AS (
    SELECT * FROM (
        SELECT
            paam.PERSON_ID,
            paam.ASSIGNMENT_ID,
            paam.ASSIGNMENT_STATUS_TYPE_ID,
            paam.ASSIGNMENT_TYPE,
            paam.SYSTEM_PERSON_TYPE,
            paam.PERIOD_OF_SERVICE_ID,
            paam.PERSON_TYPE_ID,
            paam.EMPLOYMENT_CATEGORY,
            paam.ACTION_CODE,
            paam.REASON_CODE,
            paam.ASS_ATTRIBUTE1,
            paam.EFFECTIVE_START_DATE,
            paam.EFFECTIVE_SEQUENCE,
            ROW_NUMBER() OVER (
                PARTITION BY paam.PERSON_ID
                ORDER BY paam.EFFECTIVE_START_DATE DESC,
                         paam.EFFECTIVE_SEQUENCE DESC
            ) AS rn
        FROM CORP_CORE_DEV.FCT_PER.PER_ALL_ASSIGNMENTS_M paam
        WHERE paam.PRIMARY_ASSIGNMENT_FLAG = 'Y'
    ) WHERE rn = 1
),
person_types AS (
    SELECT
        PERSON_TYPE_ID,
        CASE
            WHEN SYSTEM_PERSON_TYPE = 'EMP' AND USER_PERSON_TYPE = 'Regular' THEN 'REGULAR_FTE'
            WHEN SYSTEM_PERSON_TYPE = 'CWK' THEN 'CONTRACTOR'
            WHEN SYSTEM_PERSON_TYPE = 'EMP'
                 AND USER_PERSON_TYPE IN ('Intern', 'Temporary', 'Seasonal', 'Student')
            THEN 'EXCLUDED_EMP'
            WHEN SYSTEM_PERSON_TYPE IN ('PWK', 'ORA_PER_INTERN') THEN 'PENDING'
            ELSE 'OTHER'
        END AS worker_class
    FROM CORP_CORE_DEV.FCT_PER.PER_PERSON_TYPES_VL
),
status_types AS (
    SELECT ASSIGNMENT_STATUS_TYPE_ID, USER_STATUS, PER_SYSTEM_STATUS
    FROM CORP_CORE_DEV.FCT_PER.PER_ASSIGNMENT_STATUS_TYPES_VL
),
periods_of_service AS (
    SELECT PERIOD_OF_SERVICE_ID, DATE_START, ACTUAL_TERMINATION_DATE
    FROM CORP_CORE_DEV.FCT_PER.PER_PERIODS_OF_SERVICE
),
termination_reasons AS (
    SELECT
        LOOKUP_CODE,
        CASE
            WHEN LOOKUP_CODE = 'VOLUNTARY'    THEN 'VOLUNTARY'
            WHEN LOOKUP_CODE = 'IN_VOLUNTARY' THEN 'INVOLUNTARY'
        END AS termination_category
    FROM CORP_CORE_DEV.FCT_PER.HCM_LOOKUPS
    WHERE LOOKUP_TYPE = 'PER_TERMINATION_TYPE'
),
fan_term_reasons AS (
    SELECT
        LOOKUP_CODE,
        CASE
            WHEN MEANING ILIKE 'INVOLUNTARY_TERMINATION%' THEN 'INVOLUNTARY'
            WHEN MEANING ILIKE 'RESIGNATION%'             THEN 'VOLUNTARY'
            WHEN MEANING ILIKE 'TERMINATION~Resigned%'    THEN 'VOLUNTARY'
            WHEN MEANING ILIKE 'TERMINATION~Retirement%'  THEN 'VOLUNTARY'
            WHEN MEANING ILIKE 'TERMINATION~Job Abandon%' THEN 'INVOLUNTARY'
            WHEN MEANING ILIKE 'TERMINATION~Dismissal%'   THEN 'INVOLUNTARY'
            WHEN MEANING ILIKE 'TERMINATION~Conversion%'  THEN 'INVOLUNTARY'
            WHEN MEANING ILIKE 'DEATH%'                   THEN 'INVOLUNTARY'
            ELSE 'UNCLASSIFIED'
        END AS termination_category
    FROM CORP_CORE_DEV.FCT_PER.HCM_LOOKUPS
    WHERE LOOKUP_TYPE = 'FAN_HR_ADP_TERM_REASONS_LKP'
),
supervisors AS (
    SELECT
        ASSIGNMENT_ID,
        MANAGER_ID AS clt_leader_person_id,
        ROW_NUMBER() OVER (
            PARTITION BY ASSIGNMENT_ID
            ORDER BY EFFECTIVE_START_DATE DESC
        ) AS rn
    FROM CORP_CORE_DEV.FCT_PER.PER_ASSIGNMENT_SUPERVISORS_F
    WHERE PRIMARY_FLAG = 'Y'
      AND MANAGER_TYPE = 'LINE_MANAGER'
),
leader_names AS (
    SELECT PERSON_ID, DISPLAY_NAME FROM (
        SELECT
            PERSON_ID,
            DISPLAY_NAME,
            ROW_NUMBER() OVER (
                PARTITION BY PERSON_ID
                ORDER BY EFFECTIVE_START_DATE DESC
            ) AS rn
        FROM CORP_CORE_DEV.FCT_PER.PER_PERSON_NAMES_F
        WHERE NAME_TYPE = 'GLOBAL'
    ) WHERE rn = 1
),
base AS (
    SELECT
        la.PERSON_ID,
        CASE
            WHEN la.ASSIGNMENT_TYPE = 'E'
                 AND la.SYSTEM_PERSON_TYPE = 'EMP'
                 AND COALESCE(la.EMPLOYMENT_CATEGORY, 'FR') NOT IN ('INTERN', 'TEMP')
            THEN 'REGULAR_FTE'
            WHEN la.ASSIGNMENT_TYPE = 'C' OR la.SYSTEM_PERSON_TYPE = 'CWK'
            THEN 'CONTRACTOR'
            WHEN la.SYSTEM_PERSON_TYPE = 'PWK' THEN 'PENDING'
            WHEN la.SYSTEM_PERSON_TYPE = 'EMP'
                 AND la.EMPLOYMENT_CATEGORY IN ('INTERN', 'TEMP')
            THEN 'EXCLUDED_EMP'
            ELSE COALESCE(pt.worker_class, 'OTHER')
        END AS worker_class,
        CASE
            WHEN la.ASSIGNMENT_STATUS_TYPE_ID IN (3, 4) THEN FALSE
            WHEN pos.ACTUAL_TERMINATION_DATE IS NOT NULL
                 AND DATE(pos.ACTUAL_TERMINATION_DATE) <= CURRENT_DATE()
            THEN FALSE
            ELSE TRUE
        END AS is_active,
        pos.DATE_START                              AS hire_date,
        pos.ACTUAL_TERMINATION_DATE                 AS termination_date,
        COALESCE(tr.termination_category, ftr.termination_category) AS termination_category,
        COALESCE(sup.clt_leader_person_id, TRY_TO_NUMBER(la.ASS_ATTRIBUTE1)) AS clt_leader_id
    FROM latest_assignment la
    LEFT JOIN status_types      st  ON la.ASSIGNMENT_STATUS_TYPE_ID = st.ASSIGNMENT_STATUS_TYPE_ID
    LEFT JOIN periods_of_service pos ON la.PERIOD_OF_SERVICE_ID = pos.PERIOD_OF_SERVICE_ID
    LEFT JOIN person_types      pt  ON la.PERSON_TYPE_ID = pt.PERSON_TYPE_ID
    LEFT JOIN termination_reasons tr ON la.REASON_CODE = tr.LOOKUP_CODE
    LEFT JOIN fan_term_reasons  ftr ON la.ACTION_CODE = ftr.LOOKUP_CODE
    LEFT JOIN supervisors       sup ON la.ASSIGNMENT_ID = sup.ASSIGNMENT_ID AND sup.rn = 1
)
SELECT
    b.PERSON_ID,
    b.worker_class,
    b.is_active,
    b.hire_date,
    b.termination_date,
    b.termination_category,
    b.clt_leader_id,
    COALESCE(ln.DISPLAY_NAME, TO_VARCHAR(b.clt_leader_id), 'Unassigned') AS clt_leader_name
FROM base b
LEFT JOIN leader_names ln ON b.clt_leader_id = ln.PERSON_ID
"""

# Query B - monthly point-in-time Regular-FTE / Contractor headcount snapshot.
# For each month-start (Jan 2026 -> current month), take each person's latest primary
# assignment effective on/before that date, apply the worker-class rule, and exclude
# anyone hired after or terminated on/before the snapshot date. Recomputed each load
# => live restatement.
TREND_QUERY = """
WITH months AS (
    SELECT DATEADD(month, seq, DATE '2026-01-01') AS month_start
    FROM (
        SELECT ROW_NUMBER() OVER (ORDER BY seq4()) - 1 AS seq
        FROM TABLE(GENERATOR(ROWCOUNT => 120))
    )
    WHERE DATEADD(month, seq, DATE '2026-01-01') <= DATE_TRUNC('month', CURRENT_DATE())
),
asg AS (
    SELECT
        PERSON_ID,
        PERSON_TYPE_ID,
        ASSIGNMENT_TYPE,
        SYSTEM_PERSON_TYPE,
        EMPLOYMENT_CATEGORY,
        ASSIGNMENT_STATUS_TYPE_ID,
        PERIOD_OF_SERVICE_ID,
        EFFECTIVE_START_DATE,
        EFFECTIVE_SEQUENCE
    FROM CORP_CORE_DEV.FCT_PER.PER_ALL_ASSIGNMENTS_M
    WHERE PRIMARY_ASSIGNMENT_FLAG = 'Y'
),
person_types AS (
    SELECT
        PERSON_TYPE_ID,
        CASE
            WHEN SYSTEM_PERSON_TYPE = 'EMP' AND USER_PERSON_TYPE = 'Regular' THEN 'REGULAR_FTE'
            WHEN SYSTEM_PERSON_TYPE = 'CWK' THEN 'CONTRACTOR'
            WHEN SYSTEM_PERSON_TYPE = 'EMP'
                 AND USER_PERSON_TYPE IN ('Intern', 'Temporary', 'Seasonal', 'Student')
            THEN 'EXCLUDED_EMP'
            WHEN SYSTEM_PERSON_TYPE IN ('PWK', 'ORA_PER_INTERN') THEN 'PENDING'
            ELSE 'OTHER'
        END AS worker_class
    FROM CORP_CORE_DEV.FCT_PER.PER_PERSON_TYPES_VL
),
periods_of_service AS (
    SELECT PERIOD_OF_SERVICE_ID, DATE_START, ACTUAL_TERMINATION_DATE
    FROM CORP_CORE_DEV.FCT_PER.PER_PERIODS_OF_SERVICE
),
pit AS (
    SELECT
        m.month_start,
        a.PERSON_ID,
        a.PERSON_TYPE_ID,
        a.ASSIGNMENT_TYPE,
        a.SYSTEM_PERSON_TYPE,
        a.EMPLOYMENT_CATEGORY,
        a.ASSIGNMENT_STATUS_TYPE_ID,
        a.PERIOD_OF_SERVICE_ID,
        ROW_NUMBER() OVER (
            PARTITION BY m.month_start, a.PERSON_ID
            ORDER BY a.EFFECTIVE_START_DATE DESC, a.EFFECTIVE_SEQUENCE DESC
        ) AS rn
    FROM months m
    JOIN asg a ON DATE(a.EFFECTIVE_START_DATE) <= m.month_start
              AND a.EFFECTIVE_START_DATE >= DATE '2020-01-01'
),
classified AS (
    SELECT
        af.month_start,
        af.PERSON_ID,
        CASE
            WHEN af.ASSIGNMENT_TYPE = 'E'
                 AND af.SYSTEM_PERSON_TYPE = 'EMP'
                 AND COALESCE(af.EMPLOYMENT_CATEGORY, 'FR') NOT IN ('INTERN', 'TEMP')
            THEN 'REGULAR_FTE'
            WHEN af.ASSIGNMENT_TYPE = 'C' OR af.SYSTEM_PERSON_TYPE = 'CWK'
            THEN 'CONTRACTOR'
            WHEN af.SYSTEM_PERSON_TYPE = 'PWK' THEN 'PENDING'
            WHEN af.SYSTEM_PERSON_TYPE = 'EMP'
                 AND af.EMPLOYMENT_CATEGORY IN ('INTERN', 'TEMP')
            THEN 'EXCLUDED_EMP'
            ELSE COALESCE(pt.worker_class, 'OTHER')
        END AS worker_class,
        af.ASSIGNMENT_STATUS_TYPE_ID,
        pos.DATE_START,
        pos.ACTUAL_TERMINATION_DATE
    FROM pit af
    LEFT JOIN person_types      pt  ON af.PERSON_TYPE_ID = pt.PERSON_TYPE_ID
    LEFT JOIN periods_of_service pos ON af.PERIOD_OF_SERVICE_ID = pos.PERIOD_OF_SERVICE_ID
    WHERE af.rn = 1
)
SELECT
    month_start,
    worker_class,
    COUNT(*) AS headcount
FROM classified
WHERE worker_class IN ('REGULAR_FTE', 'CONTRACTOR')
  AND ASSIGNMENT_STATUS_TYPE_ID NOT IN (3, 4)
  AND (DATE_START IS NULL OR DATE(DATE_START) <= month_start)
  AND (ACTUAL_TERMINATION_DATE IS NULL OR DATE(ACTUAL_TERMINATION_DATE) > month_start)
GROUP BY month_start, worker_class
ORDER BY month_start, worker_class
"""


@st.cache_data(ttl=3600, scope="session", show_spinner="Loading HR snapshot...")
def load_snapshot() -> pd.DataFrame:
    df = CONN.query(SNAPSHOT_QUERY, ttl=0)
    df.columns = df.columns.str.lower()
    for col in ("hire_date", "termination_date"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce")
    return df


@st.cache_data(ttl=3600, scope="session", show_spinner="Loading monthly trend...")
def load_trend() -> pd.DataFrame:
    df = CONN.query(TREND_QUERY, ttl=0)
    df.columns = df.columns.str.lower()
    df["month_start"] = pd.to_datetime(df["month_start"], errors="coerce")
    return df


# =============================================================================
# UI helpers
# =============================================================================


def render_header():
    with st.container(
        horizontal=True, horizontal_alignment="distribute", vertical_alignment="center"
    ):
        st.markdown("# :material/groups: Fanatics Collectibles - HR Metrics (V1)")
        if st.button(":material/restart_alt: Reset", type="tertiary"):
            st.session_state.clear()
            st.rerun()
    if IS_CALLERS:
        st.caption(":material/verified_user: Running with caller's rights - data reflects your role.")
    else:
        st.warning(
            "Caller's rights unavailable (local dev). Numbers may be masked. "
            "Deploy to Streamlit-in-Snowflake for real values.",
            icon=":material/warning:",
        )


# =============================================================================
# Page
# =============================================================================

render_header()

snapshot = load_snapshot()
trend = load_trend()

# --- Filters ---------------------------------------------------------------
with st.container(
    horizontal=True, horizontal_alignment="left", vertical_alignment="center"
):
    leaders = sorted(
        snapshot.loc[snapshot["is_active"], "clt_leader_name"].dropna().unique().tolist()
    )
    selected_leaders = st.multiselect(
        "CLT Leader", options=leaders, default=[], placeholder="All CLT Leaders"
    )
    worker_choice = st.segmented_control(
        "Population (breakdown & trend)",
        options=["Regular FTE", "Contractor"],
        default="Regular FTE",
    )

worker_class = "CONTRACTOR" if worker_choice == "Contractor" else "REGULAR_FTE"

# Apply CLT Leader filter to a working copy (affects breakdown / terminations / trend
# population). KPI cards always reflect the full population.
snap_f = snapshot
if selected_leaders:
    snap_f = snapshot[snapshot["clt_leader_name"].isin(selected_leaders)]

# --- KPI cards -------------------------------------------------------------
active_fte = int(((snapshot["worker_class"] == "REGULAR_FTE") & snapshot["is_active"]).sum())
active_contractors = int(
    ((snapshot["worker_class"] == "CONTRACTOR") & snapshot["is_active"]).sum()
)

term_fte = snapshot[
    (snapshot["worker_class"] == "REGULAR_FTE")
    & (snapshot["termination_date"] >= pd.Timestamp(YTD_START))
]
term_total = int(len(term_fte))
term_vol = int((term_fte["termination_category"] == "VOLUNTARY").sum())
term_invol = int((term_fte["termination_category"] == "INVOLUNTARY").sum())
# term_total counts every terminated Regular FTE regardless of reason, so the
# vol/invol split only ties back to it once the unclassified buckets are shown too.
term_unmapped = int((term_fte["termination_category"] == "UNCLASSIFIED").sum())
term_no_match = int(term_fte["termination_category"].isna().sum())
term_unclass = term_unmapped + term_no_match

cards = st.columns(3)
with cards[0]:
    with st.container(border=True):
        st.metric("Active FTE Headcount", f"{active_fte:,}")
        st.caption("Regular FTEs not terminated (incl. leave/suspended)")
with cards[1]:
    with st.container(border=True):
        st.metric("Active Contractors", f"{active_contractors:,}")
        st.caption("Contingent workers not terminated")
with cards[2]:
    with st.container(border=True):
        st.metric(
            "Terminations YTD",
            f"{term_total:,}",
            help=(
                "Counts every terminated Regular FTE with a termination date on or "
                f"after {YTD_START}, classified or not. Unclassified splits into "
                f"{term_unmapped:,} unmapped reason (ADP reason code matched the "
                "lookup but no voluntary/involuntary pattern) and "
                f"{term_no_match:,} no lookup match (neither termination-reason "
                "lookup returned a row)."
            ),
        )
        st.caption(
            f"Voluntary {term_vol:,}  |  Involuntary {term_invol:,}  "
            f"|  Unclassified {term_unclass:,}"
        )

st.divider()

# --- Headcount by CLT Leader ----------------------------------------------
pop_label = worker_choice
st.markdown(f"### Active {pop_label} by CLT Leader")
by_leader = (
    snap_f[(snap_f["worker_class"] == worker_class) & snap_f["is_active"]]
    .groupby("clt_leader_name")
    .size()
    .reset_index(name="headcount")
    .sort_values("headcount", ascending=False)
)
if by_leader.empty:
    st.info("No matching active population for the current filters.")
else:
    leader_chart = (
        alt.Chart(by_leader)
        .mark_bar()
        .encode(
            x=alt.X("headcount:Q", title="Headcount"),
            y=alt.Y("clt_leader_name:N", title=None, sort="-x"),
            tooltip=[
                alt.Tooltip("clt_leader_name:N", title="CLT Leader"),
                alt.Tooltip("headcount:Q", title="Headcount", format=","),
            ],
        )
        .properties(height=max(CHART_HEIGHT, 24 * len(by_leader)))
    )
    st.altair_chart(leader_chart, use_container_width=True)

st.divider()

# --- Terminations YTD ------------------------------------------------------
st.markdown("### Terminations YTD (Regular FTE)")
term_f = term_fte
if selected_leaders:
    term_f = term_fte[term_fte["clt_leader_name"].isin(selected_leaders)]

tcol = st.columns(2)
with tcol[0]:
    split = category_labels(term_f["termination_category"]).value_counts().reset_index()
    split.columns = ["category", "count"]
    if split.empty:
        st.info("No terminations in range.")
    else:
        split_chart = (
            alt.Chart(split)
            # Horizontal so the four category labels read without rotation.
            .mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4)
            .encode(
                # Fixed sort + fixed color range so a filter that drops a category
                # never repaints the survivors.
                x=alt.X("count:Q", title="Terminations"),
                y=alt.Y(
                    "category:N",
                    title=None,
                    sort=CAT_ORDER,
                    axis=alt.Axis(labelLimit=220),
                ),
                color=alt.Color(
                    "category:N",
                    scale=alt.Scale(domain=CAT_ORDER, range=CAT_COLORS),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("category:N", title="Category"),
                    alt.Tooltip("count:Q", title="Count", format=","),
                ],
            )
            .properties(height=CHART_HEIGHT, title="By termination category")
        )
        st.altair_chart(split_chart, use_container_width=True)
with tcol[1]:
    if term_f.empty:
        st.info("No terminations in range.")
    else:
        monthly = term_f.copy()
        monthly["month"] = monthly["termination_date"].dt.to_period("M").dt.start_time
        monthly["category"] = category_labels(monthly["termination_category"])
        monthly_agg = (
            monthly.groupby(["month", "category"]).size().reset_index(name="count")
        )
        monthly_chart = (
            alt.Chart(monthly_agg)
            # 2px surface gap between stacked segments keeps adjacent fills readable.
            .mark_bar(stroke="#fcfcfb", strokeWidth=1)
            .encode(
                x=alt.X("month:T", title=None),
                y=alt.Y("count:Q", title="Terminations", stack="zero"),
                color=alt.Color(
                    "category:N",
                    title=None,
                    scale=alt.Scale(domain=CAT_ORDER, range=CAT_COLORS),
                    sort=CAT_ORDER,
                    legend=alt.Legend(orient="bottom", columns=2),
                ),
                order=alt.Order("color_category_sort_index:Q"),
                tooltip=[
                    alt.Tooltip("month:T", title="Month", format="%Y-%m"),
                    alt.Tooltip("category:N", title="Category"),
                    alt.Tooltip("count:Q", title="Count", format=","),
                ],
            )
            .properties(height=CHART_HEIGHT, title="By month (V/I stacked)")
        )
        st.altair_chart(monthly_chart, use_container_width=True)

st.divider()

# --- Monthly HC Trend ------------------------------------------------------
st.markdown(f"### Monthly HC Trend - {pop_label}")
st.caption(
    "Point-in-time snapshot on the 1st of each month; prior months restate live as "
    "corrections land (expected behavior)."
)
trend_f = trend[trend["worker_class"] == worker_class].copy()
# Note: the CLT Leader filter is not applied to the point-in-time trend because the
# as-of population is computed server-side without leader attribution.
if selected_leaders:
    st.caption(":material/info: CLT Leader filter does not apply to the point-in-time trend.")
if trend_f.empty:
    st.info("No trend data available.")
else:
    trend_chart = (
        alt.Chart(trend_f)
        .mark_line(point=True)
        .encode(
            x=alt.X("month_start:T", title=None),
            y=alt.Y("headcount:Q", title="Headcount", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("month_start:T", title="Month", format="%Y-%m"),
                alt.Tooltip("headcount:Q", title="Headcount", format=","),
            ],
        )
        .properties(height=CHART_HEIGHT)
    )
    st.altair_chart(trend_chart, use_container_width=True)

conn_label = "caller's rights" if IS_CALLERS else "owner (masked)"
st.caption(
    f":material/schedule: Snapshot as of report load. Tie-out target: Active FTE = 1,954 "
    f"(2026-07-10). Data via {conn_label}."
)
