import base64
import binascii
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime

KEY_PREFIX = "gw_live_"


@dataclass(frozen=True)
class GatewayKey:
    id: str
    client_id: str
    key_prefix: str
    digest: str
    enabled: bool = True
    expires_at: datetime | None = None


@dataclass(frozen=True)
class IssuedGatewayKey:
    record: GatewayKey
    plaintext: str


class GatewayKeyHasher:
    def __init__(self, pepper: bytes) -> None:
        if len(pepper) < 32:
            raise ValueError("Gateway key pepper must be at least 32 bytes")
        self._pepper = pepper

    @classmethod
    def from_base64(cls, value: str) -> "GatewayKeyHasher":
        try:
            pepper = base64.b64decode(value, validate=True)
        except binascii.Error as exc:
            raise ValueError("Gateway key pepper must be valid base64") from exc
        return cls(pepper)

    def issue(self, *, key_id: str, client_id: str) -> IssuedGatewayKey:
        token = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
        return IssuedGatewayKey(
            record=GatewayKey(
                id=key_id,
                client_id=client_id,
                key_prefix=token[:16],
                digest=self.digest(token),
            ),
            plaintext=token,
        )

    def digest(self, token: str) -> str:
        if not token.startswith(KEY_PREFIX):
            raise ValueError("Gateway key has an invalid prefix")
        return hmac.new(self._pepper, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, token: str, record: GatewayKey) -> bool:
        if not record.enabled:
            return False
        if record.expires_at is not None and record.expires_at <= datetime.now(UTC):
            return False
        try:
            candidate = self.digest(token)
        except ValueError:
            return False
        return hmac.compare_digest(candidate, record.digest)
