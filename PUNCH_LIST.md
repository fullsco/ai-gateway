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
| `claude-opus-5` has no cross-provider fallback | blocked | see "Blocked" below |

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
- **GoRouter, TabiAi and hcnsec routes are unpriced, not priced at zero.** See
  "Blocked". A missing cost is visible as missing; a zero is a lie.
- **Credential recovery is lazy, not proactive.** A credential on trial is only tried
  when the healthy one is unavailable, so the pool heals under pressure rather than
  in advance. Scoring already ranks a trial below a healthy credential, so recovery
  costs no throughput. The alternative, probing on a timer, spends money to discover
  something the next real failure discovers for free.

---

## Blocked

### Pricing for GoRouter, TabiAi and hcnsec

Three of four providers bill a **flat fee per request**, not per token. Measured with
a control read confirming the counter is otherwise still:

| Provider | Counter delta per request | Token counts across samples |
|---|---|---|
| hcnsec | 640.94 | 138, 5,963, 413 total |
| TabiAi | 80 | 7185/1, 8783/2, 7412/82 |
| GoRouter | 30 | 7117/1, 8716/2, 7432/188 |

Two things stop this becoming a price:

1. **The unit is ambiguous.** `/v1/dashboard/billing/subscription` reports
   `soft_limit_usd: 100000000` with no scale, and `credit_grants` is 404. TabiAi and
   hcnsec return byte-identical subscription payloads, so they are the same reseller
   software. Both also report a per-request `cost` that varies with tokens, which
   contradicts their own flat counter by between 412x and 931x.
2. **The pricing model cannot express a per-request fee.** `validate_pricing` accepts
   `blended_per_million`, or `input_per_million` with `output_per_million`, and
   nothing else. Recording a flat fee as a per-token rate would misdescribe how the
   provider charges.

Unblocking needs one of: a per-request fee shape in the pricing model plus a resolved
unit; the providers stating their unit; or accepting these routes stay unpriced.

### `claude-opus-5` cross-provider fallback

`allow_model_fallback` is false on all three of its routes. TabiAi now serves
`claude-opus-5` correctly and GoRouter serves it intermittently, so a fallback path
exists for the first time. It stays off because both are unpriced, and the standing
decision is to price a route before sending flagship traffic to it. AgentRouter's
depth went from 1 usable credential to 19, which removed the single point of failure
that made this urgent.

---

## Open

| Item | Why it matters |
|---|---|
| Seven AgentRouter credentials still need a human | Two are out of quota and will clear. One needs this host's egress IP on the token's allow list. One needs model entitlement. Each carries a `note` saying which. |
| GoRouter is intermittently unreachable | Connection resets interleave with successful requests from the same host and credential. It works often enough to serve as a fallback, but not reliably enough to be a primary. |
| Credential recovery is unproven end to end | The mechanism is proven at engine level and by tests, but no trial has been observed completing on live traffic, because the healthy credential keeps succeeding. |
| The pricing model has no per-request fee shape | Three of four providers bill that way, so this is a gap in the model rather than an edge case. See "Blocked". |
