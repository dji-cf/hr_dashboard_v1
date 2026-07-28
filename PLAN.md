# Fanatics Collectibles — HR Metrics (V1) — Streamlit-in-Snowflake

Interactive Streamlit app surfacing the four validated V1 HR metrics on the new
Snowflake HR database, per the People Analytics requirements (Sigma Dashboard V1,
Chris Tarzian, 2026-07-10).

## Why restricted caller's rights

The role used to build/own this app (`POWER_ANALYST_ORACLE_PROD`) sees **masked**
HR data. The People Analytics role sees **unmasked** data. To let viewers see real
numbers without exposing them to the owner, the app queries Snowflake through a
**restricted caller's rights** connection:

```python
conn = st.connection("snowflake-callers-rights")  # runs as the viewer, not the owner
```

Consequences of restricted caller's rights (Snowflake docs):

- **Container runtime only.** It does NOT work in a warehouse runtime and **errors in
  local dev**. Deploy target is therefore `SYSTEM$ST_CONTAINER_RUNTIME_PY3_11` on a
  compute pool.
- **Uses the viewer's DEFAULT role**, not the role selected in Snowsight. The People
  Analytics viewers' *default* role must be the unmasked one.
- **Session-scoped.** Cached data must use `@st.cache_data(ttl=..., scope="session")`.
- **Create the connection at the top of the script** (auth token valid ~2 min); never
  behind `if`/page branches.
- Requires **Streamlit >= 1.53.1**.

## Metrics (V1 scope)

| Metric | Definition | Cuts |
|---|---|---|
| Active FTE Headcount | Count of Regular FTEs not terminated as of report timestamp. Includes leave/suspended (anyone not terminated counts). | Total + by CLT Leader |
| Active Contractors | Count of contingent workers not terminated as of report timestamp. | Total |
| Terminations YTD | Regular FTE terminations with termination effective date on/after 2026-01-01, counted by effective date (backdated terms land in correct month). Split Voluntary vs Involuntary. | Total + V/I split + by month |
| Monthly HC Trend | Regular FTE headcount snapshot on the 1st of each month, Jan 2026 → present. Live restatement — prior months recompute as corrections land. | By month; Total |

### Shared population rules
- **Regular FTE**: worker type = Employee, excluding intern/temp classes and pending
  (future-start) workers.
- **Not terminated**: no termination effective date on/before the snapshot date.
- **Grain**: latest primary assignment record per person from `PER_ALL_ASSIGNMENTS_M`
  (one person = one head). Job data, not positions.
- **Lookups**: assignment status and action reason lookups joined so filters use
  readable values, not raw codes.

## Data architecture

Two queries, both executed through the caller's-rights connection.

### Query A — current snapshot
The provided CTE (`PER_ALL_ASSIGNMENTS_M` latest primary assignment per person, joined
to status types, periods of service, person types, termination lookups, supervisors),
**extended with a name join** to `CORP_CORE_DEV.FCT_PER.PER_PERSON_NAMES_F` to add
`clt_leader_name`. Returns one row per person:
`person_id, worker_class, is_active, hire_date, termination_date, termination_category,
clt_leader (person_id), clt_leader_name, ...`.

Derived in pandas:
- Active FTE = `worker_class='REGULAR_FTE' AND is_active`.
- Active Contractors = `worker_class='CONTRACTOR' AND is_active`.
- Terminations YTD = `worker_class='REGULAR_FTE' AND termination_date >= '2026-01-01'`,
  grouped by `termination_category` (V/I) and by month.
- Headcount by CLT Leader = active count grouped by `clt_leader_name`.

### Query B — monthly point-in-time HC trend
Generate month-starts Jan 2026 → current month. For each month, take each person's
latest assignment effective on/before that month-start (`ROW_NUMBER` partitioned by
person + month), apply the Regular-FTE rule, exclude anyone with `hire_date > month_start`
or `termination_date <= month_start`. Count by `worker_class` per month. Recomputed on
each load ⇒ automatic live restatement. Returns `month, worker_class, headcount`.

## App layout (single scrolling page, default theme)

1. Header (title + Reset).
2. Filter row: CLT Leader multiselect + worker-class toggle (FTE / Contractor).
3. KPI cards: Active FTE, Active Contractors, Terminations YTD (V/I subvalue). Cards
   always show both populations (ignore the worker toggle).
4. Headcount by CLT Leader — bar chart. Respects CLT filter + worker toggle.
5. Terminations YTD — V/I split + monthly stacked bar (V vs I). Respects CLT filter.
6. Monthly HC Trend — line chart. Respects worker toggle; CLT filter applies to the
   point-in-time population.

## Deploy target

| Setting | Value |
|---|---|
| App name | `HR_METRICS_V1` (display: "Fanatics Collectibles — HR Metrics (V1)") |
| Database.Schema | `ORACLE_DATA_PROD.SANDBOX` |
| query_warehouse | `ANALYTICS_WAREHOUSE` |
| runtime_name | `SYSTEM$ST_CONTAINER_RUNTIME_PY3_11` |
| compute_pool | `SYSTEM_COMPUTE_POOL_CPU` (account `DEFAULT_STREAMLIT_COMPUTE_POOL`) |
| external_access_integrations | `PYPI_ACCESS_INTEGRATION` |

Deploy via `snow streamlit deploy --replace` (CLI 3.21.0).

## Prerequisites / open items (require an admin — outside this app)

1. **Caller grants** — a role with `MANAGE CALLER GRANTS` must grant `CALLER USAGE` on
   `CORP_CORE_DEV` + `FCT_PER` and `CALLER SELECT` on each source table to the app-owner
   role. Without these, caller's-rights queries fail at runtime with an authorization
   error. Source tables:
   - `CORP_CORE_DEV.FCT_PER.PER_ALL_ASSIGNMENTS_M`
   - `CORP_CORE_DEV.FCT_PER.PER_PERSON_TYPES_VL`
   - `CORP_CORE_DEV.FCT_PER.PER_ASSIGNMENT_STATUS_TYPES_VL`
   - `CORP_CORE_DEV.FCT_PER.PER_PERIODS_OF_SERVICE`
   - `CORP_CORE_DEV.FCT_PER.HCM_LOOKUPS`
   - `CORP_CORE_DEV.FCT_PER.PER_ASSIGNMENT_SUPERVISORS_F`
   - `CORP_CORE_DEV.FCT_PER.PER_PERSON_NAMES_F`
2. **Viewer default role** — People Analytics viewers' *default* role must be the
   unmasked one (caller's rights ignores the Snowsight-selected role).
3. **PyPI EAI USAGE** — `GRANT USAGE ON INTEGRATION PYPI_ACCESS_INTEGRATION TO ROLE <owner>`.

## Tie-out / acceptance

- Active FTE must match within ±1–2 heads of **1,954 as of 2026-07-10**.
- Remaining baselines (Contractors, Terminations, Trend) supplied by Chris at review.
- Any dev1 ↔ prod discrepancy documented (not absorbed).
- Sign-off: Chris Tarzian per metric before sharing beyond the working team.

## Known limitations

- Caller's rights can't run locally — full validation requires the deployed app viewed
  by an unmasked-default-role user. Local `streamlit run` uses an owner connection and
  shows masked numbers (import/UI validation only, not tie-out).
- Monthly HC Trend intentionally restates history; it will diverge from frozen
  monthly-report snapshots over time. Expected behavior, not a defect.
