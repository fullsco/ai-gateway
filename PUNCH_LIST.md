# Punch list

What has been fixed, what is deliberately left alone, and what is still open. The
P0 to P5 tiers were tracked in conversation for most of this project, which meant
they existed nowhere a second person could read them. That is the gap this file
closes.

Tiers describe urgency at the time the work was scheduled, not difficulty. Every
closed item names the commit, so the reasoning is one `git show` away: commit
messages carry the evidence and the numbers, and are the primary record.

Statuses are **fixed**, **decided** (deliberately not changed, with a reason),
**open**, or **blocked** (cannot proceed until something outside the code changes).

---

## P0 to P2 — correctness of what the dashboard claims

| Item | Status | Commit |
|---|---|---|
| Usage, cost and limits meant different things in different views | fixed | `e935645` |
| Streamed tokens were discarded, and unmeasured traffic was priced anyway | fixed | `e8e4e2e` |
| Provider cost was asserted rather than measured | fixed | `8b2881c` |
| Cache read and cache write rates were defaulted, not measured | fixed | `c1b0532`, `1c80ddd` |
| Saving model routing silently narrowed and disabled live routes | fixed | `f3441ed` |
| A provider fault marked the credential unhealthy | fixed | `5295daf` |
| Errors did not say what actually failed | fixed | `49d1b13` |
| A budget limit did not mean the amount it said: reservations over-reserved 1.88x | fixed | `26aad0f` |
| Configuration was not legible without reading SQL | fixed | `c1500fd` |
| Alerting could not describe a condition, or close one | fixed | `9d9b6c0`, `891ae36` |
| Operator acceptance checklist did not exist | fixed | `7c8b779` |

## P3 to P4 — legibility and suite health

| Item | Status | Commit |
|---|---|---|
| No way to see why a request was routed as it was | fixed | `31c133c` |
| Gateway keys were unlabelled, so nobody knew what used them | fixed | `f958d3f` |
| Four long-standing test failures hid real regressions | fixed | `84b992f` |
| The operations script lived outside the repository | fixed | `31c133c` |
| `gpt-5.6-sol` was unpriced; an unpriced route could take a model offline | fixed | `cb19ff4` |

## P5 — final pre-launch hardening

### Operator-facing correctness

| Item | Status | Commit |
|---|---|---|
| Five different problems all reported as 401 "Invalid gateway key", including a client simply not permitted a protocol | fixed | `ce93d4f` |
| Credentials and Providers counted only healthy credentials, showing 1 usable where the router could use 17 | fixed | `dcb50b5` |
| A disabled client kept serving traffic, because access is enforced from the published snapshot | fixed | `fa2ae82` |
| OpenCode's `gateway-openai` provider could not authenticate | fixed by operator | consolidated onto the `Opencode` client, which permits both protocols |

### Availability

| Item | Status | Commit |
|---|---|---|
| A provider-scoped failure retired only the credential, so one provider consumed every attempt | fixed | `8f85161` |
| An unhealthy credential could never recover, because health is restored only by a success it could never earn | fixed | `16ff688` |
| Only rate limits set a cooldown, so every other failure state was stranded permanently | fixed | `87e3c84` |
| Eight AgentRouter credentials were parked as auth failures; four of them worked | fixed | `87e3c84` |
| The pool alert fired only at zero routable credentials, which is already an outage | fixed | `8cafbc9` |
| `claude-opus-5` has no cross-provider fallback | fixed | pricing first, then fallback; see "Blocked" |

### Robustness

| Item | Status | Commit |
|---|---|---|
| An upstream truncating a stream escaped as an unhandled ASGI exception, and was recorded as a client cancellation | fixed | `048dd7d` |
| A slow route could hold an attempt for the provider's full 600s | fixed | `6417fa3` |
| A stream arriving in the wrong protocol was relayed, and its tokens silently resolved to none | fixed | `aaa334e` |
| The OpenAI adapter sent no user-agent, so Cloudflare refused it as a generic library | fixed | `fed9eab` |

---

## Decided, and deliberately not changed

- **`claude-sonnet-5` and `kimi-k3` do not resolve.** They are different models from
  anything configured. Aliasing them to an Opus mapping would answer with a model
  nobody asked for. They fail with a clear 404 on purpose.
- **`glm-5.2` is disabled, not deleted.** hcnsec stopped serving it. `request_attempts`
  references the mapping with `on delete set null`, so deleting it would blank the
  provider on 32 historical attempts and cut the link to the billing evidence
  recorded against that route. Commit `a3988f4`.
- **hcnsec is exposed as `nemotron-3-ultra`, not `deepseek-v4-pro`.** Asking hcnsec
  for `DeepSeek-V4-Pro` returns `nvidia/nemotron-3-ultra-550b-a55b` on every
  successful sample. The canonical model is named for what answers; the upstream id
  stays `DeepSeek-V4-Pro` because that is the only string hcnsec routes. Commit
  `a3988f4`.
- **`nemotron-3-ultra` is unpriced, not priced at zero.** hcnsec publishes no rate
  card, so its flat fee cannot be corroborated. A missing cost is visible as missing;
  a zero is a lie. GoRouter and TabiAi are now priced from their published cards.
- **Credential recovery is lazy, not proactive.** A credential on trial is only tried
  when the healthy one is unavailable, so the pool heals under pressure rather than
  in advance. Scoring already ranks a trial below a healthy credential, so recovery
  costs no throughput. The alternative, probing on a timer, spends money to discover
  something the next real failure discovers for free.

---

## Blocked

Nothing is currently blocked. The two items that were, pricing for the flat-fee
providers and the `claude-opus-5` fallback that waited on it, are recorded below.

### Resolved: pricing for the flat-fee providers

Three of four providers bill a **flat fee per request**. This was first read as
unpriceable, on two grounds that both turned out to be answerable.

The first was whether the fee is genuinely flat or merely a coarse minimum. Every
early sample was small, 138 to 8,783 input tokens, and a minimum charge looks
identical inside a narrow range. Widening it settled the question: 7,185 and 246,190
input tokens moved TabiAi's counter by exactly the same 80, a 34x increase in size
for no change in charge. It is flat, so a per-token rate cannot describe it, which is
why `per_request` now exists as a third pricing shape. 

The second was the unit. All three providers publish a rate card at `/api/pricing`
using the one-api convention: `quota_type` 1 is a flat `model_price` in USD with
`model_ratio` 0, and `quota_type` 0 is a per-token ratio. The listed prices match the
measured counter deltas exactly at cent scale:

| Provider | Listed | Measured delta | Agrees at cent scale |
|---|---|---|---|
| TabiAi `claude-opus-5` | 0.8 | 80 | yes |
| GoRouter `claude-opus-5` | 0.3 | 30 | yes |

The same convention independently validates the AgentRouter rates measured months
earlier: its card lists `claude-opus-5` at ratio 1 with completion ratio 5, which at
the family's $2 per million base is $2 in and $10 out, exactly as measured, and
`gpt-5.6-sol` at ratio 2, so $4 and $20, also exactly as measured. Two independent
derivations agreeing is the strongest evidence this project holds for any rate.

`nemotron-3-ultra` on hcnsec is still unpriced: its `/api/pricing` returns an empty
catalogue, so there is nothing to corroborate its 640.94 counter delta against.

### Resolved: `claude-opus-5` cross-provider fallback

`allow_model_fallback` is now true on the AgentRouter and GoRouter routes and false on
TabiAi, which is last and has nothing after it. This mirrors how
`claude-opus-5-thinking` was already configured. Routing now attempts
`AgentRouter, GoRouter, TabiAi` instead of AgentRouter three times.

It was gated on pricing by standing decision, and that gate held: the routes were
priced first, so a flat-fee fallback records $0.80 rather than nothing. Verified on
live traffic, where GoRouter failed and TabiAi served the request at exactly
$0.80000000 USD.

Note that the high-level `PUT /models/{id}/routing` endpoint cannot set this field. It
writes `allow_model_fallback` false on insert and does not update it on conflict, so
the low-level `/routes/{id}` endpoint is the only way, and `policy_id` and `pool_id`
must be read back and preserved or a routing policy is silently lost.

---

## Decided during production preparation

- **The AWS host runs under systemd, keeps its WARP proxy, and shares one control
  plane with development.** A single Supabase project serves both, on the current plan,
  so a configuration publish is live in both places within seconds and both spend the
  same budget. Accepted deliberately. The consequence is that both hosts must run
  compatible code, which is why unknown snapshot fields are now ignored rather than
  rejected.
- **The Aliyun WAF challenge on AgentRouter is caused by the missing user-agent, not by
  the egress IP.** Measured direct from AWS with the mapping's headers: three of three
  clean JSON responses. Without a user-agent, direct: a 15,999 byte Aliyun WAF page
  returned as HTTP 200. The WARP proxy is kept as insurance, because Aliyun rules are
  reputation-based and a datacenter range can be challenged later, but it is not the
  remedy and must not be relied on as one. See deploy/EGRESS.md.
- **A user-agent problem has now been misread as an IP block three times**, on
  AgentRouter, GoRouter and TabiAi, because curl and urllib are refused for the same
  reason the gateway was. Any future "provider blocks our IP" claim should be tested
  with the mapping's own default_headers before the network is blamed.

## Open

| Item | Why it matters |
|---|---|
| Seven AgentRouter credentials still need a human | Two are out of quota and will clear. One needs this host's egress IP on the token's allow list. One needs model entitlement. Each carries a `note` saying which. |
| GoRouter is intermittently unreachable | Connection resets interleave with successful requests from the same host and credential. It works often enough to serve as a fallback, but not reliably enough to be a primary. |
| Credential recovery is unproven end to end | The mechanism is proven at engine level and by tests, but no trial has been observed completing on live traffic, because the healthy credential keeps succeeding. |
| `nemotron-3-ultra` is still unpriced | hcnsec publishes an empty `/api/pricing`, so its flat 640.94 counter delta cannot be corroborated. It is the only unpriced route left. |
| `gpt-5.6-sol` has one provider and no fallback | AgentRouter only. It can report no eligible route when that pool is momentarily exhausted. GoRouter and TabiAi do not list it. |
