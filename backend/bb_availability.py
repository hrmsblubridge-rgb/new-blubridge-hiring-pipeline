"""Enhancement 1 — Chennai in-person availability, withdrawal and reactivation.

Additive module. Writes ONLY:
  • new optional fields on `bb_registrations` (existing booked rows keep them null)
  • `bb_availability_responses`        (append-only history)
  • `bb_availability_question_config`  (one `_id="current"` doc)
  • `bb_availability_question_drafts`  (one per admin username)
  • `bb_availability_question_versions`
  • `bb_availability_question_audit`

Never deletes, never backfills, never touches OTP / shortlisting / bookings.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator


# ---- router handles ----
avail_pub_router = APIRouter(prefix="/api/pub")
avail_admin_router = APIRouter(prefix="/api/bb")

_db = None
_auth_fn = None


def init_availability(database, auth_fn):
    """Called from server.py startup."""
    global _db, _auth_fn
    _db = database
    _auth_fn = auth_fn


async def _require_auth(request: Request):
    if _auth_fn is None:
        raise HTTPException(status_code=500, detail="Availability module not initialized")
    return await _auth_fn(request)


# =========================================================================
# Fixed semantic reason IDs. Admins may rename labels, but these IDs are
# frozen so historical responses survive every rename.
# =========================================================================
RESERVED_REASON_IDS = {
    "cannot_travel",
    "distance",
    "virtual_preferred",
    "work_commit",
    "timing",
    "not_interested",
    "other",
}

WITHDRAW_REASON_IDS = {"candidate_withdrew_after_no"}
REACTIVATION_RESPONSE_IDS = {"reactivate_yes", "reactivate_no", "reactivate_other"}

OTHER_TEXT_MAX = 50
REACT_OTHER_TEXT_MAX = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =========================================================================
# Default admin-editable configuration
# =========================================================================
DEFAULT_CONFIG: dict = {
    "main_question": (
        "Are you available to attend an in-person interview at the "
        "BluBridge Technologies office in Chennai?"
    ),
    "yes_label": "YES — I can attend an in-person interview in Chennai.",
    "no_label": "NO — I cannot attend an in-person interview in Chennai.",
    "reason_question": (
        "Why are you unable to attend an in-person interview at our "
        "Chennai office?"
    ),
    "reason_options": [
        {"id": "cannot_travel",     "label": "Unable to travel to Chennai.",                   "active": True},
        {"id": "distance",          "label": "Distance or location difficulty.",               "active": True},
        {"id": "virtual_preferred", "label": "Looking for a virtual or online interview.",     "active": True},
        {"id": "work_commit",       "label": "Work or personal commitments.",                  "active": True},
        {"id": "timing",            "label": "Interview timing is unsuitable.",                "active": True},
        {"id": "not_interested",    "label": "Not interested in this opportunity.",            "active": True},
        {"id": "other",             "label": "Other.",                                         "active": True},
    ],
    "other_label": "Please tell us why (max 50 characters).",
    "explanation_text": (
        "BluBridge Technologies currently conducts interviews only in person "
        "at our Chennai office. Online or virtual interviews are not "
        "available for this hiring process."
    ),
    "withdraw_question": "Would you like to withdraw your application from BluBridge Technologies?",
    "withdraw_yes_label": "YES — Withdraw my application.",
    "withdraw_no_label": "NO — Keep my application active.",
    "withdraw_explanation": (
        "If you confirm withdrawal, you will no longer receive interview "
        "scheduling reminders for this application."
    ),
    "withdraw_confirm_check": (
        "I understand that withdrawing my application will stop further "
        "interview scheduling communications for this application."
    ),
    "withdraw_button_label": "Confirm Withdrawal",
    "withdraw_thankyou": (
        "Your application has been withdrawn from BluBridge Technologies. "
        "Thank you for your interest."
    ),
    "reactivation_question": (
        "You previously withdrew your application from BluBridge Technologies. "
        "Would you like to reactivate your application and proceed with an "
        "in-person interview at our Chennai office?"
    ),
    "reactivation_yes_label":   "YES — Reactivate my application.",
    "reactivation_no_label":    "NO — Keep my application withdrawn.",
    "reactivation_other_label": "Other — I have a question, doubt or requirement.",
    "reactivation_other_placeholder": "Tell us your question, doubt or requirement.",
    "reactivation_no_confirmation":    "Your application remains withdrawn. Thank you.",
    "reactivation_other_confirmation": "Thank you — our HR team will get back to you.",
    "booked_no_notice": (
        "Your existing interview booking is preserved. Because you selected "
        "NO, you cannot pick a new slot while your latest response stands. "
        "Our HR team has been notified for review."
    ),
}


# =========================================================================
# Index bootstrap
# =========================================================================
async def ensure_availability_indexes():
    if _db is None:
        return
    try:
        await _db.bb_availability_responses.create_index(
            [("registration_id", 1), ("submitted_at", -1)],
            name="reg_submitted_desc",
        )
        await _db.bb_availability_responses.create_index(
            [("schedule_token", 1), ("submitted_at", -1)],
            name="token_submitted_desc",
        )
        await _db.bb_availability_responses.create_index(
            [("submission_nonce", 1)],
            name="submission_nonce_unique",
            unique=True,
            sparse=True,
        )
        await _db.bb_availability_question_versions.create_index(
            [("version", -1)], name="version_desc", unique=True,
        )
        await _db.bb_availability_question_audit.create_index(
            [("at", -1)], name="audit_at_desc",
        )
        await _db.bb_availability_question_drafts.create_index(
            [("username", 1)], name="username_unique", unique=True,
        )
    except Exception as e:
        import logging
        logging.getLogger("bb_availability").warning(f"index creation skipped: {e}")


# =========================================================================
# Config read / write helpers
# =========================================================================
async def _get_current_config() -> dict:
    """Return the currently-published config, seeding the default on first call."""
    doc = await _db.bb_availability_question_config.find_one({"_id": "current"})
    if doc:
        doc.pop("_id", None)
        return doc
    # Seed version 1 using $setOnInsert so concurrent callers never race-overwrite.
    now = _now_iso()
    seed = {
        **DEFAULT_CONFIG,
        "version": 1,
        "published_at": now,
        "published_by": "system",
    }
    await _db.bb_availability_question_config.update_one(
        {"_id": "current"},
        {"$setOnInsert": seed},
        upsert=True,
    )
    await _db.bb_availability_question_versions.update_one(
        {"version": 1},
        {"$setOnInsert": {**seed, "snapshot_at": now}},
        upsert=True,
    )
    return seed


def _reason_label_for_id(cfg: dict, reason_id: str) -> Optional[str]:
    for opt in cfg.get("reason_options") or []:
        if opt.get("id") == reason_id and opt.get("active", True):
            return opt.get("label") or ""
    return None


def _active_reason_ids(cfg: dict) -> set:
    return {o["id"] for o in (cfg.get("reason_options") or []) if o.get("active", True)}


# =========================================================================
# Registration record helpers
# =========================================================================
async def _reg_by_token(token: str) -> Optional[dict]:
    return await _db.bb_registrations.find_one({"schedule_token": token})


def _booking_snapshot(reg: dict) -> dict:
    return {
        "status": reg.get("status"),
        "schedule_date": reg.get("schedule_date"),
        "schedule_time": reg.get("schedule_time"),
        "reschedule_count": reg.get("reschedule_count"),
        "otp_verified": bool(reg.get("otp_verified")),
    }


def _registration_summary(reg: dict, cfg: dict) -> dict:
    """What the public GET endpoint returns about the candidate's state."""
    latest = reg.get("chennai_availability_latest", None)
    withdrawn = bool(reg.get("withdrawn_by_candidate"))
    has_booking = bool(reg.get("schedule_date"))
    return {
        "name": reg.get("full_name") or "",
        "email": reg.get("email") or "",
        "phone": reg.get("phone") or "",
        "is_shortlisted": bool(reg.get("is_shortlisted")),
        "otp_verified": bool(reg.get("otp_verified")),
        "has_booking": has_booking,
        "booking": _booking_snapshot(reg) if has_booking else None,
        "latest_availability": latest,  # true | false | null
        "latest_availability_at": reg.get("chennai_availability_latest_at"),
        "latest_reason_id": reg.get("chennai_availability_reason_id"),
        "latest_reason_text": reg.get("chennai_availability_reason_text"),
        "latest_other_text": reg.get("chennai_availability_other_text"),
        "withdrawn": withdrawn,
        "withdrawn_at": reg.get("withdrawn_at"),
        "withdrawal_reason_id": reg.get("withdrawal_reason_id"),
        "needs_hr_review": bool(reg.get("needs_hr_review")),
        "reactivation_count": int(reg.get("reactivation_count") or 0),
        "reactivated_at": reg.get("reactivated_at"),
        "question_version": cfg.get("version"),
        "superseded": bool(reg.get("superseded")),
    }


async def _append_response(reg: dict, response: dict) -> dict:
    """Idempotent insert into history by `submission_nonce`.

    Returns the stored document (either the newly inserted one or the existing
    one if the nonce already exists). Also raises 409 if the same nonce is
    replayed with a materially different payload.
    """
    nonce = response.get("submission_nonce")
    registration_id = response.get("registration_id")
    if nonce:
        existing = await _db.bb_availability_responses.find_one(
            {"submission_nonce": nonce}, {"_id": 0}
        )
        if existing:
            # Nonce reuse with a different payload is a client bug — reject.
            differ_keys = ("registration_id", "response_type", "availability",
                           "reason_id", "other_text", "withdraw_decision",
                           "reactivation_choice")
            for k in differ_keys:
                if response.get(k) != existing.get(k):
                    raise HTTPException(
                        status_code=409,
                        detail="A different response was already recorded for this submission nonce.",
                    )
            return existing
    try:
        await _db.bb_availability_responses.insert_one(response)
        return response
    except Exception as e:
        # Race on unique index — re-read and return original.
        import logging
        logging.getLogger("bb_availability").info(f"history insert race: {e}")
        if nonce:
            existing = await _db.bb_availability_responses.find_one(
                {"submission_nonce": nonce}, {"_id": 0}
            )
            if existing:
                return existing
        return response


async def _already_submitted(nonce: Optional[str]) -> Optional[dict]:
    """Return the prior row for this nonce if it exists, else None."""
    if not nonce:
        return None
    return await _db.bb_availability_responses.find_one(
        {"submission_nonce": nonce}, {"_id": 0}
    )


# =========================================================================
# PUBLIC (token) ENDPOINTS
# =========================================================================
@avail_pub_router.get("/availability/{token}")
async def get_availability(token: str):
    """Return the currently-published question config + the candidate's state."""
    reg = await _reg_by_token(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Invalid or expired link")
    cfg = await _get_current_config()
    return {
        "config": cfg,
        "candidate": _registration_summary(reg, cfg),
    }


class AvailabilityBody(BaseModel):
    availability: bool                      # True = YES, False = NO
    reason_id: Optional[str] = None         # required when availability=False
    other_text: Optional[str] = None        # required when reason_id="other"
    withdraw_decision: Optional[bool] = None  # used when NO + user chose to withdraw (not yet confirmed)
    submission_nonce: Optional[str] = None  # for idempotency across retries

    @field_validator("other_text")
    @classmethod
    def _clip(cls, v):
        if v is None:
            return v
        v = v.strip()
        if len(v) > OTHER_TEXT_MAX:
            raise ValueError(f"other_text may be at most {OTHER_TEXT_MAX} characters")
        return v


@avail_pub_router.post("/availability/{token}")
async def submit_availability(token: str, body: AvailabilityBody):
    """Submit an in-person availability answer.

    Rules:
      • YES → unlocks scheduling, clears needs-HR-review if present.
      • NO  → requires `reason_id` ∈ active reasons. `other` requires non-empty
              `other_text` ≤50 chars. May optionally set `withdraw_decision`
              but that alone does NOT withdraw the application — withdrawal
              requires the dedicated `/withdraw` endpoint.
      • If the candidate is already withdrawn, any submission here is 409;
        they must reactivate first via `/reactivate`.
      • Historical booking stays untouched; if already booked and new
        answer is NO, `needs_hr_review` is raised.
    """
    reg = await _reg_by_token(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Invalid or expired link")
    if reg.get("withdrawn_by_candidate"):
        raise HTTPException(status_code=409, detail="Application is withdrawn — please reactivate first.")
    if reg.get("superseded"):
        raise HTTPException(status_code=410, detail="This application has been superseded by a newer registration.")

    cfg = await _get_current_config()

    # Idempotency: if we've already saved this nonce, validate payload and replay.
    prior = await _already_submitted(body.submission_nonce)
    if prior:
        if prior.get("response_type") != "availability":
            raise HTTPException(
                status_code=409,
                detail="This submission nonce was already used for a different operation.",
            )
        # Payload consistency check: nonce reuse with different payload → 409.
        if (prior.get("availability") != bool(body.availability)
                or prior.get("reason_id") != (body.reason_id or None)
                or prior.get("other_text") != (body.other_text or None)):
            raise HTTPException(
                status_code=409,
                detail="A different response was already recorded for this submission nonce.",
            )
        reg2 = await _reg_by_token(token)
        return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg2, cfg)}

    active_ids = _active_reason_ids(cfg)
    reason_id = body.reason_id or None
    other_text = body.other_text or None
    reason_text = None

    if body.availability is False:
        if not reason_id:
            raise HTTPException(status_code=400, detail="Please select a reason.")
        if reason_id not in active_ids:
            raise HTTPException(status_code=400, detail="Invalid reason selected.")
        reason_text = _reason_label_for_id(cfg, reason_id)
        if reason_id == "other":
            if not other_text:
                raise HTTPException(status_code=400, detail="Please tell us why (required for Other).")
        else:
            other_text = None
    else:
        reason_id = None
        other_text = None

    now = _now_iso()
    had_booking = bool(reg.get("schedule_date"))
    needs_review = bool(reg.get("needs_hr_review"))
    set_fields = {
        "chennai_availability_latest": bool(body.availability),
        "chennai_availability_latest_at": now,
        "chennai_availability_reason_id": reason_id,
        "chennai_availability_reason_text": reason_text,
        "chennai_availability_other_text": other_text,
        "chennai_availability_withdraw_decision": body.withdraw_decision,
    }
    # YES clears any NO-triggered HR-review flag; never clears a flag we didn't raise.
    if body.availability is True and needs_review and reg.get("needs_hr_review_reason") == "booked_changed_to_no":
        set_fields["needs_hr_review"] = False
        set_fields["needs_hr_review_cleared_at"] = now

    # Already-booked candidate flipping to NO → raise HR review (but DO NOT
    # alter the booking itself).
    if had_booking and body.availability is False:
        set_fields["needs_hr_review"] = True
        set_fields["needs_hr_review_reason"] = "booked_changed_to_no"
        set_fields["needs_hr_review_at"] = now

    await _db.bb_registrations.update_one({"_id": reg["_id"]}, {"$set": set_fields})

    # History append
    nonce = body.submission_nonce or uuid.uuid4().hex
    await _append_response(reg, {
        "registration_id": str(reg["_id"]),
        "schedule_token": token,
        "response_type": "availability",
        "availability": bool(body.availability),
        "reason_id": reason_id,
        "reason_text": reason_text,
        "other_text": other_text,
        "withdraw_decision": body.withdraw_decision,
        "submitted_at": now,
        "submission_nonce": nonce,
        "question_version": cfg.get("version"),
        "source": "pub_schedule_link",
        "booking_snapshot": _booking_snapshot(reg),
    })

    reg2 = await _reg_by_token(token)
    return {
        "ok": True,
        "candidate": _registration_summary(reg2, cfg),
    }


class WithdrawBody(BaseModel):
    confirmed: bool                              # Must be True
    submission_nonce: Optional[str] = None


@avail_pub_router.post("/availability/{token}/withdraw")
async def submit_withdraw(token: str, body: WithdrawBody):
    """Final, idempotent withdrawal.

    Requires:
      • latest availability is NO (user just answered NO before this).
      • `confirmed` is True (the checkbox).
      • not already withdrawn (idempotent 200 if it is).
    """
    reg = await _reg_by_token(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Invalid or expired link")
    if reg.get("superseded"):
        raise HTTPException(status_code=410, detail="Application superseded.")

    cfg = await _get_current_config()
    # Idempotency — must come BEFORE the state check so a replay of the exact
    # successful withdrawal always returns 200 regardless of current state.
    prior = await _already_submitted(body.submission_nonce)
    if prior:
        if prior.get("response_type") != "withdrawal":
            raise HTTPException(
                status_code=409,
                detail="This submission nonce was already used for a different operation.",
            )
        return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg, cfg)}

    if reg.get("withdrawn_by_candidate"):
        # Already withdrawn by a prior request (no nonce / different nonce) — idempotent 200.
        return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg, cfg)}

    if not body.confirmed:
        raise HTTPException(status_code=400, detail="Please confirm the acknowledgement before withdrawing.")
    if reg.get("chennai_availability_latest") is not False:
        raise HTTPException(status_code=409, detail="Withdrawal is only available after answering NO.")

    now = _now_iso()
    # Conditional atomic update: only flip when still not-withdrawn. Prevents
    # a second request that slips past the check above from firing side-effects.
    upd = await _db.bb_registrations.update_one(
        {"_id": reg["_id"], "withdrawn_by_candidate": {"$ne": True}},
        {"$set": {
            "withdrawn_by_candidate": True,
            "withdrawn_at": now,
            "withdrawal_reason_id": reg.get("chennai_availability_reason_id"),
            "withdrawal_reason_text": reg.get("chennai_availability_reason_text"),
            "withdrawal_other_text": reg.get("chennai_availability_other_text"),
        }},
    )
    if upd.modified_count == 0:
        # A concurrent writer got there first — return idempotent 200.
        reg2 = await _reg_by_token(token)
        return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg2, cfg)}
    nonce = body.submission_nonce or uuid.uuid4().hex
    await _append_response(reg, {
        "registration_id": str(reg["_id"]),
        "schedule_token": token,
        "response_type": "withdrawal",
        "availability": False,
        "withdraw_decision": True,
        "withdraw_confirmed": True,
        "reason_id": reg.get("chennai_availability_reason_id"),
        "reason_text": reg.get("chennai_availability_reason_text"),
        "other_text": reg.get("chennai_availability_other_text"),
        "submitted_at": now,
        "submission_nonce": nonce,
        "question_version": cfg.get("version"),
        "source": "pub_schedule_link_withdraw",
        "booking_snapshot": _booking_snapshot(reg),
    })

    reg2 = await _reg_by_token(token)
    return {"ok": True, "candidate": _registration_summary(reg2, cfg)}


class ReactivateBody(BaseModel):
    choice: str                                    # "yes" | "no" | "other"
    other_text: Optional[str] = None               # required when choice == "other"
    submission_nonce: Optional[str] = None

    @field_validator("choice")
    @classmethod
    def _choice_v(cls, v):
        v2 = (v or "").strip().lower()
        if v2 not in {"yes", "no", "other"}:
            raise ValueError("choice must be 'yes', 'no' or 'other'")
        return v2

    @field_validator("other_text")
    @classmethod
    def _clip(cls, v):
        if v is None:
            return v
        v = v.strip()
        if len(v) > REACT_OTHER_TEXT_MAX:
            raise ValueError(f"other_text may be at most {REACT_OTHER_TEXT_MAX} characters")
        return v


@avail_pub_router.post("/availability/{token}/reactivate")
async def submit_reactivate(token: str, body: ReactivateBody):
    """Reactivation flow for a previously withdrawn candidate.

    YES   → reactivate, record YES availability snapshot, open scheduling.
    NO    → remain withdrawn.
    Other → record message, no reactivation.

    Company-side rejection (`status == "Rejected"` or `is_shortlisted != True`
    at the time of this request) blocks reactivation outright.
    """
    reg = await _reg_by_token(token)
    if not reg:
        raise HTTPException(status_code=404, detail="Invalid or expired link")
    if reg.get("superseded"):
        raise HTTPException(status_code=410, detail="Application superseded.")

    cfg = await _get_current_config()
    # Idempotency replay — must come BEFORE the "not withdrawn" state check,
    # otherwise a replay of a successful YES reactivation would 409 because
    # the candidate is already reactivated.
    prior = await _already_submitted(body.submission_nonce)
    if prior:
        if prior.get("response_type") != "reactivation":
            raise HTTPException(
                status_code=409,
                detail="This submission nonce was already used for a different operation.",
            )
        if prior.get("reactivation_choice") != body.choice:
            raise HTTPException(
                status_code=409,
                detail="A different reactivation choice was already recorded for this submission nonce.",
            )
        return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg, cfg)}

    if not reg.get("withdrawn_by_candidate"):
        raise HTTPException(status_code=409, detail="Application is not in a withdrawn state.")

    now = _now_iso()
    choice = body.choice

    if choice == "yes":
        # Independent company-side eligibility check.
        if reg.get("status") == "Rejected" or reg.get("is_shortlisted") is not True:
            raise HTTPException(
                status_code=409,
                detail="This application is no longer eligible for an interview.",
            )
        # Conditional atomic update — only flip if still withdrawn.
        upd = await _db.bb_registrations.update_one(
            {"_id": reg["_id"], "withdrawn_by_candidate": True},
            {"$set": {
                "withdrawn_by_candidate": False,
                "reactivated_at": now,
                # YES answer snapshot
                "chennai_availability_latest": True,
                "chennai_availability_latest_at": now,
                "chennai_availability_reason_id": None,
                "chennai_availability_reason_text": None,
                "chennai_availability_other_text": None,
                "chennai_availability_withdraw_decision": False,
            },
             "$inc": {"reactivation_count": 1}},
        )
        if upd.modified_count == 0:
            # Already reactivated by a concurrent writer — idempotent return.
            reg2 = await _reg_by_token(token)
            return {"ok": True, "idempotent": True, "candidate": _registration_summary(reg2, cfg)}
    elif choice == "no":
        # Stay withdrawn — just record the decision.
        pass
    else:  # other
        if not body.other_text:
            raise HTTPException(status_code=400, detail="Please tell us your question or requirement.")

    nonce = body.submission_nonce or uuid.uuid4().hex
    await _append_response(reg, {
        "registration_id": str(reg["_id"]),
        "schedule_token": token,
        "response_type": "reactivation",
        "reactivation_choice": choice,
        "other_text": body.other_text if choice == "other" else None,
        "submitted_at": now,
        "submission_nonce": nonce,
        "question_version": cfg.get("version"),
        "source": "pub_schedule_link_reactivate",
        "booking_snapshot": _booking_snapshot(reg),
    })

    reg2 = await _reg_by_token(token)
    return {"ok": True, "candidate": _registration_summary(reg2, cfg)}


# =========================================================================
# ADMIN ENDPOINTS — CSRF-gated by auth cookie only (matches existing
# bb_router style; no additional CSRF header used elsewhere in bb_modules).
# =========================================================================
@avail_admin_router.get("/availability-question/current")
async def admin_get_current(request: Request):
    await _require_auth(request)
    cfg = await _get_current_config()
    return cfg


class DraftBody(BaseModel):
    config: dict


def _validate_draft(cfg: dict) -> dict:
    """Strict additive validator. Raises HTTPException on structural issues.

    Guarantees: reserved reason IDs remain present with `id`, `label`
    non-empty. Admins may add new IDs so long as they are kebab/snake-ish
    and don't collide with reserved semantic flags.
    """
    required_keys = (
        "main_question", "yes_label", "no_label",
        "reason_question", "reason_options", "other_label",
        "explanation_text",
        "withdraw_question", "withdraw_yes_label", "withdraw_no_label",
        "withdraw_explanation", "withdraw_confirm_check",
        "withdraw_button_label", "withdraw_thankyou",
        "reactivation_question",
        "reactivation_yes_label", "reactivation_no_label", "reactivation_other_label",
        "reactivation_other_placeholder",
        "reactivation_no_confirmation", "reactivation_other_confirmation",
        "booked_no_notice",
    )
    out = {}
    for k in required_keys:
        if k not in cfg:
            raise HTTPException(status_code=400, detail=f"Missing field: {k}")
        v = cfg[k]
        if k == "reason_options":
            if not isinstance(v, list) or not v:
                raise HTTPException(status_code=400, detail="reason_options must be a non-empty list")
            ids_seen = set()
            cleaned = []
            for i, opt in enumerate(v):
                if not isinstance(opt, dict):
                    raise HTTPException(status_code=400, detail=f"reason_options[{i}] must be an object")
                oid = (opt.get("id") or "").strip()
                olab = (opt.get("label") or "").strip()
                if not oid or not olab:
                    raise HTTPException(status_code=400, detail=f"reason_options[{i}] needs id+label")
                if not re.fullmatch(r"[a-z0-9_]+", oid):
                    raise HTTPException(status_code=400, detail=f"reason id '{oid}' must be lower snake_case")
                if oid in ids_seen:
                    raise HTTPException(status_code=400, detail=f"duplicate reason id '{oid}'")
                ids_seen.add(oid)
                cleaned.append({"id": oid, "label": olab, "active": bool(opt.get("active", True))})
            missing_reserved = RESERVED_REASON_IDS - ids_seen
            if missing_reserved:
                raise HTTPException(
                    status_code=400,
                    detail=f"These reserved reason IDs must remain: {sorted(missing_reserved)}",
                )
            # At least one active reason must exist, and 'other' must stay active
            # so candidates can always submit NO with a free-form note.
            active_ids_ = {o["id"] for o in cleaned if o.get("active", True)}
            if not active_ids_:
                raise HTTPException(status_code=400, detail="At least one reason must be active.")
            if "other" not in active_ids_:
                raise HTTPException(status_code=400, detail="The 'other' reason must remain active.")
            out[k] = cleaned
        else:
            if not isinstance(v, str) or not v.strip():
                raise HTTPException(status_code=400, detail=f"{k} must be a non-empty string")
            out[k] = v
    return out


@avail_admin_router.get("/availability-question/draft")
async def admin_get_draft(request: Request):
    user = await _require_auth(request)
    username = user if isinstance(user, str) else (
        (user.get("username") if isinstance(user, dict) else getattr(user, "username", None))
        or "unknown"
    )
    doc = await _db.bb_availability_question_drafts.find_one({"username": username})
    if not doc:
        # Seed draft with current config so admins see something.
        cfg = await _get_current_config()
        return {"draft": cfg, "based_on_version": cfg.get("version"), "username": username}
    doc.pop("_id", None)
    return {"draft": doc.get("config"), "based_on_version": doc.get("based_on_version"),
            "username": username, "saved_at": doc.get("saved_at")}


@avail_admin_router.post("/availability-question/draft")
async def admin_save_draft(request: Request, body: DraftBody):
    user = await _require_auth(request)
    username = user if isinstance(user, str) else (
        (user.get("username") if isinstance(user, dict) else getattr(user, "username", None))
        or "unknown"
    )
    cleaned = _validate_draft(body.config)
    cur = await _get_current_config()
    now = _now_iso()
    await _db.bb_availability_question_drafts.update_one(
        {"username": username},
        {"$set": {
            "username": username,
            "config": cleaned,
            "based_on_version": cur.get("version"),
            "saved_at": now,
        }},
        upsert=True,
    )
    return {"ok": True, "saved_at": now, "based_on_version": cur.get("version")}


@avail_admin_router.post("/availability-question/publish")
async def admin_publish(request: Request, body: DraftBody):
    """Publish a new version of the question config.

    Historical responses stay intact: each response row already carries the
    `question_version` and `reason_text` it was captured with.
    """
    user = await _require_auth(request)
    username = user if isinstance(user, str) else (
        (user.get("username") if isinstance(user, dict) else getattr(user, "username", None))
        or "unknown"
    )
    cleaned = _validate_draft(body.config)
    cur = await _get_current_config()
    now = _now_iso()
    # Atomic monotonically-increasing version allocation using a counter doc.
    # Two concurrent publishes will receive different values because $inc is
    # atomic in MongoDB.
    from pymongo import ReturnDocument
    counter = await _db.bb_availability_counter.find_one_and_update(
        {"_id": "question_version"},
        {"$inc": {"value": 1}, "$setOnInsert": {"created_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seeded_value = int(counter.get("value") or 1)
    # Ensure the counter never slips below current published version (seed-up).
    if seeded_value <= int(cur.get("version") or 1):
        counter = await _db.bb_availability_counter.find_one_and_update(
            {"_id": "question_version"},
            {"$set": {"value": int(cur.get("version") or 1) + 1}},
            return_document=ReturnDocument.AFTER,
        )
        seeded_value = int(counter.get("value"))
    next_version = seeded_value
    new_doc = {
        **cleaned,
        "version": next_version,
        "published_at": now,
        "published_by": username,
    }
    # Insert the immutable version row FIRST — unique index on `version`
    # guarantees only one writer wins if races slip through the counter.
    try:
        await _db.bb_availability_question_versions.insert_one({**new_doc, "snapshot_at": now})
    except Exception:
        # Unique conflict → pick the next available slot.
        counter = await _db.bb_availability_counter.find_one_and_update(
            {"_id": "question_version"},
            {"$inc": {"value": 1}},
            return_document=ReturnDocument.AFTER,
        )
        next_version = int(counter.get("value"))
        new_doc["version"] = next_version
        await _db.bb_availability_question_versions.insert_one({**new_doc, "snapshot_at": now})
    # Now atomically update current only if the new version is strictly greater.
    # Catch DuplicateKey on upsert if a concurrent writer inserted first.
    try:
        await _db.bb_availability_question_config.update_one(
            {"_id": "current", "$or": [{"version": {"$lt": next_version}}, {"version": {"$exists": False}}]},
            {"$set": new_doc},
            upsert=True,
        )
    except Exception:
        # Current doc already written at a higher version — our version row
        # is still safely stored in bb_availability_question_versions.
        pass
    await _db.bb_availability_question_audit.insert_one({
        "action": "publish",
        "from_version": cur.get("version"),
        "to_version": next_version,
        "by": username,
        "at": now,
    })
    # Clear this user's draft so the editor restarts from the published copy.
    # Soft-reset (never hard-delete): overwrite the draft with the newly
    # published config and mark it as `cleared_after_publish` so future edits
    # start from a clean, up-to-date baseline.
    await _db.bb_availability_question_drafts.update_one(
        {"username": username},
        {"$set": {
            "username": username,
            "config": new_doc,
            "based_on_version": next_version,
            "saved_at": now,
            "cleared_after_publish": True,
        }},
        upsert=True,
    )
    return {"ok": True, "version": next_version, "published_at": now}


@avail_admin_router.get("/availability-question/versions")
async def admin_versions(request: Request):
    await _require_auth(request)
    docs = await _db.bb_availability_question_versions.find(
        {}, {"_id": 0}
    ).sort("version", -1).to_list(200)
    return {"versions": docs}


@avail_admin_router.get("/availability-question/audit")
async def admin_audit(request: Request):
    await _require_auth(request)
    docs = await _db.bb_availability_question_audit.find(
        {}, {"_id": 0}
    ).sort("at", -1).to_list(200)
    return {"audit": docs}
