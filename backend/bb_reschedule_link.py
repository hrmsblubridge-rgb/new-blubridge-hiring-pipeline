"""Enhancement 2 §15A — automatic rescheduling-link delivery.

Called from the AI-calling outcome pipeline when a verified Bolna execution
reports an explicit `RESCHEDULE_REQUESTED` (or `wants_reschedule=true`) for any
of the three call purposes.

Behaviour:
  • Uses the EXISTING scheduling link workflow (bb_registrations.schedule_token
    + the already-configured AiSensy / Resend templates). No new provider.
  • Respects Enhancement 1: candidate withdrawn, latest_availability NO,
    superseded registration, non-shortlisted → suppressed.
  • Idempotent on (registration_id, schedule_version, execution_id) via a
    unique sparse index on bb_reschedule_notifications.idempotency_key.
  • Never claims success until the delivery provider actually returned OK.
  • Dev-mode safe: when ENABLE_WHATSAPP/ENABLE_EMAIL is "false" the delivery
    call is a no-op and the outcome row records `SENT_MOCKED`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("bb_reschedule_link")

_db = None


def init_reschedule_link(database):
    global _db
    _db = database


async def ensure_indexes():
    if _db is None:
        return
    try:
        await _db.bb_reschedule_notifications.create_index(
            [("idempotency_key", 1)],
            name="rl_idemp_unique", unique=True, sparse=True,
        )
        await _db.bb_reschedule_notifications.create_index(
            [("registration_id", 1), ("created_at", -1)],
            name="rl_reg_created_desc",
        )
    except Exception as e:  # pragma: no cover
        logger.warning(f"bb_reschedule_link index skipped: {e}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _eligibility_check(reg: dict) -> Optional[str]:
    """Enhancement-1 aware eligibility. Returns a reason to SUPPRESS or None."""
    if not reg:
        return "REGISTRATION_NOT_FOUND"
    if reg.get("superseded"):
        return "SUPERSEDED"
    if reg.get("withdrawn_by_candidate"):
        return "WITHDRAWN"
    if reg.get("is_shortlisted") is not True:
        return "NOT_SHORTLISTED"
    # Chennai availability: must be True (not null, not False).
    la = reg.get("chennai_availability_latest")
    if la is False:
        return "AVAILABILITY_NO"
    # legacy null is allowed — the link itself re-prompts the Enhancement 1 gate
    # in the website when the candidate clicks through.
    if not reg.get("schedule_token"):
        return "NO_SCHEDULE_TOKEN"
    return None


async def deliver_reschedule_link(
    registration_id: str,
    schedule_version: int,
    execution_id: str,
    source_purpose: str,
) -> dict:
    """Idempotent deliver. Safe to call from provider webhooks, verify handlers
    and manual replay tools."""
    if _db is None:
        return {"status": "FAILED", "reason": "LINK_MODULE_NOT_INITIALISED"}

    idempotency_key = f"{registration_id}:{schedule_version}:{execution_id}:reschedule_link"
    prior = await _db.bb_reschedule_notifications.find_one(
        {"idempotency_key": idempotency_key}, {"_id": 0},
    )
    if prior:
        return {"status": "SUPPRESSED_DUPLICATE", "prior": prior}

    from bson import ObjectId as _OID
    try:
        reg = await _db.bb_registrations.find_one({"_id": _OID(registration_id)})
    except Exception:
        reg = await _db.bb_registrations.find_one({"_id": registration_id})

    reason = _eligibility_check(reg)
    row = {
        "idempotency_key": idempotency_key,
        "registration_id": registration_id,
        "schedule_version": schedule_version,
        "execution_id": execution_id,
        "source_purpose": source_purpose,
        "created_at": _now(),
        "channel": None,
        "message_id": None,
        "state": None,
        "reason": None,
    }
    if reason:
        row.update(state="SUPPRESSED_INELIGIBLE", reason=reason)
        await _db.bb_reschedule_notifications.insert_one(row)
        return {"status": "SUPPRESSED_INELIGIBLE", "reason": reason}

    # Dev-mode mock: no real sends.
    enable_wa = os.environ.get("ENABLE_WHATSAPP", "false").strip().lower() == "true"
    enable_em = os.environ.get("ENABLE_EMAIL", "false").strip().lower() == "true"
    if not (enable_wa or enable_em):
        row.update(state="SENT_MOCKED", channel="mock",
                   message_id=f"mock-{execution_id[:8]}")
        await _db.bb_reschedule_notifications.insert_one(row)
        # Also stamp the registration so Journey/Export reflect delivery intent.
        try:
            await _db.bb_registrations.update_one(
                {"_id": reg["_id"]},
                {"$set": {
                    "reschedule_link_last_state": "SENT_MOCKED",
                    "reschedule_link_last_source": source_purpose,
                    "reschedule_link_last_at": row["created_at"],
                }},
            )
        except Exception:
            pass
        return {"status": "SENT_MOCKED"}

    # Real-provider branch (unreachable in development because ENABLE_* are
    # false during the development pod). Kept thin and defensive — we let the
    # existing messaging helpers do the actual send and only record what they
    # returned. This path is NOT executed under hiring_ai_testing.
    try:
        from messaging import notify_shortlisted  # type: ignore
        wa_ok, em_ok = await notify_shortlisted(
            reg.get("full_name", ""), reg.get("phone", ""), reg.get("email", ""),
            reg.get("schedule_token", ""),
        )
        state = "SENT" if (wa_ok or em_ok) else "FAILED"
        row.update(state=state, channel="whatsapp+email" if (wa_ok and em_ok)
                   else "whatsapp" if wa_ok else "email" if em_ok else None)
    except Exception as e:  # pragma: no cover
        logger.exception(f"reschedule link delivery failed: {e}")
        row.update(state="FAILED", reason=str(e)[:200])
    await _db.bb_reschedule_notifications.insert_one(row)
    if row["state"] == "SENT":
        await _db.bb_registrations.update_one(
            {"_id": reg["_id"]},
            {"$set": {"reschedule_link_last_state": "SENT",
                      "reschedule_link_last_source": source_purpose,
                      "reschedule_link_last_at": row["created_at"]}},
        )
    return {"status": row["state"]}
