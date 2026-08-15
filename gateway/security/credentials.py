import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CredentialDecryptionError(ValueError):
    """Raised when an encrypted credential cannot be authenticated or decrypted."""


@dataclass(frozen=True)
class EncryptedCredential:
    version: int
    nonce: str
    ciphertext: str


class CredentialCipher:
    VERSION = 1

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("Credential encryption master key must be exactly 32 bytes")
        self._cipher = AESGCM(master_key)

    @classmethod
    def from_base64(cls, encoded_key: str) -> "CredentialCipher":
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except binascii.Error as exc:
            raise ValueError("Credential encryption master key must be valid base64") from exc
        return cls(key)

    def encrypt(self, secret: str, *, context: str) -> EncryptedCredential:
        if not secret:
            raise ValueError("Credential secret must not be empty")
        if not context:
            raise ValueError("Credential encryption context must not be empty")
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(nonce, secret.encode("utf-8"), context.encode("utf-8"))
        return EncryptedCredential(
            version=self.VERSION,
            nonce=base64.b64encode(nonce).decode("ascii"),
            ciphertext=base64.b64encode(ciphertext).decode("ascii"),
        )

    def decrypt(self, envelope: EncryptedCredential, *, context: str) -> str:
        if envelope.version != self.VERSION:
            raise CredentialDecryptionError("Unsupported credential encryption version")
        if not context:
            raise ValueError("Credential encryption context must not be empty")
        try:
            nonce = base64.b64decode(envelope.nonce, validate=True)
            ciphertext = base64.b64decode(envelope.ciphertext, validate=True)
            plaintext = self._cipher.decrypt(nonce, ciphertext, context.encode("utf-8"))
        except (InvalidTag, ValueError, binascii.Error) as exc:
            raise CredentialDecryptionError("Credential decryption failed") from exc
        return plaintext.decode("utf-8")
