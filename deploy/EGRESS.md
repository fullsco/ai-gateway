# Upstream Egress: WAF / Cloudflare Challenge Recovery

## Symptom

Every inference request fails with HTTP 502 and category
`upstream_waf_rejection`. Gateway logs look deceptively healthy:

```
"HTTP Request: POST https://agentrouter.org/v1/messages?beta=true \"HTTP/1.1 200 OK\""
"path":"/v1/messages","status_code":502
```

The upstream returns **HTTP 200** but the body is an HTML CAPTCHA page, not
JSON. `_valid_upstream_json()` in `gateway/api/executor.py` rejects it (which
is correct behaviour), retries every credential, and returns 502.

Confirm with:

```bash
curl -s -o /tmp/b.txt -w '%{http_code}\n' -X POST https://agentrouter.org/v1/messages \
  -H 'content-type: application/json' -d '{}'
grep -c aliyun_waf /tmp/b.txt   # >0 means Aliyun WAF challenge
```

## Root cause

Two independent layers, both unrelated to gateway code:

1. **Edge protection challenges the host's egress IP.** This box egresses from
   an AWS range (`100.62.162.5`, `ec2-*.compute-1.amazonaws.com`). Aliyun WAF
   (AgentRouter) serves a sliding-CAPTCHA page. Verified blanket, not
   path-scoped: `POST /zzz-nonexistent` is challenged too. TLS fingerprint is
   NOT the trigger — Chrome/Safari/Edge/Firefox impersonation all get
   challenged, and a real headless Chromium session also fails.
   `HEAD` appears to "pass" only because a bodyless response has nowhere to
   inject the CAPTCHA HTML; it reaches the backend but cannot validate keys.

2. **AgentRouter additionally requires the Claude Code wire image.** Without
   it: `401 unauthorized client detected`. With `user-agent: claude-cli/...`
   plus `x-app: cli`: authentication succeeds.

## A separate, second bug: missing User-Agent (GoRouter / TabiAi)

GoRouter and TabiAi are **not** IP-blocked. They sit behind Cloudflare bot
rules that reject **generic HTTP-library User-Agents**. httpx sends
`python-httpx/<version>` by default, which draws a `Just a moment...` HTML
challenge → fails `_valid_upstream_json()` → HTTP 502.

Confirmed by ablation on a single variable (direct connection, no proxy):

| User-Agent | Result |
|---|---|
| `python-httpx/0.28.1` (httpx default) | BLOCKED (Cloudflare) |
| `python-requests/2.32.0` | BLOCKED |
| `Go-http-client/2.0`, `node-fetch/1.0` | BLOCKED |
| `curl/8.5.0`, omitted entirely | BLOCKED |
| Chrome browser UA | BLOCKED (JS challenge) |
| `ai-gateway/0.1`, `foo/1.0`, `claude-cli/...` | **PASS** |

The trigger is the *generic library* UA, not the absence of a browser UA.

`AnthropicCompatibleAdapter` already set `DEFAULT_USER_AGENT = "ai-gateway/0.1"`.
`OpenAICompatibleAdapter` did **not**. So on the same host,
`anthropic_messages` routes worked while `openai_chat_completions` routes were
hard-blocked — including hcnsec's `DeepSeek-V4-Pro` and AgentRouter's
`gpt-5.6-sol`. Fixed by giving the OpenAI adapter the same default; provider
`default_headers` still override it.

Regression cover: `tests/test_provider_user_agent.py`.

Note: GoRouter/TabiAi also return an intermittent
`403 Access denied: abusive or non-compliant use is prohibited`. It appears
randomly on both the AWS and WARP egress IPs, interleaved with successful
`401`s, and is not removed by spacing requests — an upstream anti-abuse
sampling rule (these probes used a deliberately invalid key), not a network
path problem.

## Fix

### 1. Clean egress via Cloudflare WARP (proxy mode, no tunnel)

Proxy mode exposes a local SOCKS5 port and does **not** alter system routing,
so it does not interfere with a cloudflared tunnel used elsewhere.

```bash
curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg \
  | sudo gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] \
https://pkg.cloudflareclient.com/ noble main" \
  | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
sudo apt-get update -qq && sudo apt-get install -y cloudflare-warp

sudo systemctl enable --now warp-svc
warp-cli --accept-tos registration new
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect
```

Ubuntu releases newer than `noble` have no WARP repo yet — pin `noble`.

Verify a clean IP and that the WAF is gone:

```bash
curl -s --socks5-hostname 127.0.0.1:40000 https://api.ipify.org   # not an AWS IP
```

### 2. Point the gateway at the proxy

`.env`:

```
GATEWAY_UPSTREAM_PROXY_URL=socks5h://127.0.0.1:40000
```

`socks5h` resolves DNS at the proxy, so upstream hostname lookups don't leak
and don't resolve to a challenged region. Requires `httpx[socks]`
(`socksio`, pinned in `requirements.lock`).

Implementation: `gateway/providers/egress.py` — `build_upstream_client()` is
used by `RuntimeBuilder`, so both inference traffic and health probes
(which reuse `runtime.http_client`) egress through the proxy.

Setting an explicit proxy also sets `trust_env=False` so ambient `*_proxy`
variables can't silently override it.

### 3. Per-provider Claude Code wire image (AgentRouter only)

Set the provider's `default_headers` in the control plane:

```json
{ "user-agent": "claude-cli/1.0.60 (external, cli)", "x-app": "cli" }
```

`runtime_builder` already validates that `default_headers` cannot contain
credential headers, so this is safe to store.

## Verification ladder

| Upstream response | Meaning |
|---|---|
| HTML + `aliyun_waf` | Still IP-challenged; proxy not in effect |
| `401 unauthorized client detected` | Proxy works; wire image missing |
| `403 user quota is not enough` | Proxy + wire image OK; **billing/quota** |
| `503 无可用渠道` | Model name not served for this key/group |
| `200` + `content[]` | Fully working |

Reproduce the gateway's own path without touching the database:
build a `ProviderConfig` + `AnthropicCompatibleAdapter`, send via
`build_upstream_client()`, then assert `_valid_upstream_json(body)`.

## Pitfalls

- **A 200 status does not mean success.** Always inspect the body; WAF pages
  are served with 200.
- **Do not test the key against `co.agentrouter.org`.** It resolves to a
  different ALB with no WAF but a separate tenant, so valid keys report
  `Invalid API Key!`. Use the base URL the key was issued for.
- `/tmp` is a 476MB tmpfs; `pip install` can fail with
  `OSError: [Errno 122] Disk quota exceeded`. Use `TMPDIR=~/.piptmp`.
- Model IDs here are dash-form (`claude-opus-4-8`), not dot-form
  (`claude-opus-4.8`) — the latter returns `503 无可用渠道`.
- `tests/test_deployment.py` expects the cloudflared `--token-file` + docker
  `secrets` form. Running without a tunnel leaves that test failing; it is
  unrelated to egress.
