"""Enhancement 2 — consent-scope regression for the three calling workflows.

Confirms that:
  1. The current `DEFAULT_PUBLISHED_SCOPE` and `scope_for()` map DO NOT include
     `booking_confirmation`, so admin-published consents today cannot authorize
     Booking Confirmation calls.
  2. `purposes.consent_covers()` therefore returns False for `booking_confirmation`
     even when the candidate has granted consent to the currently-published
     version, preventing silent expansion.
  3. Both legacy workflows (`interview_confirmation`, `missed_interview_followup`)
     remain covered by the latest published scope.
  4. Older, previously-stored consents (legacy `2026-06-v{2,3,4}` version_ids)
     keep exactly the scope they originally stamped — never upgraded.

SAFETY: pure-function assertions against `ai_calling.modes.scope_for` and
`ai_calling.purposes.consent_covers`. No DB writes, no network, no provider
calls. Nothing is published.
"""
from ai_calling.modes import CONSENT_SCOPES, DEFAULT_PUBLISHED_SCOPE, scope_for
from ai_calling.purposes import BOOKING, CONFIRM, MISSED, PURPOSES, consent_covers


def test_three_calling_workflows_defined():
    assert set(PURPOSES) == {CONFIRM, MISSED, BOOKING}


def test_default_published_scope_excludes_booking_confirmation():
    """Today's `DEFAULT_PUBLISHED_SCOPE` must NOT contain booking_confirmation,
    so an admin publish that happens before the new 3-workflow wording ships
    cannot silently authorize Booking Confirmation calls."""
    assert BOOKING not in DEFAULT_PUBLISHED_SCOPE
    assert CONFIRM in DEFAULT_PUBLISHED_SCOPE
    assert MISSED in DEFAULT_PUBLISHED_SCOPE


def test_newly_published_version_does_not_cover_booking():
    """A freshly admin-published version_id (prefix `consent-`) falls back to
    `DEFAULT_PUBLISHED_SCOPE`; it must not permit booking_confirmation."""
    vid = "consent-2026-11-16-1794820917329965"
    scope = scope_for(vid)
    assert BOOKING not in scope
    assert CONFIRM in scope and MISSED in scope


def test_legacy_versions_retain_original_scope():
    """Historical consents keep the EXACT scope they originally stamped.
    None of the legacy versions ever authorized booking_confirmation."""
    for v, expected in CONSENT_SCOPES.items():
        s = scope_for(v)
        assert s == list(expected)
        assert BOOKING not in s


def test_unknown_version_grants_nothing():
    """Unknown / malformed version strings fall through to [] so a corrupted
    record cannot grant any calling purpose."""
    for junk in ("", None, "not-a-version", "2025-01-draft", "random"):
        assert scope_for(junk) == []


def _consent(status: str, scope: list = None, text_version: str = None) -> dict:
    return {"status": status, "scope": scope, "text_version": text_version}


def test_consent_covers_blocks_booking_on_current_publish():
    """Candidate granted the currently-published wording → consent_covers
    returns False for booking_confirmation, True for the other two purposes."""
    vid = "consent-2026-11-16-1794820917329965"
    c = _consent("granted", scope=scope_for(vid), text_version=vid)
    assert consent_covers(c, CONFIRM) is True
    assert consent_covers(c, MISSED) is True
    assert consent_covers(c, BOOKING) is False


def test_consent_covers_requires_granted_status():
    """A `not_given` or `withdrawn` consent covers no purpose, regardless of
    the stored scope."""
    for status in ("not_given", "withdrawn", None):
        c = {"status": status, "scope": [CONFIRM, MISSED, BOOKING], "text_version": "ignored"}
        assert consent_covers(c, CONFIRM) is False
        assert consent_covers(c, MISSED) is False
        assert consent_covers(c, BOOKING) is False


def test_legacy_v4_consent_still_covers_missed_and_confirm_not_booking():
    c = _consent("granted", scope=None, text_version="2026-06-v4")
    assert consent_covers(c, CONFIRM) is True
    assert consent_covers(c, MISSED) is True
    assert consent_covers(c, BOOKING) is False


def test_legacy_v2_consent_only_covers_interview_confirmation():
    c = _consent("granted", scope=None, text_version="2026-06-v2")
    assert consent_covers(c, CONFIRM) is True
    assert consent_covers(c, MISSED) is False
    assert consent_covers(c, BOOKING) is False


# ---------------------------------------------------------------------------
# Scope-at-publish: new `derive_published_scope()` (Enhancement 2 scope fix)
# ---------------------------------------------------------------------------
from ai_calling.modes import derive_published_scope  # noqa: E402


def test_derive_scope_grants_booking_only_when_phrase_present():
    """`booking_confirmation` is added ONLY when the wording explicitly
    mentions "booking confirmation" (case-insensitive, whitespace-tolerant).
    Legacy workflows are always granted."""
    # Current admin-published wording (pre-fix) — no booking phrase.
    old = ("I agree to receive automated AI calls from BluBridge Technologies about "
           "my interview. Calls may be recorded and transcribed for attendance and "
           "rescheduling purposes.")
    assert derive_published_scope(old) == [CONFIRM, MISSED]

    # Enhancement 2 approved wording — explicit Booking Confirmation phrase.
    new = ("I agree to receive automated AI voice calls. These may include: "
           "(a) a Booking Confirmation call shortly after I schedule my slot; "
           "(b) a Pre-Interview Confirmation call on the day before my interview; "
           "and (c) a Missed-Interview Follow-up call.")
    assert derive_published_scope(new) == [BOOKING, CONFIRM, MISSED]


def test_derive_scope_case_and_whitespace_tolerant():
    """The match tolerates case and inner whitespace so minor formatting
    variations by the admin do not accidentally drop booking_confirmation."""
    for phrase in ("Booking Confirmation", "booking confirmation",
                   "BOOKING CONFIRMATION", "booking  confirmation",
                   "booking\nconfirmation"):
        text = f"Wording that mentions a {phrase} call. More text. " * 3
        assert BOOKING in derive_published_scope(text), phrase


def test_derive_scope_rejects_unrelated_booking_mentions():
    """A substring match on 'booking' alone must NOT grant booking_confirmation;
    the full phrase is required."""
    text = "Wording that only talks about interview booking and nothing else. " * 3
    assert derive_published_scope(text) == [CONFIRM, MISSED]
    assert BOOKING not in derive_published_scope(text)


def test_derive_scope_handles_empty_and_none():
    assert derive_published_scope("") == [CONFIRM, MISSED]
    assert derive_published_scope(None) == [CONFIRM, MISSED]
    assert BOOKING not in derive_published_scope("")
