# Launch acceptance checklist

Run this yourself, in order, before launch. No database access and no knowledge of
the implementation is required. Everything is done through the Dashboard at
**https://www.duedirect.link** and through OpenCode and Claude Code as you normally
use them.

Each step gives you an **action**, the **expected result**, and a place to record
pass or fail. Where a step tells you to break something on purpose, it also tells
you how to put it back.

Two things to know before you start:

- Configuration changes are saved to a *working* copy. Nothing takes effect for
  traffic until you **publish**. Sections A to E are read-only, so you can go
  through them without changing anything.
- Section J deliberately causes failures. Do it when you can tolerate a few
  requests failing, and follow the restore step at the end of each one.

---

## A. Sign in and first impressions

| # | Action | Expected result | Pass |
|---|---|---|---|
| A1 | Open the Dashboard URL while signed out | You are sent to a login page, not an error | ☐ |
| A2 | Sign in with your email and password | The Overview loads | ☐ |
| A3 | Enter a wrong password on purpose | A clear "invalid credentials" style message, no blank page or stack trace | ☐ |
| A4 | Sign in again, then reload the page | You stay signed in | ☐ |
| A5 | Sign out | You are returned to the login page and cannot reach the Dashboard by pressing Back | ☐ |

---

## B. Overview: does it tell the truth?

| # | Action | Expected result | Pass |
|---|---|---|---|
| B1 | Read **Requests today** and **Successful** / **Failed** | The numbers are plausible for your usage. Failed is not blank | ☐ |
| B2 | Read **Estimated month cost** | A dollar amount with a currency, **not** `0.0000` and **not** blank | ☐ |
| B3 | Read **Active snapshot** | A version number. Note it down; you will use it in section H | ☐ |
| B4 | Read **Runtime ready** | Says ready | ☐ |
| B5 | Look for a pricing coverage figure in **Analytics** | A percentage. If it is below 100%, some traffic is unpriced — section G covers why that matters | ☐ |

**What good looks like:** every number is either a real value or an explicit
statement that it is unavailable. If you see a suspiciously round `0` anywhere,
treat it as a failure and tell me which panel.

---

## C. Providers, credentials and models

| # | Action | Expected result | Pass |
|---|---|---|---|
| C1 | Open **Providers** | Your providers are listed with health and how many credentials each has | ☐ |
| C2 | Confirm the list matches what you expect | AgentRouter, GoRouter and hcnsec enabled; TabiAi and the archived AgentRouter OpenAI disabled | ☐ |
| C3 | Open **Credentials (Advanced)** | Every credential shows only a masked hint, never a full key | ☐ |
| C4 | Find the **Quota** columns | Each credential says whether its quota is *measured*, *trend only*, or *no signal* — not just a bare number | ☐ |
| C5 | Find the **Balance** columns | A balance with the time it was observed, or "Not observed". Never an implied zero | ☐ |
| C6 | Open **Models** | Your five models are listed | ☐ |
| C7 | Pick `claude-opus-5` | You can see which providers can serve it | ☐ |
| C8 | Open **Clients** | Three enabled clients with keys, three marked "unused duplicate" and disabled | ☐ |

**Note on C4/C5:** a quota number with no limit configured has no denominator, so
it cannot tell you remaining capacity. The Dashboard now says so rather than
implying otherwise. That is the correct behaviour, not a gap.

---

## D. Routing: who serves what, and why

| # | Action | Expected result | Pass |
|---|---|---|---|
| D1 | Open **Models and routing**, select `claude-opus-5` | AgentRouter is shown as primary, GoRouter as fallback | ☐ |
| D2 | Select `claude-opus-5-thinking` | GoRouter is primary, TabiAi is the fallback | ☐ |
| D3 | Read the "What will happen" sentence | It names the primary and the fallbacks in plain language | ☐ |
| D4 | **Do not change anything.** Press **Save working changes** | It saves without error | ☐ |
| D5 | Open **Configuration** | Either no pending changes, or only changes you recognise. It must **not** report that routes were removed or disabled | ☐ |

| D6 | Run `env -u GATEWAY_DATABASE_URL .venv/bin/python deploy/verify_provider_failover.py` | For `claude-opus-5-thinking` the attempted providers are `GoRouter, TabiAi`. No model reports STILL BROKEN | ☐ |
| D7 | In the same output, read the routable/total column | If any provider shows 1 routable, its spare credentials need attention before launch. See Known conditions | ☐ |

**D4 and D5 together are the single most important safety check in this document.**
Loading routing and saving it back without touching anything must not alter your
configuration. If Configuration shows routes being removed after D4, stop and tell
me.

---

## E. Requests, health, usage and cost

| # | Action | Expected result | Pass |
|---|---|---|---|
| E1 | Open **Requests** | Recent requests with model, status and latency | ☐ |
| E2 | Find a failed request and open **Trace request** | It shows each attempt, which provider and credential, and a plain-language reason | ☐ |
| E3 | Read the reason text | A sentence explaining what happened and what the Gateway did. Not a raw string like `provider_unavailable` | ☐ |
| E4 | Look at the trace's **Cost** | A dollar amount, or "Pricing unavailable". If some attempts were unpriced it says how many. Never `0.0000 null` | ☐ |
| E5 | Open **Health** | Recent health events per provider and credential, each with a readable reason | ☐ |
| E6 | Open **Usage** | Token counts and per-record cost | ☐ |
| E7 | Open **Analytics** | Totals by model and provider, and a **failed** breakdown separate from the totals | ☐ |
| E8 | Check the failed breakdown | Retries and failed attempts are shown separately, not silently folded into your totals | ☐ |

---

## F. Budgets

Your global cap is **$4,000 per month, blocking**.

| # | Action | Expected result | Pass |
|---|---|---|---|
| F1 | Open **Budgets** | "Global monthly spend cap", USD 4000.00, monthly, enforcement `block` | ☐ |
| F2 | Read the **used** figure | Spend for the *current month only*, not since the beginning of time | ☐ |
| F3 | Read **requests this period** | A request count for the same period | ☐ |

**What this cap means now:** reserved spend is corrected to what requests actually
cost, so the cap trips at $4,000 of real spend. Before this was fixed it would have
tripped near $2,124. You will be warned at 75% and again at 90%.

---

## G. Alerts

| # | Action | Expected result | Pass |
|---|---|---|---|
| G1 | Open **Alerts** | Any current alerts, most severe first | ☐ |
| G2 | For each alert, read the four fields | It tells you **what happened**, **why it matters**, **what to do**, and the **measured values** | ☐ |
| G3 | Confirm you can act on it without asking anyone | If an alert leaves you unsure what to do, that is a failure — tell me which one | ☐ |
| G4 | Open **Alert rules** | Thirteen rules, each with a readable name | ☐ |
| G5 | Read one rule's condition | A sentence such as "at or above 0.5, measured over 15 minutes". Not raw JSON | ☐ |
| G6 | Check the currently open alerts | You should see the two GoRouter credentials flagged for low balance, and a spend alert | ☐ |
| G7 | Find **Provider is down to its last credential** in the rules | Present and enabled. It warns while one credential is left, where **Provider has no usable credentials left** only fires once nothing is usable | ☐ |

---

## H. Configuration change, publish and rollback

This section changes configuration and puts it back.

| # | Action | Expected result | Pass |
|---|---|---|---|
| H1 | Note the **Active snapshot** version from B3 | — | ☐ |
| H2 | In **Models and routing**, move a fallback provider's order, then **Save working changes** | Saves, and says changes are unpublished | ☐ |
| H3 | Open **Configuration** | It describes your change in plain language, and only your change | ☐ |
| H4 | Press **Publish** | A new version number, higher than H1 | ☐ |
| H5 | Send one request from OpenCode | It still works | ☐ |
| H6 | **Roll back** to the version from H1 | A new version is published whose content matches the old one | ☐ |
| H7 | Send one request again | It still works | ☐ |
| H8 | Open **Configuration** | No pending changes | ☐ |

---

## I. OpenCode and Claude Code

Use both clients as you normally would. The point is that nothing internal should
be visible to you.

| # | Action | Expected result | Pass |
|---|---|---|---|
| I1 | In OpenCode, ask a question using `claude-opus-5` | A normal answer | ☐ |
| I2 | In OpenCode, ask something that produces a long streamed answer | Text streams smoothly, no stall or truncation | ☐ |
| I3 | In OpenCode, switch to `gpt-5.6-sol` and ask again | A normal answer | ☐ |
| I4 | In OpenCode, try `nemotron-3-ultra` | A normal answer, but be patient: measured latency ranges from 6s to over 300s and can hit the 600s provider timeout | ☐ |
| I5 | In Claude Code, run a multi-step task that reads files and makes edits | Completes normally | ☐ |
| I6 | In Claude Code, interrupt a long response part-way | The client recovers; the next request works | ☐ |
| I7 | Run ten requests in a row in either client | All succeed. No manual intervention needed | ☐ |
| I8 | Open **Requests** in the Dashboard | Your client requests appear, attributed to the right client | ☐ |
| I9 | Ask for a model that does not exist, e.g. `claude-sonnet-5` | A clear message that no provider is configured for it — not a hang and not a generic error | ☐ |

---

## J. Controlled failure drills

These deliberately break things. Each has a restore step. Do them one at a time.

### J1 — A credential fails

| Step | Action | Expected result | Pass |
|---|---|---|---|
| a | **Credentials (Advanced)**: disable one AgentRouter credential. Publish | Saves and publishes | ☐ |
| b | Send five requests from OpenCode using `claude-opus-5` | All five succeed. You should notice nothing | ☐ |
| c | **Health** | Shows the credential is no longer in use | ☐ |
| d | **Restore:** re-enable the credential. Publish | — | ☐ |
| e | Send five more requests | All succeed | ☐ |

**Pass condition:** you could not tell from the client that anything happened.

### J2 — A provider goes away

| Step | Action | Expected result | Pass |
|---|---|---|---|
| a | **Providers**: disable GoRouter. Publish | Saves and publishes | ☐ |
| b | Send five `claude-opus-5` requests | All succeed, served by AgentRouter | ☐ |
| c | Send one `claude-opus-5-thinking` request | This one **fails**, because GoRouter is its only provider. The message should say no route is currently eligible | ☐ |
| d | **Requests** → trace the failure | The trace shows which candidates were excluded and why | ☐ |
| e | **Restore:** re-enable GoRouter. Publish | — | ☐ |
| f | Send `claude-opus-5-thinking` again | It is attempted again (it may still fail if GoRouter's own edge is blocking — see the note below) | ☐ |

**Note:** GoRouter's edge is currently rejecting this server's requests
intermittently, independent of your keys or balance. A failure at step f is a
GoRouter-side problem, and the Dashboard should describe it as an edge or
provider problem, **not** as a bad credential.

### J3 — Every credential on a provider is unusable

| Step | Action | Expected result | Pass |
|---|---|---|---|
| a | Disable **all** hcnsec credentials. Publish | — | ☐ |
| b | Send a `nemotron-3-ultra` request | Fails with "no route currently eligible" | ☐ |
| c | Wait about a minute, open **Alerts** | An alert appears saying the provider has no usable credentials, with what to do | ☐ |
| d | **Restore:** re-enable them. Publish | — | ☐ |
| e | Wait about a minute, open **Alerts** | The alert has moved to **resolved**, reason *recovered* | ☐ |
| f | Send a `nemotron-3-ultra` request | Succeeds | ☐ |

**Step e is the important one.** An alerting system that never closes an alert
cannot tell you whether a problem is live.

### J4 — Deletion safety

| Step | Action | Expected result | Pass |
|---|---|---|---|
| a | Try to delete a provider that is serving a model | **Refused**, with an explanation of what depends on it | ☐ |
| b | Try to delete a credential that is the only one for a provider | Refused or clearly warned | ☐ |
| c | Confirm nothing was deleted | Providers and credentials unchanged | ☐ |

### J5 — Unsafe configuration is rejected

| Step | Action | Expected result | Pass |
|---|---|---|---|
| a | Try to leave a model with no enabled provider, then publish | Publish is **refused**, naming the model that would be stranded | ☐ |
| b | Put it back | Publish succeeds | ☐ |

---

## K. Empty and error states

| # | Action | Expected result | Pass |
|---|---|---|---|
| K1 | Open a section with nothing in it, e.g. **Activity** on a quiet day | A sentence explaining it is empty, not a blank panel or a spinner | ☐ |
| K2 | Filter **Requests** to something that matches nothing | An explicit "nothing found" state | ☐ |
| K3 | Open **Provider pools (Advanced)** | Explains there are none configured. Empty is correct here | ☐ |
| K4 | Stop the Gateway, then reload the Dashboard | A clear message that the Gateway is unreachable, not a stack trace | ☐ |
| K5 | Start the Gateway again, reload | Everything returns to normal | ☐ |

---

## L. Advanced sections

You should not need these for normal operation, but nothing is hidden.

| # | Action | Expected result | Pass |
|---|---|---|---|
| L1 | Open **Mappings (Advanced)** | Which provider serves which model, over which protocol | ☐ |
| L2 | Open **Routes (Advanced)** | Priorities and fallback settings | ☐ |
| L3 | Open **Policies (Advanced)** | One routing policy per model | ☐ |
| L4 | Open **Provider pools (Advanced)** | Empty, as above | ☐ |
| L5 | Confirm you did **not** need any of these to complete sections A to J | — | ☐ |

---

## Acceptance criteria

Launch when all of these are true:

1. **No client-visible failure** during J1 and J2 step b. A credential or provider
   going away must be invisible to OpenCode and Claude Code.
2. **Every failure reads as a sentence** that tells you what happened and what the
   Gateway did. No raw strings.
3. **Every number is honest.** No `$0.00` standing in for a missing measurement, no
   `0.0000 null`, no quota figure implying capacity it cannot know.
4. **Alerts open and close.** J3 step e shows an alert resolving itself.
5. **Save is lossless.** D4 and D5 show that saving unchanged routing changes
   nothing.
6. **Publish and rollback both work** and traffic keeps flowing across both.
7. **Deletion and unsafe configuration are refused** with an explanation.
8. **You can answer these from the Dashboard alone:** which providers exist, which
   credentials belong to each, which models exist, who can serve each model, which
   is primary, current health, current spend, quota state, budgets, open alerts,
   recent failures, and why a specific request failed.

---

## Known conditions at time of writing

These are already understood. Seeing them is not a failure.

- **GoRouter and TabiAi were never IP-blocked.** That was a misdiagnosis, twice. The
  gateway's OpenAI adapter sent no user-agent, so httpx supplied its own, and
  Cloudflare refuses generic library user-agents; probes with curl and urllib were
  refused for the same reason, which made the wrong answer look consistent. With a
  user-agent set, both providers answer. Ablation from this host: the httpx default,
  python-requests and curl are all refused, `ai-gateway/0.1` is accepted.
- **GoRouter is intermittently unreachable, and that is now handled.** Connection
  resets interleave with successful requests from the same host and credential, so it
  works often enough to be a fallback but not to be a primary.
  `claude-opus-5-thinking` is configured GoRouter first, TabiAi second, and in
  production now records exactly that: GoRouter fails with provider_unavailable and
  TabiAi serves the request. Seeing one failed attempt followed by a success is the
  system working, not a fault.
- **`gpt-5.6-sol` occasionally fails with "no route currently eligible".** It has one
  provider, AgentRouter, and `allow_model_fallback` is false, so when every AgentRouter
  credential is momentarily out of quota or rate limited there is nowhere to go. Seen
  once during acceptance, failing in 244ms rather than hanging.
- **Two GoRouter credentials show a low balance alert** from an observation of
  $0.061184. Balance is only as fresh as its last observation; the alert states
  when it was taken.
- **Spend is high.** Measured at roughly $29–30 per hour during continuous agent
  work, about $0.36 per request. A spend alert firing is the system working.
- **`claude-sonnet-5` and `kimi-k3` do not resolve.** They are different models
  from anything configured. They fail with a clear 404 on purpose; aliasing them to
  an Opus mapping would answer with a model you did not ask for.
- **`glm-5.2` has been retired.** api.hcnsec.cn no longer serves it and returns 503
  model_not_found. Both the mapping and the canonical model are disabled rather than
  deleted, so the 72 historical requests and 32 attempts naming it keep their
  meaning, and it can be re-enabled untouched if the channel returns. Requesting it
  now fails with a clean 404, "No provider is configured to serve this model as
  requested."
- **`nemotron-3-ultra` is what hcnsec actually serves, and it is slow.** The mapping
  asks hcnsec for `DeepSeek-V4-Pro`, because that is the only string it routes, and
  every response identifies itself as `nvidia/nemotron-3-ultra-550b-a55b`. The
  canonical model is named for what answers rather than for what the provider
  accepts. hcnsec substitutes across its whole catalogue this way: Kimi-K2.6 returns
  thinkingmachines/inkling, Qwen3.8-27B returns meta/muse-glimmer-30b. About one
  streaming request in ten instead returns Anthropic protocol events claiming to be
  claude-opus-5, so the backend is not stable even for one name. Measured latency
  was 6s, 116s, 303s, 303s, 531s and one 600s timeout, so treat it as a batch route,
  not an interactive one. It also reports about 1049 tokens of hidden prompt on
  every request.
- **Three of four providers bill a flat fee per request, and are unpriced because of
  it.** Measured with a control read confirming the counter is otherwise still: hcnsec
  moves its counter 640.94 per request, TabiAi exactly 80, GoRouter exactly 30, in each
  case regardless of token count. All three also report a per-request cost that does
  vary with tokens, contradicting their own counter by between 412x and 931x. The unit
  cannot be resolved from outside, and the pricing model has no shape for a
  per-request fee, only per-million-token rates. So the readings are recorded and the
  routes stay unpriced. Cost views under-report those routes rather than claiming they
  are free. See PUNCH_LIST.md for what would unblock it.
- **OpenCode's `gateway-openai` models need a key that permits the OpenAI protocol.**
  The `Claude Code Cli` client only permits `anthropic_messages`, so a key from it
  fails every `gateway-openai/*` request with "Invalid gateway key", which reads as a
  bad key rather than a denied protocol. Use a key from the `Opencode` client, which
  permits both protocols and therefore serves both providers in the OpenCode config.
- **Some older usage records show zero tokens.** Those predate the streaming fix
  and cannot be re-costed. Records from 2026-08-19 22:20 onwards are correct.

---

## If a step fails

Note the step number, what you saw, and the request id if one was shown. The
request id appears in the trace view and in error responses. That is enough to
find the cause without database access.
