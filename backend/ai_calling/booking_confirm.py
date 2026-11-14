"""Enhancement 2 — booking_confirmation purpose.

Adds a third AI calling purpose that fires shortly after a successful interview
booking (or an actual changed-slot reschedule). Keeps the existing
interview_confirmation and missed_interview_followup workflows intact.

Design invariants:
  • Default target = ASAP within the calling window; 3h target metric only.
  • One retry only (30 min default) for no-answer/busy.
  • Refuses to dispatch when latest_availability is NO or withdrawn.
  • Feature-activation boundary (`booking_activated_at`) prevents retroactive
    sweeps of historical bookings.
  • Idempotent by (registration_id, "booking_confirmation", schedule_version).
  • Six-hour separation rule from a completed booking_confirmation before a
    subsequent interview_confirmation runs — enforced in purposes.plan_for().
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .dryrun import IST, MIN_LEAD, _ceil5, eligibility, interview_at, into_window
from .store import db, now_iso


BOOKING = "booking_confirmation"


def booking_cfg(s: dict) -> dict:
    """Project ai_call_settings into the shape plan_* expects."""
    return {
        **s,
        "enabled": bool(s.get("booking_enabled", False)),
        "target_hours": int(s.get("booking_target_hours", 3)),
        "call_window_start": s.get("booking_window_start", "10:00"),
        "call_window_end": s.get("booking_window_end", "19:00"),
        "retry_gap": timedelta(minutes=int(s.get("booking_retry_minutes", 30))),
        "activated_at": s.get("booking_activated_at"),
    }


def _booked_at_iso(b: dict) -> Optional[str]:
    return (b.get("schedule_booked_at")
            or b.get("latest_booking_at")
            or b.get("schedule_last_updated"))


def booking_eligibility(b: dict) -> list:
    """Booking-confirmation exclusions. Reuses the shared eligibility() that
    already handles WITHDRAWN + LATEST_AVAILABILITY_NO + consent + do_not_call."""
    r = list(eligibility(b))
    # Feature activation boundary — spec §21.
    activ = b.get("_booking_activated_at")
    booked_at = _booked_at_iso(b)
    if activ and booked_at and booked_at < activ:
        r.append("PRE_ACTIVATION_BOOKING")
    return r


def plan_booking_confirmation(b: dict, now: datetime, s: dict) -> dict:
    """Return a scheduling plan for a single bb_registrations row."""
    pc = booking_cfg(s)
    now = now.astimezone(IST)
    out = {
        "purpose": BOOKING,
        "reasons": [], "interview_at": None, "target_at": None,
        "planned_at": None, "adjustments": [],
        "retry": {"eligible": True, "gap_minutes": int(pc["retry_gap"].total_seconds() // 60)},
    }
    # Booking eligibility depends on knowing whether this record is a
    # post-activation booking, so plug in the activation boundary.
    b2 = {**b, "_booking_activated_at": pc.get("activated_at")}
    out["reasons"] = booking_eligibility(b2)

    try:
        iv = interview_at(b)
    except ValueError:
        iv = None
    if iv is None:
        out.update(status="SKIPPED", reasons=out["reasons"] + ["NO_SCHEDULE"])
        return out
    out["interview_at"] = iv.isoformat()
    # Target: 3h after booking, but ASAP within calling window — whichever is sooner.
    booked_iso = _booked_at_iso(b)
    try:
        booked_dt = datetime.fromisoformat(booked_iso).astimezone(IST) if booked_iso else now
    except (TypeError, ValueError):
        booked_dt = now
    target = booked_dt + timedelta(hours=pc["target_hours"])
    out["target_at"] = target.isoformat()

    if not pc["enabled"]:
        out.update(status="SKIPPED", reasons=out["reasons"] + ["DISABLED"])
        return out
    if out["reasons"]:
        out["status"] = "SKIPPED"
        return out

    # Earliest permitted dispatch time: next 5-minute boundary inside the window.
    planned = _ceil5(now)
    moved = into_window(planned, pc)
    if moved != planned:
        out["adjustments"].append(
            f"Moved into the {pc['call_window_start']}–{pc['call_window_end']} IST window"
        )

    # Must complete initial dial before the interview start, with the standard
    # minimum lead applied (spec §6 — do not dispatch after interview start).
    if iv - moved < MIN_LEAD:
        out.update(status="SKIPPED", reasons=["SKIPPED_NO_VALID_CALL_WINDOW"])
        return out
    out.update(status="SCHEDULED", planned_at=moved.isoformat())
    return out


def retry_at(job: dict, now: datetime, s: dict):
    """One retry; must still be before the interview start with the 2-h lead."""
    pc = booking_cfg(s)
    at = into_window(now + pc["retry_gap"], pc)
    iv = datetime.fromisoformat(job["interview_at"])
    return at if iv - at >= MIN_LEAD else None


async def should_skip_min_6h_gap(reg_id: str, booking_key: str, due_at: datetime) -> bool:
    """Spec §11 — skip interview_confirmation when it is <6 h after a verified
    booking_confirmation conversation on the SAME schedule version."""
    coll = db().ai_calls
    last = await coll.find_one(
        {"registration_id": reg_id, "booking_key": booking_key,
         "purpose": BOOKING, "answered": True, "verified_at": {"$ne": None}},
        sort=[("verified_at", -1)],
    )
    if not last:
        return False
    try:
        last_t = datetime.fromisoformat(last["verified_at"])
    except (TypeError, ValueError):
        return False
    return (due_at.astimezone(IST) - last_t.astimezone(IST)) < timedelta(hours=6)


async def enqueue_on_booking(reg: dict) -> dict:
    """Idempotent enqueue hook called from the public schedule endpoint.
    Does NOT dispatch anything itself — just writes a mock-scheduler job row
    scoped to (registration_id, purpose, booking_key) which is picked up by
    the existing live scheduler. Safe to call multiple times.

    Returns `{enqueued: bool, reasons: [...]}`.
    """
    from .dryrun import booking_key
    from .store import get_settings
    s = await get_settings()
    pc = booking_cfg(s)
    # Feature gate.
    if not pc["enabled"] or not pc.get("activated_at"):
        return {"enqueued": False, "reasons": ["DISABLED"]}
    # Activation boundary enforced centrally in plan_booking_confirmation; here
    # we just skip enqueue when the booking happened before activation.
    booked_at = _booked_at_iso(reg)
    if booked_at and booked_at < pc["activated_at"]:
        return {"enqueued": False, "reasons": ["PRE_ACTIVATION_BOOKING"]}

    bkey = booking_key(reg)
    plan = plan_booking_confirmation(reg, datetime.now(IST), s)
    if plan["status"] != "SCHEDULED":
        return {"enqueued": False, "reasons": plan["reasons"] or [plan["status"]]}

    # Idempotent upsert by (registration_id, booking_key, purpose, attempt).
    row = {
        "registration_id": str(reg["_id"]),
        "booking_key": bkey,
        "purpose": BOOKING,
        "attempt": 1,
        "planned_at": plan["planned_at"],
        "target_at": plan["target_at"],
        "interview_at": plan["interview_at"],
        "status": "planned",
        "schedule_version": reg.get("schedule_version") or 1,
        "created_at": now_iso(),
    }
    await db().ai_live_mock_jobs.update_one(
        {"registration_id": row["registration_id"], "booking_key": bkey,
         "purpose": BOOKING, "attempt": 1},
        {"$setOnInsert": row}, upsert=True,
    )
    return {"enqueued": True, "reasons": []}


async def cancel_obsolete(reg_id: str, keep_booking_key: str) -> int:
    """On actual changed-slot reschedule: cancel booking_confirmation jobs for
    older booking_keys. Spec §13."""
    res = await db().ai_live_mock_jobs.update_many(
        {"registration_id": reg_id, "purpose": BOOKING,
         "booking_key": {"$ne": keep_booking_key},
         "status": {"$in": ["planned", "claimed", "needs_review"]}},
        {"$set": {"status": "cancelled", "reasons": ["CANCELLED_OBSOLETE_VERSION"],
                  "cancelled_at": now_iso()}},
    )
    return res.modified_count
