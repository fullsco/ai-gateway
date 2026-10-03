"""Disposable staging DB and process ownership for the installed-release drill."""

import asyncio
import hashlib
import json
import signal
import socket
import subprocess
from pathlib import Path
from time import monotonic
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from scripts.release_support import DATABASE_PATTERN, PORTS, ingress_config, release_dsn
from scripts.staging_boundary import clean_environment, require_staging, staging_dsn


def postgres(sql):
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At"],
        input=sql, text=True, capture_output=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Disposable qualification database administration failed")
    return result.stdout.strip()


async def fresh_database():
    require_staging()
    source = urlsplit(staging_dsn())
    name = "gateway_release_drill_" + uuid4().hex[:24]
    if not DATABASE_PATTERN.fullmatch(name):
        raise ValueError("Invalid disposable database name")
    if postgres(f"select oid from pg_database where datname='{name}'"):
        raise RuntimeError("Disposable database already exists")
    postgres(f'create database "{name}" owner gateway_qualification')
    dsn = release_dsn(urlunsplit(source._replace(path="/" + name)))
    try:
        import asyncpg

        from scripts.prepare_dbtest import prepare

        connection = await asyncpg.connect(dsn, timeout=5, command_timeout=10)
        try:
            await connection.execute(
                "create schema auth; create table auth.users(id uuid primary key)")
        finally:
            await connection.close()
        prepared = await prepare(dsn)
        if "036_alert_delivery_sequence_fix.sql" not in prepared["applied"]:
            raise RuntimeError("Release drill requires schema 036")
        return name, dsn, prepared["applied"]
    except BaseException:
        drop_database(name)
        raise


def drop_database(name):
    require_staging()
    if not DATABASE_PATTERN.fullmatch(name):
        raise ValueError("Refusing to drop a non-drill database")
    # No FORCE: an unexpected live connection must surface as incomplete cleanup.
    postgres(f'drop database "{name}"')
    if postgres(f"select oid from pg_database where datname='{name}'"):
        raise RuntimeError("Disposable database cleanup was not verified")


def verify_artifacts(artifacts):
    manifest = json.loads((artifacts / "manifest.json").read_text())
    for label, entry in manifest["versions"].items():
        if label not in {"baseline", "current"}:
            raise ValueError("Unexpected release label")
        wheel = (artifacts / entry["wheel"]).resolve(strict=True)
        if not wheel.is_relative_to(artifacts.resolve()):
            raise ValueError("Artifact path escapes the supplied directory")
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != entry["wheel_sha256"]:
            raise ValueError("Artifact checksum mismatch")
        if not (artifacts / label / "venv/bin/python").exists():
            raise ValueError("Each release requires its own installed environment")
    if set(manifest["versions"]) != {"baseline", "current"}:
        raise ValueError("Both release artifacts are required")
    return manifest


def ports_available():
    held = []
    try:
        for port in PORTS.values():
            listener = socket.socket()
            held.append(listener)
            listener.bind(("127.0.0.1", port))
    finally:
        for listener in held:
            listener.close()


def worker_pids(master_pid):
    path = Path(f"/proc/{master_pid}/task/{master_pid}/children")
    return {int(pid) for pid in path.read_text().split()}


def listening_sockets(port):
    return {f"socket:[{fields[9]}]"
            for line in Path("/proc/net/tcp").read_text().splitlines()[1:]
            if (fields := line.split())[3] == "0A"
            and fields[1] == f"0100007F:{port:04X}"}


def holds_listener(pid, sockets):
    try:
        return any(fd.readlink().as_posix() in sockets
                   for fd in Path(f"/proc/{pid}/fd").iterdir())
    except (FileNotFoundError, ProcessLookupError):
        return False


async def wait_for_handoff(process, previous, port):
    began = monotonic()
    async with asyncio.timeout(10):
        while True:
            if process.poll() is not None:
                raise RuntimeError("Owned ingress exited during handoff")
            sockets = listening_sockets(port)
            new = worker_pids(process.pid) - previous
            if (sockets and any(holds_listener(pid, sockets) for pid in new)
                    and not any(holds_listener(pid, sockets) for pid in previous)):
                return {"old_listeners_closed": True, "new_worker_listening": True,
                        "handoff_seconds": monotonic() - began}
            await asyncio.sleep(.02)


class Processes:
    def __init__(self, directory, artifacts):
        self.directory, self.artifacts = directory, artifacts
        self.processes, self.logs, self.exit_codes = {}, [], {}
        self.generations = {}

    def start(self, role):
        if role in self.processes:
            raise ValueError("Role is already running")
        label = "baseline" if role == "baseline" else "current"
        generation = self.generations.get(role, 0) + 1
        self.generations[role] = generation
        log = (self.directory / f"{role}-{generation}.log").open("xb")
        self.logs.append(log)
        evidence = self.directory / f"{role}-{generation}.json"
        self.processes[role] = subprocess.Popen([
            str(self.artifacts / label / "venv/bin/python"), "-I",
            str(Path(__file__).with_name("release_role.py")), role,
            "--config", str(self.directory / "config.json"),
            "--manifest", str(self.artifacts / "manifest.json"), "--evidence", str(evidence),
        ], cwd=self.directory, env=clean_environment(self.directory), stdout=log, stderr=log)

    def ingress(self):
        config = self.directory / "nginx.conf"
        config.write_text(ingress_config(self.directory))
        self._nginx_check(config)
        log = (self.directory / "nginx-output.log").open("xb")
        self.logs.append(log)
        self.processes["ingress"] = subprocess.Popen([
            "/usr/sbin/nginx", "-p", str(self.directory) + "/", "-c", str(config),
            "-g", "daemon off;",
        ], cwd=self.directory, env=clean_environment(self.directory), stdout=log, stderr=log)

    def _nginx_check(self, config):
        result = subprocess.run(["/usr/sbin/nginx", "-t", "-p", str(self.directory) + "/",
            "-c", str(config)], capture_output=True, timeout=10)
        if result.returncode:
            raise RuntimeError("Isolated ingress configuration did not validate")

    async def switch(self, rolled_back):
        config = self.directory / "nginx.conf"
        previous = config.read_text()
        config.write_text(ingress_config(self.directory, rolled_back))
        try:
            self._nginx_check(config)
            process = self.processes["ingress"]
            if process.poll() is not None:
                raise RuntimeError("Owned ingress is not running")
            previous_workers = worker_pids(process.pid)
            if not previous_workers:
                raise RuntimeError("Owned ingress has no workers")
            process.send_signal(signal.SIGHUP)
            return await wait_for_handoff(process, previous_workers, PORTS["ingress"])
        except BaseException:
            config.write_text(previous)
            raise

    async def stop(self, role):
        process = self.processes.pop(role, None)
        if process is None:
            return
        if process.poll() is None:
            process.send_signal(signal.SIGQUIT if role == "ingress" else signal.SIGTERM)
        try:
            code = await asyncio.wait_for(asyncio.to_thread(process.wait), 30)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)
            self.exit_codes[f"{role}-{self.generations.get(role, 1)}"] = "forced_kill"
            raise
        self.exit_codes[f"{role}-{self.generations.get(role, 1)}"] = code

    async def close(self):
        errors = []
        for role in reversed(list(self.processes)):
            try:
                await self.stop(role)
            except Exception as exc:
                errors.append(type(exc).__name__)
        for log in self.logs:
            log.close()
        if errors:
            raise RuntimeError("Release role cleanup failed: " + ",".join(errors))


async def ready(http, port, process=None):
    import httpx

    async with asyncio.timeout(30):
        while True:
            if process is not None and process.poll() is not None:
                raise RuntimeError("Release role exited before readiness")
            try:
                response = await http.get(f"http://127.0.0.1:{port}/health", timeout=1)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.1)
