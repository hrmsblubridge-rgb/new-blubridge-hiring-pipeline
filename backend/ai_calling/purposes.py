"""Three calling purposes (Enhancement 2 added booking_confirmation): pre-interview confirmation, OTP-based missed-interview follow-up, and booking_confirmation.
Pure planning/eligibility + outcome handling + admin scheduling settings + review alerts.
Nothing here dials, edits bookings, or touches otp_verified (read-only source of truth)."""
from datetime import datetime, timedelta

from bson import ObjectId
from fastapi import APIRouter, Body, HTTPException, Request

from .booking_confirm import BOOKING, plan_booking_confirmation, should_skip_min_6h_gap  # noqa: F401
from .dryrun import IST, MIN_LEAD, STATUS_REASON, TRUTHY_OTP, _ceil5, booking_key, interview_at, into_window, plan_booking
import os
from .provider import _auth, _csrf
from .store import SETTINGS_ID, db, get_settings, normalize_phone, now_iso

purposes_router = APIRouter()
CONFIRM, MISSED = "interview_confirmation", "missed_interview_followup"
PURPOSES = (CONFIRM, MISSED, BOOKING)
MISSED_STATUSES = ("Interview Scheduled", "Missed")  # "Missed" is set automatically by bg_workers WORKER E
RETRY_OUTCOMES = ("no_answer", "busy")  # provider-confirmed, no conversation
MISSED_BLOCKING_ALERTS = ("RESCHEDULE_REQUEST_PENDING", "RUNNING_LATE", "CLAIMS_ATTENDED")
SCHEDULING_KEYS = {"confirm_enabled": bool, "confirm_offset_days": int, "missed_enabled": bool, "missed_offset_minutes": int,
                   "live_daily_call_limit": int, "live_pilot_daily_cap": int, "live_max_concurrent_calls": int,
                   "live_queue_capacity": int,
                   # Enhancement 2 Session 2 — booking_confirmation admin fields.
                   "booking_enabled": bool, "booking_target_hours": int, "booking_retry_minutes": int}


def otp_state(b: dict) -> str:
    """Read-only view of the employee OTP record. Only '' / False mean 'not verified' (owner decision)."""
    if "otp_verified" not in b:
        return "missing"
    v = b["otp_verified"]
    if v in TRUTHY_OTP or (isinstance(v, str) and v.strip().lower() in ("1", "1.0", "true", "yes")):
        return "verified"
    if v == "" or v is False:
        return "not_verified"
    return "unknown"


def purpose_cfg(s: dict, purpose: str) -> dict:
    if purpose == CONFIRM:
        return {**s, "enabled": s["confirm_enabled"], "live_call_hours_before": 24 * int(s["confirm_offset_days"]),
                "call_window_start": s["confirm_window_start"], "call_window_end": s["confirm_window_end"],
                "retry_gap": timedelta(minutes=int(s["confirm_retry_minutes"]))}
    return {**s, "enabled": s["missed_enabled"], "offset": timedelta(minutes=int(s["missed_offset_minutes"])),
            "call_window_start": s["missed_window_start"], "call_window_end": s["missed_window_end"],
            "retry_gap": timedelta(minutes=int(s["missed_retry_minutes"])),
            "followup_period": timedelta(hours=int(s["missed_followup_hours"]))}


def consent_covers(c: dict, purpose: str) -> bool:
    if (c or {}).get("status") != "granted":
        return False
    from .modes import scope_for
    return purpose in (c.get("scope") or scope_for(c.get("text_version")))


def missed_eligibility(b: dict) -> list:
    r = []
    if b.get("superseded") is True:
        r.append("SUPERSEDED")
    if b.get("isTest") is True:
        r.append("TEST_RECORD")
    st = b.get("status") or ""
    if st not in MISSED_STATUSES:
        r.append(STATUS_REASON.get(st, "UNKNOWN_STATUS") if st != "Missed" else "UNKNOWN_STATUS")
    elif b.get("is_shortlisted") is not True:
        r.append("NOT_SHORTLISTED")
    r += {"verified": ["ATTENDED_OTP_VERIFIED"], "unknown": ["ATTENDANCE_UNKNOWN"],
          "missing": ["OTP_FIELD_MISSING_DATA_ISSUE"]}.get(otp_state(b), [])
    if b.get("do_not_call") is True:
        r.append("DO_NOT_CALL")
    if not consent_covers(b.get("ai_call_consent"), MISSED):
        r.append("NO_FOLLOWUP_CONSENT")
    if b.get("blocked_alerts"):
        r += [f"ALERT_{a}" for a in b["blocked_alerts"]]
    try:
        normalize_phone(b.get("phone"))
    except ValueError:
        r.append("INVALID_PHONE")
    return r


def missed_deadline(iv: datetime, pc: dict) -> datetime:
    return iv + pc["offset"] + pc["followup_period"]


def plan_missed(b: dict, now: datetime, s: dict) -> dict:
    pc, now = purpose_cfg(s, MISSED), now.astimezone(IST)
    out = {"reasons": missed_eligibility(b), "interview_at": None, "target_at": None, "planned_at": None, "adjustments": [],
           "retry": {"eligible": True, "gap_minutes": int(pc["retry_gap"].total_seconds() // 60)}}
    try:
        iv = interview_at(b)
    except ValueError:
        iv = None
    if iv is None:
        out.update(status="SKIPPED", reasons=out["reasons"] + ["NO_SCHEDULE"])
        return out
    target = iv + pc["offset"]
    out.update(interview_at=iv.isoformat(), target_at=target.isoformat())
    if out["reasons"]:
        out["status"] = "SKIPPED"
        return out
    planned = target if target > now else _ceil5(now)
    moved = into_window(planned, pc)
    if moved != planned:
        out["adjustments"].append(f"Moved into the {pc['call_window_start']}–{pc['call_window_end']} IST window")
    if moved > missed_deadline(iv, pc):
        out.update(status="NEEDS_REVIEW", reasons=["FOLLOWUP_PERIOD_EXPIRED"])
        return out
    out.update(status="SCHEDULED", planned_at=moved.isoformat())
    return out


def plan_for(purpose: str, b: dict, now: datetime, s: dict) -> dict:
    if purpose == MISSED:
        return plan_missed(b, now, s)
    if purpose == BOOKING:
        b3 = {**b, "ai_call_consent": {**(b.get("ai_call_consent") or {}),
                                        "status": "granted" if consent_covers(b.get("ai_call_consent"), BOOKING) else
                                        (b.get("ai_call_consent") or {}).get("status")}}
        return plan_booking_confirmation(b3, now, s)
    b2 = {**b, "ai_call_consent": {**(b.get("ai_call_consent") or {}),
                                   "status": "granted" if consent_covers(b.get("ai_call_consent"), CONFIRM) else
                                   (b.get("ai_call_consent") or {}).get("status")}}
    if "WRONG_NUMBER" in (b.get("blocked_alerts") or []):
        b2["do_not_call"] = True
    return plan_booking(b2, now, purpose_cfg(s, CONFIRM))


def retry_at(purpose: str, job: dict, now: datetime, s: dict):
    """One retry: confirmation +60 min (≥2 h before interview); follow-up +30 min (inside the follow-up period)."""
    pc = purpose_cfg(s, purpose)
    at = into_window(now + pc["retry_gap"], pc)
    iv = datetime.fromisoformat(job["interview_at"])
    if purpose == CONFIRM:
        return at if iv - at >= MIN_LEAD else None
    return at if at <= missed_deadline(iv, pc) else None


# ---------- outcomes (used by the mock worker now and by real result handling later) ----------

def outcome_of(purpose: str, ex: dict) -> str:
    att, mo = ex.get("attendance_status") or "UNKNOWN", ex.get("missed_interview_outcome")
    if "DO_NOT_CALL" in (att, mo):
        return "DO_NOT_CALL"  # opt-out wins over DECLINED or anything else
    if att == "WRONG_NUMBER" or mo == "WRONG_NUMBER":
        return "WRONG_NUMBER"
    if purpose == MISSED and mo:
        return mo
    if ex.get("wants_reschedule") is True or att == "RESCHEDULE_REQUESTED":
        return "RESCHEDULE_REQUESTED"
    return att


async def raise_alert(reg_id: str, bkey: str, purpose: str, kind: str, detail: dict) -> None:
    await db().ai_call_alerts.update_one(
        {"registration_id": reg_id, "booking_key": bkey, "type": kind, "status": "open"},
        {"$setOnInsert": {"registration_id": reg_id, "booking_key": bkey, "type": kind, "status": "open", "purpose": purpose,
                          "detail": detail, "created_at": now_iso(), "needs_hr_review": True}}, upsert=True)


async def cancel_missed(reg_id: str, bkey: str, reason: str) -> int:
    return (await db().ai_live_mock_jobs.update_many(
        {"registration_id": reg_id, "booking_key": bkey, "purpose": MISSED, "status": {"$in": ["planned", "claimed", "needs_review"]}},
        {"$set": {"status": "cancelled", "reasons": [reason], "cancelled_at": now_iso()}})).modified_count


async def apply_outcome(job: dict, ex: dict) -> str:
    """Records alerts/suppression for one finished call. Never edits the booking or otp_verified.

    Enhancement 2 Session 2:
      • Mirrors the verified result to bb_registrations.ai_call_results.<purpose>
        (append-only history + latest snapshot, scoped to the exact
        schedule_version this job was dispatched against).
      • For a verified RESCHEDULE_REQUESTED outcome from ANY of the three AI
        purposes, invokes deliver_reschedule_link() so the candidate
        automatically receives the standard scheduling link. Enhancement-1
        eligibility and idempotency are honoured by the delivery helper.
    """
    rid, bkey, purpose = job["registration_id"], job["booking_key"], job.get("purpose", CONFIRM)
    out, real = outcome_of(purpose, ex), not rid.startswith("fixture:")
    if out == "DO_NOT_CALL" and real:
        from .consent import apply_ai_opt_out
        await apply_ai_opt_out(rid)
    elif out == "WRONG_NUMBER":
        await raise_alert(rid, "*", purpose, "WRONG_NUMBER", {})
        await db().ai_live_mock_jobs.update_many({"registration_id": rid, "status": {"$in": ["planned", "claimed"]}},
                                                 {"$set": {"status": "cancelled", "reasons": ["WRONG_NUMBER"]}})
    elif out == "RESCHEDULE_REQUESTED":
        await raise_alert(rid, bkey, purpose, "RESCHEDULE_REQUEST_PENDING",
                          {"preferred_reschedule_datetime": ex.get("preferred_reschedule_datetime"), "reason": ex.get("missed_interview_reason") or ex.get("rejection_reason")})
        await cancel_missed(rid, bkey, "RESCHEDULE_REQUEST_PENDING")
    elif out == "RUNNING_LATE":
        await raise_alert(rid, bkey, purpose, "RUNNING_LATE", {"expected_arrival_time": ex.get("expected_arrival_time")})
        await cancel_missed(rid, bkey, "RUNNING_LATE")
    elif out == "CLAIMS_ATTENDED":
        await raise_alert(rid, bkey, purpose, "CLAIMS_ATTENDED", {"attendance_dispute": True})
        await cancel_missed(rid, bkey, "CLAIMS_ATTENDED")

    # --- Enhancement 2 §15A/§20A — verified-outcome pipeline mirror ----
    # Only run for real (non-fixture) registrations with a verified timestamp
    # on the mock_call. The caller (mock_tick) stamps `verified_at` ONLY when
    # the call completed (status == completed). No-answer / busy / timeout /
    # failed never reach this branch, so the mirror is never written for
    # unverified calls.
    verified_at = (job.get("mock_call") or {}).get("verified_at") or ex.get("_verified_at")
    if real and verified_at:
        try:
            await _mirror_latest_result(rid, bkey, purpose, out, ex, job, verified_at)
        except Exception as _e:  # pragma: no cover
            import logging
            logging.getLogger("ai_calling.apply_outcome").warning(
                f"mirror_latest_result failed: {_e}")

        # Automatic rescheduling-link delivery — any of the three purposes.
        if out == "RESCHEDULE_REQUESTED":
            try:
                from bb_reschedule_link import deliver_reschedule_link
                sv = job.get("schedule_version") or 1
                exec_id = ((job.get("mock_call") or {}).get("execution_id")
                           or f"job:{str(job.get('_id') or '')}")
                await deliver_reschedule_link(rid, sv, exec_id, purpose)
            except Exception as _e:  # pragma: no cover
                import logging
                logging.getLogger("ai_calling.apply_outcome").warning(
                    f"deliver_reschedule_link failed: {_e}")
    return out


# ---- Enhancement 2 Session 2: separate verified result mirrors --------------
# Concise mapped reasons for the missed-interview outcome. Short enough for
# Excel/CSV exports; never raw transcript text.
MISSED_REASON_MAP = {
    "MISSED_CONFIRMED": "Missed — confirmed",
    "RUNNING_LATE": "Running late",
    "CLAIMS_ATTENDED": "Claims attended (dispute)",
    "RESCHEDULE_REQUESTED": "Requested reschedule",
    "DECLINED": "Declined",
    "DO_NOT_CALL": "Do not call",
    "WRONG_NUMBER": "Wrong number",
    "UNKNOWN": "Unknown",
}


def _needs_hr_review(purpose: str, outcome: str) -> bool:
    """Outcomes that require human follow-up regardless of auto-actions."""
    if outcome in ("WRONG_NUMBER", "RUNNING_LATE", "CLAIMS_ATTENDED", "DO_NOT_CALL"):
        return True
    if purpose == MISSED and outcome in ("MISSED_CONFIRMED", "RESCHEDULE_REQUESTED", "DECLINED", "UNKNOWN"):
        return True
    return False


async def _mirror_latest_result(reg_id: str, booking_key_val: str, purpose: str,
                                outcome: str, extracted: dict, job: dict,
                                verified_at: str) -> None:
    """Write the verified AI-call result into bb_registrations.ai_call_results.<purpose>.

    Preserves full history (append-only) and stamps the latest-verified
    snapshot tagged with schedule_version + OTP attendance so Candidate
    Journey, Interview Schedule table and Excel/CSV exports can render the
    three purposes independently.
    """
    from bson import ObjectId as _OID
    try:
        oid = _OID(reg_id) if _OID.is_valid(reg_id) else None
    except Exception:
        oid = None
    if oid is None:
        return
    reg = await db().bb_registrations.find_one({"_id": oid}, {"otp_verified": 1, "schedule_version": 1})
    if reg is None:
        return

    snapshot = {
        "outcome": outcome,
        "verified_at": verified_at,
        "purpose": purpose,
        "booking_key": booking_key_val,
        "schedule_version": job.get("schedule_version") or reg.get("schedule_version") or 1,
        "attempt": int(job.get("attempt") or 1),
        "execution_id": (job.get("mock_call") or {}).get("execution_id"),
        "attendance_at_call": "verified" if reg.get("otp_verified") in TRUTHY_OTP else (
            "not_verified" if reg.get("otp_verified") in ("", False) else "unknown"),
        "concise_reason": MISSED_REASON_MAP.get(outcome, ""),
        "preferred_reschedule_datetime": extracted.get("preferred_reschedule_datetime"),
        "needs_hr_review": _needs_hr_review(purpose, outcome),
    }
    history_entry = {**snapshot, "recorded_at": now_iso()}
    await db().bb_registrations.update_one(
        {"_id": oid},
        {
            "$set": {f"ai_call_results.{purpose}.latest": snapshot,
                     f"ai_call_results.{purpose}.updated_at": now_iso()},
            "$push": {f"ai_call_results.{purpose}.history": {
                "$each": [history_entry], "$slice": -50}},
        },
    )


async def open_alerts(reg_ids: list) -> dict:
    """{registration_id: {booking_key|'*': [types]}} for open alerts that block calling."""
    out = {}
    async for a in db().ai_call_alerts.find({"registration_id": {"$in": reg_ids}, "status": "open"}):
        out.setdefault(a["registration_id"], {}).setdefault(a["booking_key"], []).append(a["type"])
    return out


def blocked_for(alerts: dict, bkey: str, purpose: str) -> list:
    types = list(alerts.get("*", []))
    if purpose == MISSED:
        types += [t for t in alerts.get(bkey, []) if t in MISSED_BLOCKING_ALERTS]
    return types


# ---------- admin routes ----------

@purposes_router.get("/scheduling")
async def get_scheduling(request: Request):
    await _auth(request)
    s = await get_settings()
    backup_agent_id = (os.environ.get("BOLNA_BACKUP_AGENT_ID") or "").strip()
    return {"interview_confirmation": {"enabled": s["confirm_enabled"], "offset_days": s["confirm_offset_days"], "offset_range": [1, 7],
                                       "max_retries": 1, "retry_minutes": s["confirm_retry_minutes"],
                                       "window": [s["confirm_window_start"], s["confirm_window_end"]]},
            "missed_interview_followup": {"enabled": s["missed_enabled"], "offset_minutes": s["missed_offset_minutes"],
                                          "offset_range_minutes": [60, 10080], "max_retries": 1, "retry_minutes": s["missed_retry_minutes"],
                                          "window": [s["missed_window_start"], s["missed_window_end"]],
                                          "followup_period_hours": s["missed_followup_hours"],
                                          "attendance_source": "Existing employee OTP verification (read-only)"},
            "booking_confirmation": {"enabled": bool(s.get("booking_enabled", False)),
                                     "target_hours": int(s.get("booking_target_hours", 3)),
                                     "target_range_hours": [1, 24],
                                     "max_retries": 1,
                                     "retry_minutes": int(s.get("booking_retry_minutes", 30)),
                                     "retry_range_minutes": [15, 240],
                                     "window": [s.get("booking_window_start", "10:00"),
                                                s.get("booking_window_end", "19:00")],
                                     "activated_at": s.get("booking_activated_at"),
                                     "note": "Independent ON/OFF. Dispatch still gated by Emergency Stop, Live Calling Stop and AI_CALLING_ENABLED."},
            "bolna_backup_agent": {"configured": bool(backup_agent_id),
                                    "agent_id_masked": (backup_agent_id[:4] + "…" + backup_agent_id[-4:]) if len(backup_agent_id) >= 8 else ("configured" if backup_agent_id else ""),
                                    "live": False,
                                    "note": "Backup agent is validated and disabled. Switching agents or dispatching through it is not permitted in this phase."},
            "capacity": {k: s[k] for k in ("live_daily_call_limit", "live_pilot_daily_cap", "live_max_concurrent_calls", "live_queue_capacity")}}


@purposes_router.put("/scheduling")
async def put_scheduling(request: Request, body: dict = Body(...)):  # noqa: B008
    user = await _auth(request)
    _csrf(request)
    upd = {}
    for k, v in body.items():
        if k not in SCHEDULING_KEYS:
            raise HTTPException(400, f"Unknown setting {k}")
        if not isinstance(v, SCHEDULING_KEYS[k]) or (SCHEDULING_KEYS[k] is int and isinstance(v, bool)):
            raise HTTPException(400, f"{k} has the wrong type")
        upd[k] = v
    limits = {"confirm_offset_days": (1, 7), "missed_offset_minutes": (60, 10080),
              "booking_target_hours": (1, 24), "booking_retry_minutes": (15, 240),
              "live_daily_call_limit": (0, 100000),
              "live_pilot_daily_cap": (0, 100000), "live_max_concurrent_calls": (1, 50), "live_queue_capacity": (1, 1000)}
    # Feature-activation stamp: flipping booking_enabled ON writes an immutable
    # activation timestamp (spec §21). Flipping OFF does not clear it.
    if upd.get("booking_enabled") is True:
        before_s = await get_settings()
        if not before_s.get("booking_activated_at"):
            upd["booking_activated_at"] = now_iso()
    for k, (lo, hi) in limits.items():
        if k in upd and not lo <= upd[k] <= hi:
            raise HTTPException(400, f"{k} must be {lo}–{hi}")
    if not upd:
        raise HTTPException(400, "Nothing to update")
    before = await get_settings()
    await db().ai_call_settings.update_one({"_id": SETTINGS_ID}, {"$set": {**upd, "updated_at": now_iso()}})
    await db().ai_call_audit.insert_one({"action": "scheduling_updated", "by": user, "at": now_iso(),
                                         "detail": {k: {"from": before.get(k), "to": v} for k, v in upd.items()}})
    from .live_prep import replan_pending
    recalculated = await replan_pending(user)  # only planned/needs-review jobs; finished calls are never redialled
    return {**await get_scheduling(request), "recalculated": recalculated}


@purposes_router.get("/alerts")
async def list_alerts(request: Request, registration_id: str = None, status: str = "open"):
    await _auth(request)
    q = {"status": status} if status in ("open", "resolved") else {}
    if registration_id:
        q["registration_id"] = registration_id
    rows = await db().ai_call_alerts.find(q).sort("created_at", -1).to_list(500)
    return {"alerts": [{**{k: v for k, v in a.items() if k != "_id"}, "id": str(a["_id"])} for a in rows]}


@purposes_router.post("/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: str, request: Request, body: dict = Body(default={})):  # noqa: B008
    user = await _auth(request)
    _csrf(request)
    res = await db().ai_call_alerts.update_one({"_id": ObjectId(alert_id), "status": "open"} if ObjectId.is_valid(alert_id) else {"_id": None},
                                               {"$set": {"status": "resolved", "resolved_by": user, "resolved_at": now_iso(),
                                                         "resolution": str(body.get("note") or "")[:300]}})
    if not res.modified_count:
        raise HTTPException(404, "Open alert not found")
    await db().ai_call_audit.insert_one({"action": "alert_resolved", "by": user, "alert_id": alert_id, "at": now_iso()})
    return {"resolved": True}


__all__ = ["CONFIRM", "MISSED", "PURPOSES", "booking_key"]
