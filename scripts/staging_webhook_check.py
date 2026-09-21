"""Finite real-DB/TLS webhook drill with a controlled loopback receiver on AWS staging."""

import argparse
import asyncio
import json
import os
import secrets
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.staging_boundary import (  # noqa: E402
    clean_environment,
    connect,
    disable_dotenv,
    require_staging,
)
from scripts.webhook_receiver import URL, LoopbackTLS, Receiver, certificates  # noqa: E402


def settings(config):
    from gateway.config import Settings

    return Settings(_env_file=None, database_url=None, environment="test",
        alert_delivery_enabled=True, alert_delivery_url=URL,
        alert_delivery_secret=config["secret"], alert_delivery_destination=config["destination"],
        alert_delivery_max_attempts=2, alert_delivery_timeout_seconds=2,
        health_probe_enabled=False, alert_monitor_enabled=False)


async def send_once(config, *, crash=False):
    import httpx

    from gateway.alert_delivery import deliver_one
    from gateway.alert_delivery_store import AlertDeliveryStore

    db = await connect()
    try:
        store = AlertDeliveryStore(db)
        if crash:
            async def lost_ack(*args, **kwargs):
                if kwargs.get("outcome") != "delivered":
                    raise AssertionError("Crash probe did not receive a successful HTTP response")
                # Process loss after receiver acknowledgment, before DB finish.
                os._exit(73)
            store.finish = lost_ack
        async with httpx.AsyncClient(transport=LoopbackTLS(config["port"], config["cert"]),
                                     trust_env=False, follow_redirects=False, timeout=2) as client:
            return await deliver_one(store, client, settings(config))
    finally:
        await db.close()


async def child(config_path, crash=False):
    args = [sys.executable, "-I", str(Path(__file__).resolve()), "--send-once",
            "--config", str(config_path)]
    if crash:
        args.append("--crash-after-ack")
    process = await asyncio.create_subprocess_exec(*args, cwd=config_path.parent,
        env=clean_environment(config_path.parent), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL)
    try:
        await asyncio.wait_for(process.communicate(), 15)
        if process.returncode != (73 if crash else 0):
            raise RuntimeError("Webhook subprocess failed")
        return process.pid
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def wait_until_due(db, event_id, *, lease=False):
    column = "lease_until" if lease else "next_attempt_at"
    began = monotonic()
    async with asyncio.timeout(40):
        while not await db.fetchval(
            f"select {column}<=clock_timestamp() from public.alert_delivery_outbox where id=$1",
            event_id,
        ):
            await asyncio.sleep(.1)
    return monotonic() - began


async def drill(output):
    import httpx
    import uvicorn

    from gateway.alert_delivery_store import AlertDeliveryStore
    from gateway.alerts import raise_alert, resolve_alerts

    root = require_staging()
    report = {"qualified": False, "scope": "controlled staging receiver only",
              "external_delivery": False, "tls_verification": True, "phases": {}}
    db = await connect()
    rule_id, server, server_task, listener = None, None, None, None
    try:
        await AlertDeliveryStore(db).require_schema()
        with TemporaryDirectory(prefix="webhook-", dir=root / "evidence") as directory:
            directory = Path(directory)
            cert, key = certificates(directory)
            config = {"secret": secrets.token_urlsafe(32), "destination": "drill_" + uuid4().hex,
                      "cert": str(cert)}
            receiver = Receiver(config["secret"])
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            listener.listen(16)
            config["port"] = listener.getsockname()[1]
            config_path = directory / "sender.json"
            with config_path.open("x") as stream:
                config_path.chmod(0o600)
                json.dump(config, stream)
            server = uvicorn.Server(uvicorn.Config(receiver.app(), host="127.0.0.1",
                access_log=False, log_level="error", ssl_certfile=str(cert), ssl_keyfile=str(key),
                timeout_graceful_shutdown=3))
            server_task = asyncio.create_task(server.serve(sockets=[listener]))
            async with asyncio.timeout(5):
                while not server.started:
                    if server_task.done():
                        await server_task
                    await asyncio.sleep(.02)
            rule_id = await db.fetchval(
                "insert into public.alert_rules(name,event_type,severity,enabled,"
                "delivery_destination) values($1,'synthetic_webhook','warning',true,$2) "
                "returning id",
                config["destination"], config["destination"],
            )

            async def opened(label):
                dedup = f"{config['destination']}:{label}"
                await raise_alert(db, rule_id=rule_id, dedup_key=dedup, severity="warning",
                    event_type="synthetic_webhook", title="Synthetic staging alert",
                    scope_type=None, scope_id=None, metadata={"private_fixture": "not delivered"},
                    cooldown_seconds=0)
                row = await db.fetchrow(
                    "select o.* from public.alert_delivery_outbox o join public.alerts a "
                    "on a.id=o.alert_id where a.rule_id=$1 and a.dedup_key=$2 order by sequence",
                    rule_id, dedup,
                )
                return dedup, dict(row)

            async def state(event_id):
                return dict(await db.fetchrow(
                    "select delivery_status,attempts,last_status,last_error,"
                    "lease_token,lease_until "
                    "from public.alert_delivery_outbox where id=$1", event_id,
                ))

            dedup, event = await opened("lifecycle")
            await opened("lifecycle")  # Real occurrence upsert must not create another event.
            assert await db.fetchval("select count(*) from public.alert_delivery_outbox "
                "where alert_id=$1", event["alert_id"]) == 1
            await child(config_path)
            first = await state(event["id"])
            assert (first["delivery_status"], first["attempts"], first["last_status"]) == (
                "pending", 1, 503)
            assert not await send_once(config), "Backoff must prevent immediate retry"
            waited = await wait_until_due(db, event["id"])
            await child(config_path)
            assert (await state(event["id"]))["delivery_status"] == "delivered"
            report["phases"]["transient_retry"] = {"attempts": 2, "backoff_wait_seconds": waited}
            await db.execute("update public.alerts set status='acknowledged' where id=$1",
                             event["alert_id"])
            assert await db.fetchval("select count(*) from public.alert_delivery_outbox "
                "where alert_id=$1", event["alert_id"]) == 1
            assert await resolve_alerts(db, dedup_keys=[dedup]) == 1
            resolved = dict(await db.fetchrow("select * from public.alert_delivery_outbox "
                "where alert_id=$1 and sequence=2", event["alert_id"]))
            assert resolved["status"] == "resolved"
            receiver.mode = "success"
            crashed_pid = await child(config_path, crash=True)
            claimed = await state(resolved["id"])
            assert claimed["delivery_status"] == "pending" and claimed["attempts"] == 1
            assert str(resolved["id"]) in receiver.accepted
            assert not await send_once(config), "Live lease must prevent duplicate delivery"
            lease_wait = await wait_until_due(db, resolved["id"], lease=True)
            restarted_pid = await child(config_path)
            final = await state(resolved["id"])
            assert restarted_pid != crashed_pid and final["delivery_status"] == "delivered"
            assert final["attempts"] == 2 and receiver.duplicates == 1
            assert not await AlertDeliveryStore(db).finish(
                resolved["id"], claimed["lease_token"], outcome="dead")
            report["phases"]["restart_after_ack"] = {
                "different_process": True, "real_lease_wait_seconds": lease_wait,
                "duplicates_deduplicated": receiver.duplicates, "stale_token_rejected": True}
            report["phases"]["transitions"] = {"open": 1, "resolved": 2,
                                                "ack_and_upsert_suppressed": True}
            for mode, expected_attempts in (("permanent", 1), ("exhaust", 2)):
                receiver.mode = mode
                _, row = await opened(mode)
                await child(config_path)
                if expected_attempts == 2:
                    await wait_until_due(db, row["id"])
                    await child(config_path)
                final = await state(row["id"])
                assert final["delivery_status"] == "dead" and final["attempts"] == expected_attempts
                report["phases"][mode] = {k: final[k] for k in (
                    "delivery_status", "attempts", "last_status", "last_error")}
            async with httpx.AsyncClient(transport=LoopbackTLS(config["port"], cert),
                                         trust_env=False) as client:
                tampered = await client.post(URL, content=b"{}", headers={
                    "X-Gateway-Timestamp": "0", "X-Gateway-Signature": "sha256=invalid",
                    "X-Gateway-Event-ID": str(uuid4())})
                assert tampered.status_code == 401
            assert len(receiver.accepted) == 2 and receiver.rejected == 1
            assert not await send_once(config)
            report.update(qualified=True, accepted_unique_events=len(receiver.accepted),
                          attempts=sum(receiver.attempts.values()),
                          receiver_rejected=receiver.rejected)
            server.should_exit = True
            await asyncio.wait_for(server_task, 5)
    finally:
        if server is not None:
            server.should_exit = True
        if server_task is not None and not server_task.done():
            await asyncio.wait_for(server_task, 5)
        if listener is not None:
            listener.close()
        if rule_id is not None:
            await db.execute("delete from public.alert_delivery_outbox where destination=$1",
                             config["destination"])
            await db.execute("delete from public.alerts where rule_id=$1", rule_id)
            await db.execute("delete from public.alert_rules where id=$1", rule_id)
            assert not await db.fetchval(
                "select exists(select 1 from public.alert_rules where id=$1)", rule_id)
        await db.close()
    report["cleanup"] = "receiver stopped; synthetic rule, alerts, outbox and secrets removed"
    output.write_text(json.dumps(report, indent=2))
    return report


def main():
    require_staging()
    disable_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--send-once", action="store_true")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--crash-after-ack", action="store_true")
    args = parser.parse_args()
    if args.send_once:
        config = json.loads(args.config.read_text())
        asyncio.run(send_once(config, crash=args.crash_after_ack))
    else:
        if (args.output is None
                or not args.output.resolve().is_relative_to(require_staging() / "evidence")):
            parser.error("--output must be inside staging evidence")
        print(json.dumps(asyncio.run(asyncio.wait_for(drill(args.output), 150))))


if __name__ == "__main__":
    main()
