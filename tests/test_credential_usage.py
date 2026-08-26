"""The usage poller is the only thing that can refresh a quota observation.

It shipped disabled by default with no test at all, and the consequence was
visible in production: twenty-five credentials carried a spend figure observed
six days earlier, rendered as though it were current, and nothing in the system
could refresh it. Turning a worker on that writes to every credential row
without proving what it does on failure would be reckless, so the cases that
matter are pinned here:

* a good answer updates the right credential, with provenance and a timestamp
* every kind of bad answer leaves the previous observation untouched
* a stale observation becomes fresh again after one successful poll
* the loop polls when it starts, not one interval later
* the poll identifies itself the way provider traffic does, because two relays
  sit behind an edge that refuses generic library user-agents
* the poller never writes a balance, because no provider exposes one it could
  trust and inventing one would be worse than admitting ignorance
"""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from gateway.health.usage import (
    USAGE_PATH,
    poll_credential_usage,
    usage_poll_loop,
)
from gateway.providers import DEFAULT_USER_AGENT
from gateway.security.credentials import CredentialCipher

ENCRYPTION_KEY = base64.b64encode(b"u" * 32).decode()


def encrypted(credential_id: str, secret: str) -> dict[str, Any]:
    envelope = CredentialCipher.from_base64(ENCRYPTION_KEY).encrypt(
        secret, context=f"provider-credential:{credential_id}"
    )
    return {
        "secret_version": envelope.version,
        "secret_nonce": envelope.nonce,
        "secret_ciphertext": envelope.ciphertext,
    }


def credential(
    credential_id: str,
    *,
    secret: str = "sk-secret",
    base_url: str = "https://relay.example",
    provider_name: str = "Relay",
) -> dict[str, Any]:
    return {
        "id": credential_id,
        "provider_id": f"provider-of-{credential_id}",
        "base_url": base_url,
        "provider_name": provider_name,
        **encrypted(credential_id, secret),
    }


class RecordingPool:
    """Serves the candidate query and records every write the poller performs."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.writes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "from public.provider_credentials" in query
        return self.rows

    async def execute(self, query: str, *args: Any) -> None:
        self.writes.append((query, args))

    def updated_ids(self) -> list[Any]:
        return [args[0] for _, args in self.writes]


def responder(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def usage_response(total: float) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "total_usage": total})


@pytest.mark.asyncio
async def test_successful_poll_records_spend_with_provenance_and_time() -> None:
    pool = RecordingPool([credential("cred-1")])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return usage_response(1234.5)

    async with responder(handler) as client:
        observations = await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert [(item.credential_id, item.total_usage) for item in observations] == [
        ("cred-1", 1234.5)
    ]
    # The credential's own secret is what identifies the account upstream, so the
    # figure returned belongs to that credential and no other.
    assert seen[0].url.path == USAGE_PATH
    assert seen[0].headers["authorization"] == "Bearer sk-secret"

    assert len(pool.writes) == 1
    query, args = pool.writes[0]
    assert args[0] == "cred-1"
    assert args[1] == 1234.5
    # Provenance and observation time are written in the same statement as the
    # figure. A number without either is indistinguishable from a guess.
    assert "quota_source = 'upstream_usage'" in query
    assert "quota_observed_at = now()" in query


@pytest.mark.asyncio
async def test_each_credential_is_updated_from_its_own_answer() -> None:
    pool = RecordingPool(
        [
            credential("cred-1", secret="sk-one"),
            credential("cred-2", secret="sk-two"),
            credential("cred-3", secret="sk-three"),
        ]
    )
    by_secret = {"sk-one": 10.0, "sk-two": 20.0, "sk-three": 30.0}

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        return usage_response(by_secret[token])

    async with responder(handler) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert [(args[0], args[1]) for _, args in pool.writes] == [
        ("cred-1", 10.0),
        ("cred-2", 20.0),
        ("cred-3", 30.0),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "response"),
    [
        ("server error", httpx.Response(500, json={"error": "boom"})),
        ("unauthorized", httpx.Response(401, json={"error": "bad key"})),
        ("not implemented", httpx.Response(404, text="Not Found")),
        ("rate limited", httpx.Response(429, json={"error": "slow down"})),
        # A parking page or an edge challenge answering 200 with HTML is the case
        # that would silently overwrite a real figure with nonsense.
        ("html parking page", httpx.Response(200, text="<html>hello</html>")),
        ("json but not an object", httpx.Response(200, json=[1, 2, 3])),
        ("object without total_usage", httpx.Response(200, json={"object": "list"})),
        ("total_usage not a number", httpx.Response(200, json={"total_usage": "lots"})),
        ("total_usage null", httpx.Response(200, json={"total_usage": None})),
    ],
)
async def test_a_bad_answer_never_overwrites_a_previous_observation(
    name: str, response: httpx.Response
) -> None:
    pool = RecordingPool([credential("cred-1")])

    async with responder(lambda request: response) as client:
        observations = await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert observations == [], name
    # No statement at all is the only safe outcome. Writing a null, a zero, or a
    # fresh timestamp with an unchanged figure would each be a different lie.
    assert pool.writes == [], name


@pytest.mark.asyncio
async def test_a_transport_failure_never_overwrites_a_previous_observation() -> None:
    pool = RecordingPool([credential("cred-1")])

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream unreachable", request=request)

    async with responder(handler) as client:
        observations = await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert observations == []
    assert pool.writes == []


@pytest.mark.asyncio
async def test_one_failing_credential_does_not_stop_the_others() -> None:
    pool = RecordingPool(
        [
            credential("cred-good-1", secret="sk-one"),
            credential("cred-bad", secret="sk-bad"),
            credential("cred-good-2", secret="sk-two"),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer sk-bad":
            return httpx.Response(500, json={"error": "boom"})
        return usage_response(42.0)

    async with responder(handler) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert pool.updated_ids() == ["cred-good-1", "cred-good-2"]


@pytest.mark.asyncio
async def test_an_undecryptable_secret_is_skipped_without_a_write() -> None:
    row = credential("cred-1")
    # Encrypted under a different credential id, so the context does not match.
    row.update(encrypted("a-different-credential", "sk-secret"))
    pool = RecordingPool([row])
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return usage_response(1.0)

    async with responder(handler) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert pool.writes == []
    # A secret that cannot be decrypted must never be sent anywhere.
    assert called is False


@pytest.mark.asyncio
async def test_the_poller_never_writes_a_balance() -> None:
    """No provider exposes a balance this worker could trust.

    The relays answer their subscription endpoint with an identical placeholder
    ceiling for every credential, so a balance is an operator observation. The
    poller must not touch the column, or a hand-recorded figure would be
    silently replaced by a derived one.
    """
    pool = RecordingPool([credential("cred-1")])

    async with responder(lambda request: usage_response(7.0)) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    written = " ".join(query for query, _ in pool.writes)
    assert "balance_amount" not in written
    assert "balance_observed_at" not in written
    assert "balance_source" not in written


@pytest.mark.asyncio
async def test_the_poll_identifies_itself_the_same_way_traffic_does() -> None:
    """The poll must send the user-agent the request path already sends.

    Two of the configured relays sit behind Cloudflare, which refuses generic
    library user-agents. The adapters set one for exactly that reason; this
    request did not, so httpx supplied "python-httpx/x.y" and the edge answered a
    challenge page. Asserting against the shared constant rather than a literal
    is deliberate: if the value the traffic path uses ever changes, the poll has
    to change with it, and the two drifting apart is the whole defect.
    """
    pool = RecordingPool([credential("cred-1")])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return usage_response(1.0)

    async with responder(handler) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert seen[0].headers["user-agent"] == DEFAULT_USER_AGENT


@pytest.mark.asyncio
async def test_an_edge_that_refuses_library_user_agents_is_still_polled() -> None:
    """Reproduces the observed production failure end to end.

    gorouter.app and tabitoken.com answer 403 with an HTML challenge to the httpx
    default and to curl, and 200 with JSON to this gateway's user-agent. Fifteen
    credentials therefore read "never observed" while serving live traffic
    normally, because the request path sent the header and this worker did not.
    The poller's own guards behaved correctly throughout - HTML is refused rather
    than stored - so nothing looked broken except the absence of any figure.
    """
    pool = RecordingPool([credential("cred-1")])

    def cloudflare(request: httpx.Request) -> httpx.Response:
        agent = request.headers.get("user-agent", "")
        if agent != DEFAULT_USER_AGENT:
            return httpx.Response(
                403,
                text="<html><title>Attention Required! | Cloudflare</title></html>",
                headers={"content-type": "text/html; charset=UTF-8"},
            )
        return usage_response(2720.0)

    async with responder(cloudflare) as client:
        observations = await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert [(item.credential_id, item.total_usage) for item in observations] == [
        ("cred-1", 2720.0)
    ]
    assert pool.updated_ids() == ["cred-1"]


@pytest.mark.asyncio
async def test_a_stale_observation_becomes_fresh_after_a_successful_poll() -> None:
    """The production symptom, reproduced and then cleared.

    A credential whose figure was observed six days ago is indistinguishable
    from a current one until something re-observes it. This asserts the poller
    is that something.
    """
    six_days_ago = datetime.now(UTC) - timedelta(days=6)
    stored = {
        "quota_used": 12373.4414,
        "quota_source": "upstream_usage",
        "quota_observed_at": six_days_ago,
    }

    class StatefulPool(RecordingPool):
        async def execute(self, query: str, *args: Any) -> None:
            await super().execute(query, *args)
            stored["quota_used"] = args[1]
            stored["quota_source"] = "upstream_usage"
            stored["quota_observed_at"] = datetime.now(UTC)

    pool = StatefulPool([credential("cred-1")])
    assert datetime.now(UTC) - stored["quota_observed_at"] > timedelta(hours=24)

    async with responder(lambda request: usage_response(18697.9984)) as client:
        await poll_credential_usage(pool, ENCRYPTION_KEY, client=client)

    assert stored["quota_used"] == 18697.9984
    assert datetime.now(UTC) - stored["quota_observed_at"] < timedelta(minutes=1)


@pytest.mark.asyncio
async def test_no_candidates_makes_no_requests() -> None:
    pool = RecordingPool([])
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return usage_response(1.0)

    async with responder(handler) as client:
        assert await poll_credential_usage(pool, ENCRYPTION_KEY, client=client) == []

    assert called is False
    assert pool.writes == []


@pytest.mark.asyncio
async def test_the_loop_polls_when_it_starts_rather_than_one_interval_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that sleeps first leaves every restart stale for a full interval.

    At the shipped interval of fifteen minutes that means a gateway restart
    guarantees quarter-hour-old figures with no way to ask for better, which is
    the same complaint the poller exists to answer.
    """
    polls: list[float] = []
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def fake_poll(pool: Any, key: str) -> list[Any]:
        polls.append(1.0)
        return []

    monkeypatch.setattr("gateway.health.usage.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("gateway.health.usage.poll_credential_usage", fake_poll)

    with pytest.raises(asyncio.CancelledError):
        await usage_poll_loop(900, lambda: RecordingPool([]), ENCRYPTION_KEY)

    # One poll before the first sleep, and one per interval after it.
    assert len(polls) == 2
    assert sleeps == [900, 900]


@pytest.mark.asyncio
async def test_the_loop_survives_a_failing_poll_and_keeps_its_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    attempts: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    async def failing_poll(pool: Any, key: str) -> list[Any]:
        attempts.append(1)
        raise RuntimeError("database gone")

    monkeypatch.setattr("gateway.health.usage.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("gateway.health.usage.poll_credential_usage", failing_poll)

    with pytest.raises(asyncio.CancelledError):
        await usage_poll_loop(60, lambda: RecordingPool([]), ENCRYPTION_KEY)

    # A raising poll must not kill the worker, or one bad cycle disables
    # refreshing until the next deploy.
    assert len(attempts) == 3
    assert sleeps == [60, 60, 60]


@pytest.mark.asyncio
async def test_the_loop_does_nothing_without_a_pool_or_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    polls: list[int] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    async def fake_poll(pool: Any, key: str) -> list[Any]:
        polls.append(1)
        return []

    monkeypatch.setattr("gateway.health.usage.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("gateway.health.usage.poll_credential_usage", fake_poll)

    with pytest.raises(asyncio.CancelledError):
        await usage_poll_loop(30, lambda: None, ENCRYPTION_KEY)
    assert polls == []

    sleeps.clear()
    with pytest.raises(asyncio.CancelledError):
        await usage_poll_loop(30, lambda: RecordingPool([]), None)
    assert polls == []
