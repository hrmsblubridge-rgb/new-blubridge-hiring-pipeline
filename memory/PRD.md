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
### 2026-02-14 — iter66 polish
- `BatchBody.call_purpose` now `Optional[str] = None` so a missing scenario returns 400 (with the whitelist message) instead of Pydantic 422.
- `ai_calling/purposes.py` — added `CONFIRM_REASON_MAP` + `_concise_reason(purpose, outcome)` so booking/confirmation outcomes like `CONFIRMED`, `RESCHEDULE_REQUESTED`, `DECLINED` get a concise label instead of an empty export cell.
- `bb_modules._format_iso_dt` — appends `UTC` suffix and the docstring no longer claims IST, so Verified-At / Delivered-At columns aren't confused for local time.
- `ai_calling/dispatch.py` single test-call now accepts `booking_confirmation`, matching the 3-scenario batch selector.
- `CandidateJourneyModal.jsx` — AI Call Results section now always renders; when nothing is verified yet, each row shows "No verified call yet" placeholders.
- `SchedulingPanel.jsx` — "Activated YYYY-MM-DD" green stamp only shows when the toggle is ON. When OFF but previously activated, a neutral grey "First activated … (stamp is immutable; toggle currently OFF)" line renders under `data-testid=booking-activated-at-off`.
- `BatchPanel.jsx` — batch rows now show "scenario: …" (`data-testid=batch-row-<id>-purpose`).
- `/app/memory/test_credentials.md` corrected (password is `QAZwsx##123`, not `Admin User`).

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
