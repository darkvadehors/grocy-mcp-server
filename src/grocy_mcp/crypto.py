"""Chiffrement AES-GCM des clés API Grocy (clé maître fournie par systemd LoadCredential)."""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
_NONCE_BYTES = 12
_TAG_BYTES = 16


class CryptoError(Exception):
    """Clé maître invalide ou donnée chiffrée illisible."""


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode()


def load_key(path: Path) -> bytes:
    try:
        key = base64.b64decode(path.read_bytes().strip(), validate=True)
    except (OSError, binascii.Error):
        raise CryptoError("clé de chiffrement illisible") from None
    if len(key) != KEY_BYTES:
        raise CryptoError("la clé de chiffrement doit faire 32 octets (en base64)")
    return key


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._aead = AESGCM(key)

    def __repr__(self) -> str:
        return "SecretBox(<clé masquée>)"

    def seal(self, plaintext: str, aad: str) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), aad.encode())

    def open(self, blob: bytes, aad: str) -> str:
        if len(blob) < _NONCE_BYTES + _TAG_BYTES:
            raise CryptoError("donnée chiffrée invalide")
        try:
            return self._aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], aad.encode()).decode()
        except InvalidTag:
            raise CryptoError("déchiffrement impossible") from None
