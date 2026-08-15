from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
import jwt


@dataclass(frozen=True)
class AdminClaims:
    subject: str
    email: str | None
    role: str


class SupabaseJWTVerifier:
    def __init__(
        self,
        supabase_url: str,
        *,
        audience: str = "authenticated",
        admin_role: str = "admin",
        jwks_ttl_seconds: float = 300,
    ) -> None:
        self.issuer = f"{supabase_url.rstrip('/')}/auth/v1"
        self.jwks_url = f"{self.issuer}/.well-known/jwks.json"
        self.audience = audience
        self.admin_role = admin_role
        self.jwks_ttl_seconds = jwks_ttl_seconds
        self._keys: dict[str, Any] = {}
        self._keys_loaded_at = 0.0

    async def verify(self, token: str) -> AdminClaims:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ValueError("Invalid administrator session") from exc
        kid = header.get("kid")
        if not isinstance(kid, str):
            raise ValueError("JWT has no key identifier")
        await self._refresh_keys_if_needed()
        key = self._keys.get(kid)
        if key is None:
            await self._refresh_keys(force=True)
            key = self._keys.get(kid)
        if key is None:
            raise ValueError("JWT signing key is unknown")
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[header.get("alg", "")],
                audience=self.audience,
                issuer=self.issuer,
            )
        except jwt.PyJWTError as exc:
            raise ValueError("Invalid administrator session") from exc
        role = claims.get("app_metadata", {}).get("role")
        if role != self.admin_role:
            raise PermissionError("Administrator role required")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject:
            raise ValueError("JWT subject is missing")
        return AdminClaims(subject, claims.get("email"), role)

    async def _refresh_keys_if_needed(self) -> None:
        if monotonic() - self._keys_loaded_at >= self.jwks_ttl_seconds:
            await self._refresh_keys()

    async def _refresh_keys(self, *, force: bool = False) -> None:
        if not force and monotonic() - self._keys_loaded_at < self.jwks_ttl_seconds:
            return
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.jwks_url)
            response.raise_for_status()
        keys = response.json().get("keys", [])
        self._keys = {
            item["kid"]: jwt.PyJWK.from_dict(item).key
            for item in keys
            if isinstance(item, dict) and isinstance(item.get("kid"), str)
        }
        self._keys_loaded_at = monotonic()
