"""Tests for user_store payer-model resolver + clinician setter.

payer_model is clinician-owned, stored on the canonical intake payload, and
defaults to "cash" (the insurance-lapse-bridge GTM is cash-pay first). These
tests pin the default, the enum guard, and the set/resolve roundtrip.
"""
from __future__ import annotations

import uuid

import pytest


def test_resolve_defaults_to_cash_for_empty_token():
    import user_store
    assert user_store.resolve_payer_model("") == "cash"
    assert user_store.resolve_payer_model(None) == "cash"


def test_set_payer_model_rejects_unknown_value():
    import user_store
    with pytest.raises(ValueError):
        user_store.set_payer_model("tok", "self-pay")


def test_set_and_resolve_roundtrip_preserves_intake():
    import user_store
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "P", "injury_type": "knee"})
    try:
        # Default before any set.
        assert user_store.resolve_payer_model(token) == "cash"
        # Case-insensitive normalization on set.
        assert user_store.set_payer_model(token, "Insurance") == "insurance"
        assert user_store.resolve_payer_model(token) == "insurance"
        # Set must not clobber the rest of the intake payload.
        intake = user_store.get_intake(token)
        assert intake["name"] == "P"
        assert intake["injury_type"] == "knee"
        assert intake["payer_model"] == "insurance"
    finally:
        user_store.delete_intake(token)


def test_resolve_falls_back_when_payer_model_absent():
    import user_store
    token = str(uuid.uuid4())
    user_store.ensure_user(token)
    user_store.save_intake(token, {"name": "Q"})
    try:
        assert user_store.resolve_payer_model(token) == "cash"
    finally:
        user_store.delete_intake(token)
