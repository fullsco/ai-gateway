"""Tests for upstream egress proxy support.

Regression cover for the outage where provider edge protection (Aliyun WAF)
challenged this host's datacenter egress IP and returned HTML with HTTP 200,
which the executor rejected as UPSTREAM_WAF_REJECTION -> client-visible 502.
"""

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.providers.egress import build_upstream_client, upstream_client_kwargs


def test_no_proxy_configured_keeps_trust_env() -> None:
    settings = Settings(upstream_proxy_url=None)
    kwargs = upstream_client_kwargs(settings)
    assert kwargs == {"trust_env": True}
    assert "proxy" not in kwargs


def test_socks_proxy_is_applied_and_env_is_ignored() -> None:
    settings = Settings(upstream_proxy_url="socks5h://127.0.0.1:40000")
    kwargs = upstream_client_kwargs(settings)
    assert kwargs["proxy"] == "socks5h://127.0.0.1:40000"
    # An explicit proxy must win over ambient *_proxy environment variables.
    assert kwargs["trust_env"] is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "https://proxy.internal:3128",
        "socks5://127.0.0.1:40000",
        "socks5h://127.0.0.1:40000",
    ],
)
def test_supported_proxy_schemes_are_accepted(url: str) -> None:
    assert Settings(upstream_proxy_url=url).upstream_proxy_url == url


@pytest.mark.parametrize("url", ["ftp://p:21", "tcp://p:1", "127.0.0.1:40000"])
def test_unsupported_proxy_schemes_are_rejected(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(upstream_proxy_url=url)


def test_blank_proxy_is_treated_as_unset() -> None:
    # Empty env values (GATEWAY_UPSTREAM_PROXY_URL=) must not configure a proxy.
    settings = Settings(upstream_proxy_url="   ")
    assert settings.upstream_proxy_url is None
    assert "proxy" not in upstream_client_kwargs(settings)


def test_tls_verification_can_be_disabled_only_with_a_proxy() -> None:
    with_proxy = Settings(
        upstream_proxy_url="http://127.0.0.1:8080",
        upstream_proxy_verify_tls=False,
    )
    assert upstream_client_kwargs(with_proxy)["verify"] is False

    without_proxy = Settings(upstream_proxy_url=None, upstream_proxy_verify_tls=False)
    assert "verify" not in upstream_client_kwargs(without_proxy)


def test_verification_stays_enabled_by_default() -> None:
    settings = Settings(upstream_proxy_url="socks5h://127.0.0.1:40000")
    assert settings.upstream_proxy_verify_tls is True
    assert "verify" not in upstream_client_kwargs(settings)


def test_build_upstream_client_returns_async_client_and_honours_overrides() -> None:
    client = build_upstream_client(
        Settings(upstream_proxy_url=None),
        timeout=httpx.Timeout(12),
    )
    assert isinstance(client, httpx.AsyncClient)
    assert client.timeout.connect == 12


def test_socks_extra_is_installed() -> None:
    # socks5h:// silently fails at runtime without httpx[socks].
    import socksio  # noqa: F401
