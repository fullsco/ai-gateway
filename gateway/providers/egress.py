"""Factory for the HTTP client used to reach upstream AI providers.

Some upstream providers sit behind aggressive edge protection (Aliyun WAF,
Cloudflare) that challenges requests based on the *source IP* of the caller.
When the gateway runs in a datacenter whose egress range is challenged, every
upstream POST comes back as an HTML interstitial with HTTP 200, which the
executor correctly rejects as an invalid upstream response
(`UPSTREAM_WAF_REJECTION`) and surfaces to clients as HTTP 502.

Routing upstream traffic through an egress proxy with a clean reputation fixes
this without changing any provider or protocol behaviour. Configure it with:

    GATEWAY_UPSTREAM_PROXY_URL=socks5h://127.0.0.1:40000

`socks5h://` is preferred so DNS resolution also happens at the proxy, which
keeps the gateway from leaking lookups for upstream hostnames.
"""

from __future__ import annotations

import httpx

from gateway.config import Settings, get_settings

__all__ = ["build_upstream_client", "upstream_client_kwargs"]


def upstream_client_kwargs(settings: Settings | None = None) -> dict[str, object]:
    """Return httpx keyword arguments implementing the configured egress policy."""
    resolved = settings or get_settings()
    kwargs: dict[str, object] = {"trust_env": True}
    proxy = resolved.upstream_proxy_url
    if proxy:
        # httpx routes every scheme through this proxy, including CONNECT for https.
        kwargs["proxy"] = proxy
        kwargs["trust_env"] = False
        if not resolved.upstream_proxy_verify_tls:
            kwargs["verify"] = False
    return kwargs


def build_upstream_client(
    settings: Settings | None = None,
    **overrides: object,
) -> httpx.AsyncClient:
    """Create an AsyncClient that honours the configured upstream egress proxy."""
    kwargs = upstream_client_kwargs(settings)
    kwargs.update(overrides)
    return httpx.AsyncClient(**kwargs)  # type: ignore[arg-type]
