import base64

import pytest

from gateway.security.credentials import CredentialCipher, CredentialDecryptionError


def test_credential_envelope_round_trip_binds_context() -> None:
    cipher = CredentialCipher(b"a" * 32)

    envelope = cipher.encrypt("upstream-secret", context="provider:credential-1")

    assert cipher.decrypt(envelope, context="provider:credential-1") == "upstream-secret"
    with pytest.raises(CredentialDecryptionError):
        cipher.decrypt(envelope, context="provider:credential-2")


def test_credential_cipher_accepts_valid_base64_master_key() -> None:
    encoded_key = base64.b64encode(b"b" * 32).decode("ascii")

    assert isinstance(CredentialCipher.from_base64(encoded_key), CredentialCipher)


def test_credential_cipher_rejects_invalid_master_key() -> None:
    with pytest.raises(ValueError, match="valid base64"):
        CredentialCipher.from_base64("not base64")
    with pytest.raises(ValueError, match="32 bytes"):
        CredentialCipher(b"short")
