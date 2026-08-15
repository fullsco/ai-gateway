import pytest

from gateway.admin.auth import SupabaseJWTVerifier


@pytest.mark.asyncio
async def test_malformed_admin_token_is_rejected() -> None:
    verifier = SupabaseJWTVerifier("https://example.supabase.co")

    with pytest.raises(ValueError, match="Invalid administrator session"):
        await verifier.verify("invalid")


def test_verifier_uses_supabase_auth_issuer_and_jwks_urls() -> None:
    verifier = SupabaseJWTVerifier("https://project.supabase.co")

    assert verifier.issuer == "https://project.supabase.co/auth/v1"
    assert verifier.jwks_url.endswith("/.well-known/jwks.json")


@pytest.mark.asyncio
async def test_token_without_key_identifier_is_rejected() -> None:
    verifier = SupabaseJWTVerifier("https://project.supabase.co")

    with pytest.raises(ValueError, match="key identifier"):
        await verifier.verify("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.invalid")
