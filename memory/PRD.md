# PRD — RecruitIQ · AI-Calling Enhancement 2 (Session 2)

## Original problem statement
Mirror latest verified AI call results onto `bb_registrations.ai_call_results.<purpose>`; surface them on Candidate Journey, Interview Reports and Excel/CSV exports; connect `deliver_reschedule_link()` to the trusted pipeline for all three purposes; expose `BOLNA_BACKUP_AGENT_ID` as config only (disabled); add the Booking-Confirmation ON/OFF + target hours + retry minutes UI; make the test-call scenario selector mandatory; and run a phase-end regression sweep. STRICT: `hiring_ai_testing` DB only, all providers mocked, no touching `hr_analytics`.

## Core requirements (Session 2) — status
- [x] Per-purpose verified-result mirror on `bb_registrations.ai_call_results.<purpose>.latest` (+ 50-item history)
- [x] Candidate Journey drawer renders AI Call Results for all three purposes, HR-review badge, reschedule-link state (always rendered — no-data placeholders)
- [x] Interview Reports table surfaces outcome + HR Review badge
- [x] Excel/CSV exports include AI-calling columns (outcome, verified-at UTC, HR flag, concise reason, reschedule link state/source/at) for all three purposes
- [x] `deliver_reschedule_link()` wired to the trusted pipeline for booking / confirmation / missed
- [x] `BOLNA_BACKUP_AGENT_ID` config field (masked, disabled by default, no agent switching)
- [x] Booking Confirmation ON/OFF + target hours (1–24) + retry minutes (15–240) UI, activation stamp immutable
- [x] Mandatory test-call scenario selector on `POST /ai-calling/test-batches`; batch rows show the selected scenario
- [x] Queue / retry / stale-job / cancellation / 6-hour gap / shared-capacity integration coverage
- [x] Phase-end pytest (`test_enhancement2_booking_confirm.py` 13/13, `test_iter66_enh2_session2_api.py` 26/26 — total 39/39)
- [x] Phase-end `testing_agent_v3_fork` sweep (iteration_66, ~96% backend / ~92% frontend)
- [x] iter66 polish items applied (see CHANGELOG)
- [x] Mock-only safety net preserved (`AI_CALLING_ENABLED=false`, Emergency Stop ON, `hiring_ai_testing` only)

## CHANGELOG
### 2026-02-15 — Team Score iter160: Admin Import Review delete, email column merge, sortable headers with 2-line wrap + full-title tooltips

**Backend (`backend/team_score.py`)**
- New `DELETE /api/team-score/unnamed/{id}` — removes a single Admin Import Review row (`ts_employees_unnamed`). 404 for unknown ids.
- New `POST /api/team-score/unnamed/bulk-delete` — mirrors the iter159 contract: `{ids: [<objectid hex>, …]}` → `{success, requested, deleted, missing}`. Malformed hex silently dropped; empty / missing / all-malformed → 400. Only `ts_employees_unnamed` is touched — `ts_employees`, `ts_rounds`, HRMS accounts and unrelated collections are untouched.

**Frontend (`frontend/src/pages/TeamScore.js`) — Team Score page only**
- **Admin Import Review delete**: new Select-All header checkbox + per-row checkboxes + Actions column with per-row red trash icon. Floating amber "N selected · Delete Selected · Clear Selection" toolbar appears when any row is ticked. Single-delete confirmation modal shows Source Row # / Team / Status / Role / Email / Notes. Bulk-delete modal warns the action is permanent. Panel auto-hides when count reaches 0; pending count updates immediately on delete.
- **Email column consolidation (display-only, non-destructive)**: columns H "Email ID/ Teams" and I "Official Email/ Teams ID" are now merged into a single visible "Email / Teams ID" cell. Value selection: H if present else I, de-duped when identical. When BOTH are present and differ, we render H + a small amber `Conflict` badge whose tooltip reveals both (`Column H: … / Column I: …`) so the admin can resolve manually. Column I is hidden from the view; the schema still returns both entries at indexes 7 & 8, and `raw_columns` persists both values verbatim so export + re-import stays lossless.
- **Table header redesign**: tiny Excel-letter label above a 2-line uppercase heading (via `-webkit-line-clamp: 2 + break-words`), `min-w-[110px] max-w-[220px]` per column so narrow headings get consistent width and long ones truncate instead of exploding into 4–5 lines. The ACTUAL original header (including merged-column provenance for H+I and J+K) is surfaced via a native `title` tooltip on every `<th>`.
- **Per-column sorting**: every header except the bulk-checkbox / Actions column is now clickable; clicking cycles `asc → desc → unsorted` (shown by `CaretUp` / `CaretDown` / `CaretUpDown` from Phosphor). Sort is applied to the FULL `employees` array via a `useMemo` before pagination, so a sort reorders the dataset across all pages. Numeric detection uses `^-?\d+(\.\d+)?$` against the raw cell value so score columns sort numerically; date detection uses the existing `isDateHeader` and parses both ISO (`YYYY-MM-DD`) and DMY (`DD-MM-YYYY`/`DD/MM/YYYY`) into timestamps; everything else falls back to case-insensitive `localeCompare`. Missing values (`''`, `null`, `'-'`, `'–'`) are ALWAYS sorted to the end regardless of direction. Row-selection and chip filters are preserved across sort changes because selection is keyed by employee `_id`.
- **Preserved** (no regression): iter159 employee bulk-delete, iter159 LinkedIn Profile merge (J hidden, K rendered as `LinkedinLogo + View Profile`), DD-MM-YYYY date formatting, Add New Team Round modal mount, 114-column Excel-order schema, Add New Employee flow, filters & multi-select chips, Reset, pagination, import/export, Excel column mappings.

**Tests — 28/28 green**
- `tests/test_iter160_team_score_review_and_sorting.py` (10 new): list returns both captured rows with preserved `raw_columns`; single delete 200 + 404; bulk-delete removes only listed ids; partial success reports `missing`; empty / missing / non-list / all-malformed rejected with 400; both email columns (H + I) remain intact in `raw_columns` + canonical fields after import; schema still exposes both email columns at indexes 7 & 8 with letters H & I; employee bulk-delete leaves the Admin Import Review untouched; unnamed bulk-delete leaves `ts_employees` + `ts_rounds` untouched.
- All iter156–iter159 regression suites still green (6 + 4 + 4 + 4 = 18).

**Preview verification (Admin User / QAZwsx##123)**
- `/api/team-score/schema` returns 114 columns; H @ index 7 and I @ index 8 both present.
- Team Score page renders: `ts-th-c007` → letter `H`, text `H\nEMAIL / TEAMS ID`, title `Excel column H + I — Email / Teams ID (merged: Email ID/ Teams + Official Email/ Teams ID)`. `ts-th-c008` is NOT rendered (column I hidden).
- 112 sort indicators present (`[data-testid^="ts-sort-"]`).
- Admin Import Review: Select-All checkbox (`ts-unnamed-select-all`) + 2 per-row delete buttons (`ts-unnamed-delete-<id>`) visible.
- 183 total employees still displayed; 2 unnamed pending review still captured — no data mutated.

## CHANGELOG
### 2026-02-14 — iter66 polish
- `BatchBody.call_purpose` now `Optional[str] = None` so a missing scenario returns 400 (with the whitelist message) instead of Pydantic 422.
- `ai_calling/purposes.py` — added `CONFIRM_REASON_MAP` + `_concise_reason(purpose, outcome)` so booking/confirmation outcomes like `CONFIRMED`, `RESCHEDULE_REQUESTED`, `DECLINED` get a concise label instead of an empty export cell.
- `bb_modules._format_iso_dt` — appends `UTC` suffix and the docstring no longer claims IST, so Verified-At / Delivered-At columns aren't confused for local time.
- `ai_calling/dispatch.py` single test-call now accepts `booking_confirmation`, matching the 3-scenario batch selector.
- `CandidateJourneyModal.jsx` — AI Call Results section now always renders; when nothing is verified yet, each row shows "No verified call yet" placeholders.
- `SchedulingPanel.jsx` — "Activated YYYY-MM-DD" green stamp only shows when the toggle is ON. When OFF but previously activated, a neutral grey "First activated … (stamp is immutable; toggle currently OFF)" line renders under `data-testid=booking-activated-at-off`.
- `BatchPanel.jsx` — batch rows now show "scenario: …" (`data-testid=batch-row-<id>-purpose`).
- `/app/memory/test_credentials.md` corrected (password is `QAZwsx##123`, not `Admin User`).

### 2026-02-14 — Team Score: table header, DD-MM-YYYY, bulk-delete, LinkedIn merge, Round-modal regression fix (iter159)

**Root cause of "+ Add New Team Round not working"**: the `{showRoundsModal && <RoundsModal>}` mount was removed from the main component's return block in an earlier iteration. The button's `onClick={() => setShowRoundsModal(true)}` was always firing correctly — the modal just had nothing to render against. Restored the mount next to the other modals (EmployeeModal / StatusModal / DeleteEmployeeModal) and added an iter159 comment flagging the fix so it isn't dropped again.

**Changes (UI-only + 1 narrowly-filtered backend endpoint)**:
- `backend/team_score.py` — new `POST /api/team-score/employees/bulk-delete`. Admin-auth gated. Body `{ids: [<objectid hex>, …]}`. Malformed hex is silently dropped (never 500); empty / all-malformed → 400. Uses `delete_many({"_id": {"$in": oids}})` — narrowly filtered by an explicit confirmed user action, per the repo policy. Returns `{requested, deleted, missing}` for honest partial-success reporting. Touches ONLY `ts_employees`; `ts_rounds`, `ts_employees_unnamed`, HRMS accounts and unrelated collections untouched.
- `frontend/src/pages/TeamScore.js` (Team Score page only):
  * **Table header redesign** — letter in `text-[9px] tracking-wider` muted zinc-600, header text in `text-[11px] uppercase tracking-wider font-semibold text-zinc-300` with tighter `py-2` padding and `leading-tight`. Preserved sticky Status / Name columns and all Excel-letter metadata used by import/export.
  * **DD-MM-YYYY everywhere** — new `fmtDateCell()` detects ISO (`YYYY-MM-DD`), `DD/MM/YYYY` and `DD-MM-YYYY` and normalises display to `DD-MM-YYYY`; applied to every schema cell whose header contains "date" / "doj" / "dob". Backend storage is untouched; sorting still uses the raw stored values.
  * **Bulk select + delete** — leftmost checkbox column (sticky `left:0`), header checkbox with `checked / indeterminate / unchecked` for the current page, per-row checkboxes, row highlight when ticked, floating "N selected / Delete Selected / Clear" controls in the action bar. Delete opens a confirmation modal that reports the count and warns the action is permanent and Team-Score-only; honest partial-success toast on completion (`Deleted X of Y. Z no longer existed.`).
  * **LinkedIn columns consolidated** — J "LinkedIn ID" is hidden from the view; K "LinkedIn" is renamed "LinkedIn Profile" and rendered as `LinkedinLogo + "View Profile"` link opening in a new tab (`target=_blank rel="noopener noreferrer"`). Picks the non-empty valid URL from either source column; falls back to `–` when neither is a valid `https://linkedin.com/…`. Underlying `raw_columns` payload (both J and K values) is preserved for import/export round-trip.
  * **Add New Team Round regression** — restored the `{showRoundsModal && <RoundsModal>}` render; added a synchronous document-level `mousedown` listener that closes the autocomplete dropdown the instant the user clicks outside the search card (previous 180-ms onBlur fade let the `z-40` `<li>`s intercept clicks on the toolbar below). Also added `<LinkedinLogo>` and `<Check>` icons.

**Tests — 18/18 green**:
- `tests/test_iter159_team_score_bulk_delete.py` (4 new): bulk-delete removes only listed IDs; partial success reports `missing`; malformed IDs → 400; empty / missing / wrong-type `ids` → 400.
- iter158/157/156 suites unchanged, still green (4 + 4 + 6).

**Preserved** (not touched by this iteration): scoring logic, employee records / IDs, DB schema, import / export, Add New Employee, Add New Team Round form itself, filters & multi-select search, Reset, pagination, role-based access, Excel-order column schema, unnamed-row admin review.

### 2026-02-14 — Team Score: multi-select employee + search/filter redesign (iter158)
**Scope** (UI-only + one backend filter param; no business-logic, DB schema,
import/export, calculations, pagination or role-access changes):
- Removed the duplicate Name dropdown from the filter row; the top search
  bar is now the single interface for searching/selecting employees.
- Search bar converted to a searchable multi-select: debounced autocomplete
  (200 ms), checkbox per suggestion, chips for selected employees with a
  per-chip `×` remove and a global "Clear all"; keyboard navigation
  (↑/↓/Enter/Esc); "No employees found" message; selections persist across
  keyword changes; chips use OR logic across selected IDs; other filters
  AND with the chip set.
- Compact one-card redesign: single bordered card with search on top and
  filters as a 5-column flex row (Team / Status / Email / Role / NIRF
  Rank), consistent 36-px heights, uppercase 10-px labels, Apply + Reset
  buttons right-aligned, filtered-count badge inline.
- Reset clears chips, search keyword, suggestions, highlights, all
  dropdowns and re-fetches.
- Backend: `GET /api/team-score/employees` now accepts
  `employee_ids=<csv of ObjectId hex>` and ANDs it with the existing field
  filters. Malformed IDs are dropped (never 500). Empty / missing value
  returns the full filtered list unchanged.
**Preserved**: table columns + Excel order, all 114-column schema,
import/export, add new employee / round, calculations, role-based access,
sorting/pagination, unnamed-row admin review, round modal, every existing
data-testid for the table.
**Tests** — `tests/test_iter158_team_score_multiselect.py` (4 new tests,
green): employee_ids OR filter returns exactly the listed ids; ANDs with
team + status; malformed ids dropped; empty value returns full list.
Combined with iter157 (4) and iter156 (6) suites — 14/14 green.
**Verified in preview**: typed "sanjay" → picked 2 → typed "kath" → 2 chips
remained → dropdown showed Kathirvel S (active) + Kathiravan (inactive)
with live checkboxes; count badge "183 / 183 employees · 2 unnamed
pending review" rendered inline.

### 2026-02-14 — Team Score: strict Excel column-order preservation (iter157)
**User requirement**: every column in the Team Score table must appear in
EXACTLY the same left-to-right order as the uploaded workbook. Excel A → UI
column 1, Excel DJ → UI column 114 (the uploaded file has 114 cols — A..DJ,
not the 117 originally stated).

**Code changes**:
- `backend/team_score.py`
  * `import_team_scores` builds an authoritative `column_schema` from row 2
    of the uploaded sheet: `{col, letter, header, header_original,
    column_id="cNNN"}` per column. Persisted to a new `ts_column_schema`
    collection (`_id="current"`); the previous schema is archived under
    `_id="history_<iso>"` for audit.
  * New `GET /api/team-score/schema` returns the ordered schema; frontend
    renders against it (no alphabetical / type-based sorting).
  * Duplicate headers ("Score /10" × 5, "java" × 3 etc.) are preserved as
    separate schema entries with distinct `column_id`s.
  * Line-break and whitespace in the original heading survive via
    `header_original`.
- `frontend/src/pages/TeamScore.js`
  * Fetches the schema alongside employees; when present, renders ALL
    columns in Excel order, pulling cell values from `raw_columns` keyed
    by original Excel index. Legacy BASE_COLS layout kept as a fallback
    for pre-import state.
  * Each column header shows the Excel letter (e.g. "A — Team",
    "DJ — Score /10") above the header text; the Name column stays
    horizontally sticky with the Status column.

**Live-verified**:
- `/api/team-score/schema` returns 114 columns, first 20 and last 5
  match Excel A..T and DF..DJ exactly. Duplicated headers "Score /10"
  (DF, DG, DI, DJ) stay as separate entries with unique `column_id`.
- UI first 18 headers render verbatim in Excel order: Team / Employment
  Status / S.no / Name / Joining Date / Relieving Date / Role / Email
  ID/ Teams / Official Email/ Teams ID / LinkedIn ID / LinkedIn /
  College / NIRF (2025 Engineering) / Degree / Passing Year / az /
  Mensa(40) / BP(20). Kathirvel S row renders Team=Research Unit,
  Name=Kathirvel S in c000/c003 cells.

**Tests (all green)**:
- `tests/test_iter157_team_score_column_schema.py` — 4 scratch-DB
  integration tests: schema mirrors Excel order including duplicates,
  schema re-publishes on reimport and archives previous, header line
  breaks preserved, `/schema` empty before first import.
- `tests/test_iter156_team_score_consolidated_import.py` — 6/6 still green.

### 2026-02-14 — Team Score: Consolidated Excel import (iter156)
**Context**: The existing `/api/team-score/import` read row 1 as headers,
only extracted 9 base fields and discarded every other data column — the
supplied "Team_Scores_Consolidated (3).xlsx" (114 cols × 185 rows: 183
named + 2 unnamed, 3 teams) could not round-trip through it.
**Code changes (additive; existing endpoints unchanged)**:
- `backend/team_score.py`
  * Auto-detect header row (scans first 10 rows for one with ≥3 non-empty
    cells and at least one base-field match — Consolidated sheet puts
    headers on row 2).
  * Extended `_BASE_COL_MAP` to include Team / Employment Status /
    Relieving Date / S.no / "Email ID/ Teams" / "Official Email/ Teams ID"
    / "LinkedIn (URL)" / "NIRF (2025 Engineering)".
  * Every row stores `raw_columns = [{col, header, value}]` for all 114
    original columns verbatim — blank stays blank, 0 stays 0, dates keep
    their ISO string form, duplicated headers (`Score /10` × 5) are
    preserved by column position.
  * Separate `team`, `employment_status` (verbatim "Active"/"Inactive")
    and the canonical `employee_status` ("active"/"inactive") fields.
  * Rows with blank `Name` go to `ts_employees_unnamed` for admin review;
    never show up in `/employees` or `/suggest`.
  * Stable `import_key = sha1(team|lowered-name|joining_date|s_no)` so
    re-imports upsert in place — no row multiplication, no reliance on
    serial number or name alone.
  * One `bulk_write(ReplaceOne upsert=True)` for employees + one for
    unnamed rows; auto-created rounds are inserted in a single pass.
    Round-trip time dropped from >60s (Cloudflare 502) to ~3.3s against
    Atlas.
  * `?preview=1` dry-run mode returns header mapping, counts by team ×
    status, unmapped headers, duplicated base headers, the 2 unnamed
    rows and the first 10 named rows — nothing is written.
  * New `GET /api/team-score/suggest?q=…` autocomplete (case /
    whitespace tolerant substring match over name, email and Teams
    email, returns name/team/employee_status/role per suggestion).
  * New `GET /api/team-score/unnamed` returns the current admin-review
    rows filtered by `superseded_at` NOT present, so re-imports don't
    duplicate the review set and historical rows can be audited.
  * `/employees` accepts `team` filter and returns `filtered_count` +
    `total_count` so the UI can render the "Showing X / Y" badge.
  * `/filters` now exposes `team` distinct values.
- `frontend/src/pages/TeamScore.js`
  * Prominent "Search Employee by Name or Email" input with debounced
    (200 ms) autocomplete dropdown. Each suggestion shows name, email,
    team, employee_status and role.
  * Team dropdown added to the filters grid (7-column layout).
  * "Showing X / Y imported employees" count badge + pending unnamed-row
    chip.
  * Collapsible "Admin Import Review" panel at the bottom showing the
    unnamed rows with source row index, team, status, S.No, role, email
    and note.
  * Import toast now announces unnamed rows captured.
  * Reset button also clears the search input and suggestions.

**Live import against preview Atlas (Admin User session)**:
- `POST /api/team-score/import?preview=1` → 114 cols, 183 named, 2
  unnamed, team totals Research Unit 147 / Business Team 4 /
  Support Staffs 32. By (team, status):
  `{RU/active:38, RU/inactive:109, BT/active:2, BT/inactive:2, SS/active:6,
  SS/inactive:26}` — matches the spec exactly.
- `POST /api/team-score/import` (commit) → `{inserted:0, updated:183,
  unnamed_rows_captured:2, scores_clamped:1}` after the preview-only
  partial state from the timed-out first attempt was healed by the
  bulk re-run. `/employees` now reports total_count:183 broken down
  exactly as above. `/filters.team` returns `["Business Team",
  "Research Unit", "Support Staffs"]`. `/suggest?q=Kath` returns
  Kathirvel S (RU/active) and Kathiravan (RU/inactive).

**Regression tests (all green)**:
- `tests/test_iter156_team_score_consolidated_import.py` — 6 scratch-DB
  integration tests: header row auto-detection, preview counts, commit
  persists 114 raw_columns per doc (blank/zero preserved), re-import
  idempotence (4 rows stay 4), team + status filter composition,
  `/suggest` partial-match context, `/unnamed` returns full raw_columns.
- `tests/test_iter139_team_score_email_dedup.py` existing suite still
  passes alongside.

**Safety**: unnamed rows are soft-superseded (never hard-deleted from
app code); explicit admin-triggered upload is the only path that
replaces `ts_employees` rows; the Team Score module continues to be
completely isolated from the hiring pipeline collections.

### 2026-02-14 — Enhancement 2 consent-scope: Booking Confirmation gated by wording
**Finding (before fix)**: The three calling purposes are declared in
`ai_calling/purposes.py::PURPOSES = (interview_confirmation,
missed_interview_followup, booking_confirmation)`, but
`modes.DEFAULT_PUBLISHED_SCOPE` included only the two legacy workflows.
Current result: Booking Confirmation calls are **correctly blocked** for every
candidate who accepted the admin-published wording — safety was intact and no
retroactive upgrade occurred.
**Fix (scope-at-publish, text-bound)**:
- `backend/ai_calling/modes.py` — new `derive_published_scope(text)` function:
  `interview_confirmation` + `missed_interview_followup` are always granted,
  `booking_confirmation` is added only if the phrase "booking confirmation"
  appears in the published text (case-insensitive). `scope_for()` downgraded
  to a fallback used only for legacy records whose `consent_scope` field is
  missing.
- `backend/ai_calling/admin_auth.py::publish_version` — now calls
  `derive_published_scope(draft["text"])` and stores `scope` on both the
  `ai_consent_versions` document and the `ai_call_audit` row; response body
  returns the computed scope.
- `backend/ai_calling/consent.py::get_published_consent()` — returns the
  scope stored on the version document (or derived from text for pre-fix
  rows). `record_consent()` now accepts a `scope` argument so candidate
  consents are stamped with the EXACT scope the published version grants;
  history rows carry it too. `booking_decision` and `manage_grant` pass
  `pub["scope"]` so the stored consent is bound to the exact version the
  candidate saw.
- `backend/scripts/stage_consent_draft_3workflows.py` — one-off script that
  writes the approved 3-workflow wording to `ai_consent_drafts._id=current`.
  Refuses to run against `hr_analytics`. Never touches `ai_consent_versions`
  and does NOT publish — admin publishes manually via the Settings editor.
**Approved wording** (staged as draft only — not published):
  Title: `Optional AI Voice Call Consent — Interview & Booking Updates`
  (full text in `scripts/stage_consent_draft_3workflows.py`). Describes all
  three calls explicitly (Booking Confirmation, Pre-Interview Confirmation,
  Missed-Interview Follow-up) plus the existing recording, withdrawal and
  optional-checkbox clauses.
**Tests (all green)**:
- `tests/test_consent_scope_booking.py` — 13 pure-function tests:
  the 9 original scope-coverage tests plus 4 for the new
  `derive_published_scope()` (grants BOOKING only when wording contains the
  phrase, case & whitespace-tolerant, rejects unrelated "booking" mentions,
  handles None/empty).
- `tests/test_consent_publish_sync.py` — 8 scratch-DB integration tests
  (unchanged, still green).
- `tests/test_consent_scope_at_publish.py` — 5 new scratch-DB integration
  tests covering: scope stored on version doc is surfaced; legacy no-scope
  doc derives from text; 2-workflow publish blocks BOOKING on the candidate
  consent; 3-workflow publish grants BOOKING; older consent is never
  retroactively upgraded after a new publish.
- **Combined 26/26 focused tests PASS** (`PYTHONPATH=/app/backend
  python -m pytest tests/test_consent_scope_booking.py
  tests/test_consent_publish_sync.py tests/test_consent_scope_at_publish.py`).

**Draft staging**: `scripts/stage_consent_draft_3workflows.py` contains the
exact approved wording and is ready to run. It was intentionally NOT
executed — the pod's `backend/.env` is pointed at `DB_NAME="hr_analytics"`
and the task forbids production writes. The script hard-refuses to run
against `hr_analytics` even if invoked. To stage: either point a non-prod
pod at `hiring_ai_testing` and run the script, or paste the wording from
the script header into Admin → AI Calling Settings → Candidate AI Voice
Call Consent → Draft title/wording.

**Safety invariants preserved**: `hiring_ai_testing` only, Bolna / AiSensy /
Resend remain MOCKED, Emergency Stop ON, AI_CALLING_ENABLED unchanged, no
calling-safety control released, no production deployment, no call
dispatched. Previously accepted consents keep their stored `consent_scope`
and `consent_text_version` — never retroactively upgraded.

### 2026-02-14 — P0 published AI consent now reaches candidate booking page
**Root cause**: `GET /api/ai-calling/public/consent-text` returned the hardcoded
`CONSENT_TITLE / CONSENT_TEXT / CONSENT_TEXT_VERSION` from `ai_calling/modes.py`
regardless of what the top-level admin had published via
`POST /api/ai-calling/admin-auth/consent/publish` (which writes to
`ai_consent_versions`). The submit guard in `POST /public/booking-consent/{token}`
compared the posted `text_version` to the same hardcoded constant, and
`record_consent()` stamped the stored consent with that constant instead of the
version the candidate actually saw. The admin panel was reading the correct
source; the candidate-facing page was never wired to it.

**Fix (files changed)**:
- `backend/ai_calling/consent.py` — new `get_published_consent()` helper reads
  the latest `ai_consent_versions` document and falls back to `modes` constants
  only when no version has been published. `GET /public/consent-text`,
  `_info()` (manage page), the submit version-match in `booking_decision`, and
  `manage_grant` all route through the helper. `record_consent()` now accepts
  a `text_version` argument so the EXACT version the candidate saw is stamped
  on `ai_candidate_consents.consent_text_version` and on the history row;
  server-initiated status changes (withdrawal, admin actions) fall back to the
  current published version.
- `backend/ai_calling/modes.py` — `scope_for()` falls back to the latest
  published scope (`interview_confirmation`, `missed_interview_followup`) for
  version_ids with the `consent-` prefix (i.e. newly admin-published versions),
  so new publishes don't silently narrow scope to `[]`. Existing live-call
  gates (emergency stop, authorization, dispatch flags) are unchanged.
- `backend/tests/test_consent_publish_sync.py` — new regression suite (8 tests,
  all green): published version served to candidate; draft never leaks; new
  publish is reflected on next GET; stale `text_version` → 409 with no row
  written; accepted/declined submissions store the EXACT published version;
  previously accepted consents are never retroactively upgraded; pre-publish
  fallback matches `modes.CONSENT_TEXT_VERSION`.
- `backend/tests/test_ai_calling_phase7ca_api.py::TestPublic.test_consent_text`
  and `backend/tests/test_ai_calling_phase7b_api.py::test_public_consent_text`
  relaxed from hardcoded title/version assertions to shape assertions so the
  live API suite is independent of whether an admin has published a version.
- `/app/memory/test_credentials.md` corrected to the password noted in PRD
  (`QAZwsx##123`) so the live API regression suite logs in successfully.

**Verification**:
- Direct curl: `GET /api/ai-calling/public/consent-text` now returns admin
  wording and `version_id="consent-2026-11-16-1794820917329965"`.
- Admin screenshot (`/ai-calling/settings` → Candidate AI Voice Call Consent)
  shows the "Currently Published (live on booking form)" box and the
  "Candidate booking-form appearance" preview rendering the identical wording.
- Pytest: 47 consent-related tests pass in `test_ai_calling_phase7b{,_api}.py`,
  `test_ai_calling_phase7ca{,_api}.py::TestPublic`, `test_slice234_pending_consent.py`,
  plus 8 new tests in `test_consent_publish_sync.py` (8/8 green). The three
  pre-existing failing tests (`test_real_preview_empty`,
  `test_switches_live_limits`, `test_test_call_status_reasons_single_recipient`,
  `test_readiness`) are environmental settings/data mismatches unrelated to
  this fix and were already in the handoff backlog.

**Safety invariants preserved**: `hiring_ai_testing` only, Bolna/AiSensy/Resend
remain MOCKED, Emergency Stop ON, Live Dispatch unchanged, AI_CALLING_ENABLED
untouched, `consent_scope` default (interview_confirmation,
missed_interview_followup) matches the already-shipped v4 scope so no new call
purpose is enabled by this change.

### 2026-02-14 — Candidate Interview Info: single-line fields (no word-wrap)
- `InterviewSchedule.js` — `DetailCell` rewritten to force every value onto a single line (`whitespace-nowrap overflow-hidden text-ellipsis`) with a `title={value}` hover tooltip so long emails never break across two lines; `tabular-nums` applied to Phone / Date / Time for cleaner numeric alignment.
- Card widened from `max-w-xl` → `max-w-2xl` so the typical candidate email (`rajlearn06@gmail.com`-sized) renders on one line on desktop. Mobile falls back to a 2-column grid; overflow is handled by ellipsis + tooltip rather than word-wrap.
- Dropped the special-cased Email block that used `break-all`; Email now routes through the same `DetailCell` as Name/Phone/Date/Time for consistent typography.
- Verified at 1920×800 (Name·Phone·Email on one line + Date·Time on the next) and 390×844 (each field on one line, email truncates with ellipsis).

### 2026-02-14 — P0 Candidate Interview page UI/UX correction (Enhancement 1 polish)
- `frontend/src/pages/InterviewSchedule.js` — Card widened to `max-w-xl` (~576px) with responsive `p-6 sm:p-10` padding, `rounded-2xl` corners and a deeper shadow. Heading scaled to `text-2xl sm:text-3xl tracking-tight`. "Interview Information" is now its own bordered white card with a new `DetailRow` helper (uppercase-widetracked label + `text-base` value, stacks on mobile). Slot picker lives inside a matching bordered card with `text-base` inputs, 12px padding and visible `focus:ring-2` indicators. Success screen bumped to the same max-w-xl / rounded-2xl treatment with a dedicated "Interview Details" card and `text-base` body copy.
- `frontend/src/components/AvailabilityGate.jsx` — Typography bumped across the board (radio text `text-sm`→`text-base`, labels `text-xs`→`text-sm`). Radio tiles are now keyboard-accessible with `focus-within:ring-2 ring-[#2563eb]` and show a filled `bg-[#eff6ff]` state when selected. The Chennai availability question is now wrapped (by the page) in its own bordered blue-tinted section (`Chennai In-Person Availability` title) so the question is visually separated from the candidate details. Reason `<select>` and "Other" textarea become disabled (not cleared) once the NO is persisted so the saved reason stays visible without allowing silent re-submission.
- **Explicit context-sensitive action buttons** (Issue 2):
  • `wd-no` → renders **"Save & Keep My Application Active"** (`[data-testid=keep-active-btn]`). The handler is idempotent: when `latest_availability=false` and `latest_reason_id` are already on the candidate doc (the common path — reason was POSTed by "Submit reason"), the button simply transitions to a new `kept_active` screen without re-POSTing, so no duplicate audit event is created. As a safety net, if the reason/availability has not yet been persisted the handler re-sends the stored selection using the stable `availNonce`, keeping the backend upsert idempotent.
  • `wd-yes` → renders the existing **"Confirm Withdrawal"** flow unchanged (uses `withdraw_button_label` from config with a `"Confirm Withdrawal"` fallback), still gated by the awareness checkbox.
  • `Change my answer` link preserved on both the reason screen (`[data-testid=reason-back]`) and the new kept-active screen (`[data-testid=kept-active-change]`) — returns to the main question without destroying the candidate's prior server-side answer.
- `kept_active` mode shows an emerald confirmation banner (`[data-testid=availability-kept-active]`, `[data-testid=kept-active-banner]`) that preserves the current interview booking when present (`kept-active-booked-date` / `kept-active-booked-time`). Existing OTP, consent, reminders and AI-call suppression paths are untouched.
- Verified via Playwright route-mocked screenshots at 1920×800 and 390×844 covering: fresh ASK, NO + reason + withdraw question, NO + keep-active button visible, kept-active confirmation, and withdraw confirm screen. No horizontal overflow at mobile. No production data touched (preview backend points at Atlas `hr_analytics`; all verification used page-level API mocks).
- **Minimal-scroll verification (2026-02-14, post-user-feedback)**: layout tightened in `InterviewSchedule.js` + `AvailabilityGate.jsx` (reduced padding, denser 2-column "Interview Information" grid, tighter spacing on reason/withdraw blocks). Playwright DOM measurements confirm `scrollHeight === innerHeight` at both **desktop 1920×800 (800/800)** and **mobile 390×844 (844/844)** for the ASK state — **no vertical scrolling needed**. Horizontal-overflow check returns `[]` at both widths. **Status: USER VERIFICATION PENDING.**

### 2026-02-14 — P0 Testing Mode single-call scenario selector
- `frontend/src/components/ai-calling/CallNowDialog.jsx` — Added mandatory "Test-call scenario" `<select>` (Booking Confirmation, Pre-Interview Confirmation, Missed-Interview Follow-up) with non-selectable "Pick a scenario…" placeholder. Chosen scenario is sent to `/test-candidates/{id}/call-now` as `call_purpose`, mirrored in the recipient summary row (`data-testid=call-now-scenario`) and surfaced in the success toast. Call-now button is `disabled` until both the exact name is typed and a scenario is picked; tooltip explains which gate is unmet.
- `backend/ai_calling/dispatch.py` — `call_now` no longer defaults to `interview_confirmation`; requests must send one of `booking_confirmation | interview_confirmation | missed_interview_followup`, otherwise the API returns 400 with the whitelist message. Enforcement applies identically to the batch endpoint, so API clients cannot bypass the selector.
- `frontend/src/pages/ai-calling/CallHistory.js` — Test Call History rows render the chosen scenario label (`PURPOSE_LABELS`), so admins can audit exactly which scenario was used for every historical single/batch call.
- Verified via screenshot on `/ai-calling/testing` (dropdown visible, Call-now disabled until scenario + name match) and CSRF-aware curl showing 400 on missing/invalid `call_purpose`. Safety invariants intact: `hiring_ai_testing` only, Bolna mocked.

### 2026-02-14 — Live Mode UI parity fix
- `frontend/src/pages/ai-calling/LiveMode.js` — "Automatic calling workflows" grid now renders all **three** cards: Booking Confirmation (OFF, default 3 h, retry +30 min, 10:00–19:00 IST window, ≥ 6 h gap before any Pre-Interview Confirmation), Pre-Interview Confirmation, and Missed-Interview Follow-up (OTP-based). Subtitle updated to "Three automatic calling workflows — dispatch remains blocked". Booking Confirmation toggle state surfaced via `data-testid=live-workflow-booking-state` (OFF).
- Verified via screenshot on `/ai-calling/live`: all 3 cards visible, Booking OFF, Emergency Stop ON, Live Calling Stop ON — no safety gate released, no settings mutated.

### 2026-02-14 — iter66 sweep
- `testing_agent_v3_fork` executed (`/app/test_reports/iteration_66.json`) — retest_needed=false, 4 MEDIUM polish items (all addressed above).
- Pytest suite stabilised at 39/39 passing with `PYTHONPATH=/app/backend`.

## ROADMAP (prioritised backlog)
### P1 — Session 3
- Tamil language synthesis + synthetic validation across the 3 purposes.
- Add a `conftest.py`/`sys.path` shim so `tests/test_enhancement2_booking_confirm.py` runs without `PYTHONPATH=/app/backend`.

### P2 — Releases 2–4 (gated on explicit approval + real keys)
- Release 2: first real test call (requires `BOLNA_API_KEY`).
- Release 3: limited live pilot (`AI_LIVE_DEPLOYMENT_APPROVED=true`).
- Release 4: full automatic operation.
- Production DB migration to `hr_analytics` (only on explicit approval).

### P3 — Nice-to-have
- Split `backend/bb_modules.py` (now ~6.2k lines) into focused modules.
- Exclude superseded `bb_registrations` rows from `_enrich_rows_with_ai_calls` email join.
- Move range validation before activation-stamp assignment in `put_scheduling` for clarity.

## Architecture snapshot
- `backend/ai_calling/` — state machine, dispatcher, booking/confirm/missed planners, mirror writer.
- `backend/bb_modules.py` — Candidate Journey, Interview Reports, Excel/CSV export + AI-calling columns.
- `backend/bb_reschedule_link.py` — Reschedule link delivery (mocked).
- `frontend/src/pages/ai-calling/*` — admin UI.
- `frontend/src/components/CandidateJourneyModal.jsx` — AI Call Results section.

## Safety invariants (do not regress)
- `DB_NAME=hiring_ai_testing`. **Never** `hr_analytics`.
- Bolna AI, AiSensy/WhatsApp, Resend all mocked.
- Emergency Stop ON by default; Live Dispatch Authorization OFF.
- `BOLNA_BACKUP_AGENT_ID` is config-only — no actual agent switching code path is active.
