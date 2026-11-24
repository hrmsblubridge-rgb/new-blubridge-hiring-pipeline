"""
iter170 — Hard safety guard: when TEST_MODE=false and a preview / dev URL
is about to leak into an outbound candidate message, the send MUST be
refused (not just warned). Confirms the behaviour for both Email (Resend)
and WhatsApp (AiSensy) paths without actually hitting either provider.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch

# Load backend env for MONGO_URL used indirectly by messaging init.
from dotenv import load_dotenv
load_dotenv("/app/backend/.env")

import messaging  # noqa: E402


PREVIEW_URL = "https://blubridge-recruiter.preview.emergentagent.com/schedule-interview/abc"
PROD_URL = "https://recruit.blubridge.com/schedule-interview/abc"


@pytest.fixture(autouse=True)
def _env_setup(monkeypatch):
    monkeypatch.setenv("ENABLE_EMAIL", "true")
    monkeypatch.setenv("ENABLE_WHATSAPP", "true")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    # Open the gate: tester allowlist is checked via DB; we patch can_send_message.
    yield


@pytest.mark.asyncio
async def test_email_refuses_preview_url_in_prod(monkeypatch):
    """In TEST_MODE=false, an email carrying a preview URL must be hard-refused
    (no POST to Resend, return False, loud [BLOCK:PROD_URL_LEAK] log)."""
    monkeypatch.setattr(messaging, "is_test_mode", lambda: False)
    monkeypatch.setattr(messaging, "can_send_message",
                        AsyncMock(return_value=(True, "prod_live")))

    with patch("httpx.AsyncClient") as mock_http:
        ok = await messaging.send_email(
            "rishi.nayak@blubridge.com", "9443109903",
            "Reschedule your interview",
            f'<p>Click <a href="{PREVIEW_URL}">here</a> to reschedule.</p>',
        )

    assert ok is False, "send_email must refuse when prod body contains preview URL"
    assert not mock_http.called, "httpx must NOT be invoked — Resend never hit"


@pytest.mark.asyncio
async def test_whatsapp_refuses_preview_url_in_prod(monkeypatch):
    """Same guard for AiSensy WhatsApp: a template param carrying the preview
    URL must be refused in prod."""
    monkeypatch.setattr(messaging, "is_test_mode", lambda: False)
    monkeypatch.setattr(messaging, "can_send_message",
                        AsyncMock(return_value=(True, "prod_live")))

    with patch("httpx.AsyncClient") as mock_http:
        ok = await messaging.send_whatsapp(
            "Candidate Followups1",
            "9443109903", "rishi.nayak@blubridge.com",
            ["Rishi", "AI & ML Engineer", "24-11-2026", "12:30 PM", PREVIEW_URL],
        )

    assert ok is False, "send_whatsapp must refuse when prod template contains preview URL"
    assert not mock_http.called, "httpx must NOT be invoked — AiSensy never hit"


@pytest.mark.asyncio
async def test_email_allowed_with_clean_prod_url(monkeypatch):
    """A clean production URL must sail through the guard untouched."""
    monkeypatch.setattr(messaging, "is_test_mode", lambda: False)
    monkeypatch.setattr(messaging, "can_send_message",
                        AsyncMock(return_value=(True, "prod_live")))

    # Mock the Resend POST — status 200 with json id.
    class _Resp:
        status_code = 200
        text = '{"id":"stub"}'
        def json(self): return {"id": "stub"}

    class _Client:
        async def __aenter__(self_inner): return self_inner
        async def __aexit__(self_inner, *a): return False
        async def post(self_inner, *a, **k): return _Resp()

    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        ok = await messaging.send_email(
            "rishi.nayak@blubridge.com", "9443109903",
            "Reschedule your interview",
            f'<p>Click <a href="{PROD_URL}">here</a>.</p>',
        )
    assert ok is True


@pytest.mark.asyncio
async def test_preview_url_allowed_in_test_mode(monkeypatch):
    """In TEST_MODE=true (preview), the preview URL is expected — guard MUST NOT trip."""
    monkeypatch.setattr(messaging, "is_test_mode", lambda: True)
    monkeypatch.setattr(messaging, "can_send_message",
                        AsyncMock(return_value=(True, "test_mode:tester_allowed")))

    class _Resp:
        status_code = 200
        text = '{"id":"stub"}'
        def json(self): return {"id": "stub"}

    class _Client:
        async def __aenter__(self_inner): return self_inner
        async def __aexit__(self_inner, *a): return False
        async def post(self_inner, *a, **k): return _Resp()

    with patch("httpx.AsyncClient", lambda *a, **k: _Client()):
        ok = await messaging.send_email(
            "rishi.nayak@blubridge.com", "9443109903",
            "Preview test",
            f'<p>{PREVIEW_URL}</p>',
        )
    assert ok is True
