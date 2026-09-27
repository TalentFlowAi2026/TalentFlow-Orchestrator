"""Envelope encryption and opaque invitation/OAuth token helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from talentflow_orchestrator.config.settings import Settings
from talentflow_orchestrator.domain.models import ServiceError


@dataclass(frozen=True)
class EncryptedValue:
    ciphertext: bytes
    nonce: bytes
    key_version: str


class SecretCipher:
    def __init__(self, settings: Settings, purpose: str) -> None:
        encoded = settings.data_encryption_key.get_secret_value()
        if not encoded:
            raise ServiceError("data_encryption_key_missing", retryable=False)
        try:
            master = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4),
                altchars=b"-_",
                validate=True,
            )
        except (binascii.Error, ValueError) as exc:
            raise ServiceError("data_encryption_key_invalid", retryable=False) from exc
        if len(master) != 32:
            raise ServiceError("data_encryption_key_invalid", retryable=False)
        self._key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=f"talentflow-orchestrator:{purpose}".encode(),
        ).derive(master)
        self.key_version = settings.data_encryption_key_version
        self._purpose = purpose.encode()

    def encrypt(self, plaintext: str, *, context: bytes) -> EncryptedValue:
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, plaintext.encode(), self._purpose + context)
        return EncryptedValue(ciphertext, nonce, self.key_version)

    def decrypt(self, encrypted: EncryptedValue, *, context: bytes) -> str:
        if encrypted.key_version != self.key_version:
            raise ServiceError("encryption_key_version_unavailable", retryable=False)
        try:
            plaintext = AESGCM(self._key).decrypt(
                encrypted.nonce,
                encrypted.ciphertext,
                self._purpose + context,
            )
        except (InvalidTag, ValueError) as exc:
            raise ServiceError("encrypted_value_invalid", retryable=False) from exc
        return plaintext.decode()


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def new_pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
