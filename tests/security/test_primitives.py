"""Passwords, TOTP (window + replay), backup codes, and encryption of the TOTP secret."""
import pyotp
import pytest

from app.security import crypto, passwords, totp

T0 = 1_800_000_010.0                                      # inside a 30 s step; step number = T0 // 30
STEP = int(T0 // 30)


def code_at(secret, t):
    return pyotp.TOTP(secret).at(t)


# ---- passwords ----
def test_password_hash_is_argon2id_and_salted_and_verifies():
    a, b = passwords.hash_password("correct horse battery"), passwords.hash_password("correct horse battery")
    assert a != b and a.startswith("$argon2id$")
    assert passwords.verify_password(a, "correct horse battery")
    assert not passwords.verify_password(a, "correct horse batterY")


@pytest.mark.parametrize("stored", [None, "", "not-a-hash", "$argon2id$garbage"])
def test_verifying_against_a_missing_or_broken_hash_is_just_false(stored):
    assert passwords.verify_password(stored, "whatever") is False


def test_password_policy():
    assert passwords.policy_error("short") and passwords.policy_error("a" * 12) and passwords.policy_error("x" * 201)
    assert passwords.policy_error("correct horse battery") is None


def test_burn_time_runs_without_error():
    passwords.burn_time("anything")


# ---- TOTP window and replay ----
def test_code_for_the_current_previous_and_next_step_is_accepted():
    s = totp.new_secret()
    for offset in (-1, 0, 1):
        assert totp.match_step(s, code_at(s, T0 + 30 * offset), T0, None) == STEP + offset


@pytest.mark.parametrize("offset", [-2, 2, 5])
def test_code_outside_the_window_is_refused(offset):
    s = totp.new_secret()
    assert totp.match_step(s, code_at(s, T0 + 30 * offset), T0, None) is None


def test_a_step_can_be_used_only_once_and_never_an_older_one():
    s = totp.new_secret()
    code = code_at(s, T0)
    assert totp.match_step(s, code, T0, STEP - 1) == STEP
    assert totp.match_step(s, code, T0, STEP) is None                        # same step again: replay
    assert totp.match_step(s, code_at(s, T0 - 30), T0, STEP) is None         # an older step than the last used
    assert totp.match_step(s, code_at(s, T0 + 30), T0, STEP) == STEP + 1     # the next one is fine


@pytest.mark.parametrize("bad", ["", "12345", "1234567", "abcdef", "12 345", None])
def test_codes_that_are_not_six_digits_are_refused(bad):
    assert totp.match_step(totp.new_secret(), bad, T0, None) is None


def test_setup_qr_is_an_inline_svg_for_an_otpauth_uri():
    s = totp.new_secret()
    uri = totp.provisioning_uri(s, "rahul")
    assert uri.startswith("otpauth://totp/") and f"secret={s}" in uri and "Algo" in uri
    assert totp.qr_svg(uri).lstrip().startswith("<svg")


# ---- backup codes ----
def test_ten_distinct_backup_codes_without_confusable_characters():
    codes = totp.new_backup_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    assert all(len(c) == 8 and not set(c) & set("0o1il") for c in codes)


def test_backup_code_matches_however_it_is_typed():
    c = totp.new_backup_codes()[0]
    h = totp.hash_backup_code(c)
    assert h != c and c not in h
    for typed in (c, totp.display_code(c), totp.display_code(c).upper(), f"  {totp.display_code(c)}  "):
        assert totp.verify_backup_code(h, typed)
    assert not totp.verify_backup_code(h, "aaaa-aaaa")


# ---- encryption of the secret ----
def test_secret_roundtrips_and_is_not_stored_readable():
    secret = totp.new_secret()
    enc = crypto.encrypt(secret)
    assert secret not in enc and crypto.decrypt(enc) == secret


def test_without_a_key_in_the_environment_one_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ENC_KEY")
    monkeypatch.setattr(crypto, "KEY_FILE", tmp_path / "enc.key")
    enc = crypto.encrypt("hello")
    assert (tmp_path / "enc.key").exists() and crypto.decrypt(enc) == "hello"
    assert crypto.decrypt(crypto.encrypt("again")) == "again"               # same key read back, not regenerated
