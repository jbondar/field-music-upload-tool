"""Allowlist and invite-code behaviour."""

import pytest

from app.invites import AccessStore, RedeemError, normalize_code


def store(tmp_path, seed=()):
    return AccessStore(tmp_path, list(seed))


def test_seeded_emails_are_allowed(tmp_path):
    access = store(tmp_path, ["Jake@Example.com"])
    assert access.is_allowed("jake@example.com")
    assert not access.is_allowed("stranger@example.com")


def test_redeeming_a_code_grants_access(tmp_path):
    access = store(tmp_path, ["admin@example.com"])
    invite = access.create_invite("admin@example.com")
    assert not access.is_allowed("friend@example.com")
    access.redeem(invite.code, "friend@example.com")
    assert access.is_allowed("friend@example.com")


def test_single_use_code_cannot_be_reused(tmp_path):
    access = store(tmp_path, ["admin@example.com"])
    invite = access.create_invite("admin@example.com", uses=1)
    access.redeem(invite.code, "one@example.com")
    with pytest.raises(RedeemError):
        access.redeem(invite.code, "two@example.com")
    assert not access.is_allowed("two@example.com")


def test_multi_use_code(tmp_path):
    access = store(tmp_path, [])
    invite = access.create_invite("admin@example.com", uses=2)
    access.redeem(invite.code, "one@example.com")
    access.redeem(invite.code, "two@example.com")
    with pytest.raises(RedeemError):
        access.redeem(invite.code, "three@example.com")


def test_expired_code_is_rejected(tmp_path):
    access = store(tmp_path, [])
    invite = access.create_invite("admin@example.com", expires_in_days=-1)
    with pytest.raises(RedeemError):
        access.redeem(invite.code, "friend@example.com")


def test_revoked_code_is_rejected(tmp_path):
    access = store(tmp_path, [])
    invite = access.create_invite("admin@example.com")
    assert access.revoke_invite(invite.code)
    with pytest.raises(RedeemError):
        access.redeem(invite.code, "friend@example.com")


def test_unknown_code_is_rejected(tmp_path):
    access = store(tmp_path, [])
    with pytest.raises(RedeemError):
        access.redeem("ZZZZ-9999", "friend@example.com")


def test_access_survives_restart(tmp_path):
    access = store(tmp_path, [])
    invite = access.create_invite("admin@example.com")
    access.redeem(invite.code, "friend@example.com")

    reopened = store(tmp_path, [])
    assert reopened.is_allowed("friend@example.com")
    # And the spent code is still spent.
    with pytest.raises(RedeemError):
        reopened.redeem(invite.code, "other@example.com")


def test_revoking_an_email(tmp_path):
    access = store(tmp_path, [])
    invite = access.create_invite("admin@example.com")
    access.redeem(invite.code, "friend@example.com")
    assert access.revoke("friend@example.com")
    assert not access.is_allowed("friend@example.com")


def test_code_normalisation_is_forgiving():
    assert normalize_code("abcd2345") == "ABCD-2345"
    assert normalize_code("abcd 2345") == "ABCD-2345"
    assert normalize_code("ABCD-2345") == "ABCD-2345"
