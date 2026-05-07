"""
test_langfuse_instrumentation - safety net for the Langfuse layer.

Goals:
  1. With LANGFUSE_ENABLED unset/false, every public function is a no-op
     and the patient-facing path runs unchanged.
  2. With Langfuse "configured" but the SDK missing or import-failing,
     the same no-op behavior holds (we never propagate ImportError).
  3. The PHI mask redacts user-role message content but leaves system
     prompts, tool inputs, and assistant outputs intact.
  4. The session_id derived from patient_uid is a stable hash, never
     the raw uid.

We don't run a real Langfuse instance in CI. The mask + kill-switch
tests are the load-bearing checks; integration verification happens
manually after `docker compose up` per infra/langfuse/README.md.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


@pytest.fixture(autouse=True)
def _reset_singleton(monkeypatch):
    """Each test starts with a fresh singleton state."""
    import langfuse_client
    langfuse_client.reset_for_tests()
    # Make absolutely sure no enable bleed across tests.
    monkeypatch.delenv("LANGFUSE_ENABLED", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    yield
    langfuse_client.reset_for_tests()


# ---------------------------------------------------------------------------
# Kill-switch: disabled by default
# ---------------------------------------------------------------------------

def test_disabled_by_default():
    import langfuse_client
    assert langfuse_client.is_enabled() is False
    assert langfuse_client.get_client() is None


def test_request_span_is_noop_when_disabled():
    import langfuse_client
    with langfuse_client.request_span("test", request_id="abc",
                                      patient_uid="user-1") as span:
        assert span is None


def test_span_is_noop_when_disabled():
    import langfuse_client
    with langfuse_client.span("any.span") as span:
        assert span is None


def test_flush_is_noop_when_disabled():
    import langfuse_client
    # Should not raise, should not call into any real client.
    langfuse_client.flush()


# ---------------------------------------------------------------------------
# Configured but env incomplete -> still no-op, no exceptions
# ---------------------------------------------------------------------------

def test_enabled_without_keys_returns_none(monkeypatch):
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    # No HOST/keys set.
    import langfuse_client
    assert langfuse_client.is_enabled() is True
    assert langfuse_client.get_client() is None


def test_enabled_with_keys_but_sdk_missing_returns_none(monkeypatch):
    """If the langfuse package can't be imported, we return None and log,
    never propagate ImportError into the patient flow."""
    monkeypatch.setenv("LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    # Force the import to fail by stubbing sys.modules.
    monkeypatch.setitem(sys.modules, "langfuse", None)

    import langfuse_client
    assert langfuse_client.get_client() is None


# ---------------------------------------------------------------------------
# PHI mask behavior
# ---------------------------------------------------------------------------

def test_mask_redacts_user_string_content():
    from langfuse_client import _mask
    msg = {"role": "user", "content": "I have severe knee pain after running"}
    masked = _mask(data=msg)
    assert masked["role"] == "user"
    assert masked["content"].startswith("[redacted user content")
    assert "severe" not in masked["content"]
    assert "knee" not in masked["content"]


def test_mask_redacts_user_block_content():
    from langfuse_client import _mask
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "Patient name: Andre, age 27, ACL surgery"},
            {"type": "image", "source": {"type": "base64", "data": "..."}},
        ],
    }
    masked = _mask(data=msg)
    blocks = masked["content"]
    assert blocks[0]["type"] == "text"
    assert blocks[0]["text"].startswith("[redacted")
    assert "Andre" not in blocks[0]["text"]
    # Non-text blocks pass through untouched.
    assert blocks[1] == msg["content"][1]


def test_mask_leaves_system_and_assistant_untouched():
    from langfuse_client import _mask
    payload = {
        "messages": [
            {"role": "system", "content": "You are a rehab agent. Be safe."},
            {"role": "user", "content": "my pain is 7"},
            {"role": "assistant", "content": "Let me look at your protocol."},
        ],
    }
    masked = _mask(data=payload)
    assert masked["messages"][0] == payload["messages"][0]
    assert masked["messages"][1]["content"].startswith("[redacted")
    assert masked["messages"][2] == payload["messages"][2]


def test_mask_walks_nested_structures():
    """The mask must recurse into arbitrary list/dict shapes (e.g., the
    full Langfuse trace payload, which can be deeply nested)."""
    from langfuse_client import _mask
    payload = {
        "trace": {
            "input": {
                "messages": [
                    {"role": "user", "content": "knee pain"},
                ],
            },
        },
    }
    masked = _mask(data=payload)
    inner = masked["trace"]["input"]["messages"][0]
    assert inner["content"].startswith("[redacted")


def test_mask_never_raises():
    """Even pathological inputs must not propagate exceptions."""
    from langfuse_client import _mask
    assert _mask(data=None) is None
    assert _mask(data="just a string") == "just a string"
    assert _mask(data=123) == 123
    # Recursive dict (would loop on naive walk) - we keep the implementation
    # simple and accept that recursive-detected inputs are rare; confirm we
    # at least don't crash on a list-of-list-of-dict.
    pathological = [[{"role": "user", "content": "x"}]]
    out = _mask(data=pathological)
    assert isinstance(out, list)


# ---------------------------------------------------------------------------
# session_id hashing is stable + irreversible
# ---------------------------------------------------------------------------

def test_hash_uid_is_stable_and_short():
    from langfuse_client import _hash_uid
    uid = "11111111-1111-1111-1111-111111111111"
    h1 = _hash_uid(uid)
    h2 = _hash_uid(uid)
    assert h1 == h2
    assert len(h1) == 16
    assert h1 != uid


def test_hash_uid_handles_none():
    from langfuse_client import _hash_uid
    assert _hash_uid(None) is None
    assert _hash_uid("") is None
