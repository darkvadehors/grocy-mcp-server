import base64
import os

import pytest

from grocy_mcp.crypto import CryptoError, SecretBox, generate_key, load_key


def box():
    return SecretBox(os.urandom(32))


def test_roundtrip():
    b = box()
    blob = b.seal("cle-grocy", "grocy-user:3")
    assert b"cle-grocy" not in blob
    assert b.open(blob, "grocy-user:3") == "cle-grocy"


def test_nonce_is_unique():
    b = box()
    assert b.seal("x", "a") != b.seal("x", "a")


def test_associated_data_is_enforced():
    b = box()
    blob = b.seal("cle", "grocy-user:3")
    with pytest.raises(CryptoError):
        b.open(blob, "grocy-user:4")


def test_wrong_key_and_tampering():
    blob = box().seal("cle", "a")
    with pytest.raises(CryptoError):
        box().open(blob, "a")
    b = box()
    good = bytearray(b.seal("cle", "a"))
    good[-1] ^= 1
    with pytest.raises(CryptoError):
        b.open(bytes(good), "a")
    with pytest.raises(CryptoError):
        b.open(b"court", "a")


def test_load_key(tmp_path):
    p = tmp_path / "mcp-key"
    p.write_text(generate_key() + "\n")
    assert len(load_key(p)) == 32
    p.write_text(base64.b64encode(b"trop court").decode())
    with pytest.raises(CryptoError, match="32 octets"):
        load_key(p)
    p.write_text("pas du base64 !!")
    with pytest.raises(CryptoError):
        load_key(p)


def test_repr_does_not_leak_key():
    key = os.urandom(32)
    assert key.hex() not in repr(SecretBox(key))
