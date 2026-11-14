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
