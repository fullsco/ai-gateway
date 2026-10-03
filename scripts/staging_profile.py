"""Read-only, bounded staging CPU and PostgreSQL wait sampler; never captures SQL text."""

import os
import subprocess
from collections import Counter
from pathlib import Path
from time import monotonic


def cpu_counters(path):
    return {key: int(value) for key, value in
            (line.split() for line in (path / "cpu.stat").read_text().splitlines())}


class StagingProfile:
    def __init__(self):
        group = subprocess.check_output([
            "systemctl", "show", "gateway-qualification-services.slice",
            "--property=ControlGroup", "--value",
        ], text=True, timeout=5).strip()
        if not group.startswith("/") or ".." in Path(group).parts:
            raise ValueError("Invalid staging service cgroup")
        self.services = Path("/sys/fs/cgroup") / group.lstrip("/")
        own = next(line[3:] for line in Path(f"/proc/{os.getpid()}/cgroup").read_text()
                   .splitlines() if line.startswith("0::"))
        self.generator = Path("/sys/fs/cgroup") / own.lstrip("/")
        self.roles = {}
        for name in ("api-a", "api-b", "worker", "maintenance", "provider", "dbproxy"):
            role_group = subprocess.check_output([
                "systemctl", "show", f"gateway-qualification-isolated-{name}.service",
                "--property=ControlGroup", "--value",
            ], text=True, timeout=5).strip()
            if not role_group.startswith(group + "/"):
                raise ValueError("Role outside qualification slice")
            self.roles[name] = Path("/sys/fs/cgroup") / role_group.lstrip("/")
        self.samples = []
        self.waits = Counter()

    async def start(self, db):
        if await db.fetchval("select current_database()") != "gateway_qualification":
            raise ValueError("Profiling requires the isolated staging database")
        self.started = monotonic()
        self.before = {name: cpu_counters(path) for name, path in self.paths().items()}
        self.db_before = await self.database_counters(db)

    def paths(self):
        return {"services": self.services, "generator": self.generator, **self.roles}

    async def database_counters(self, db):
        return dict(await db.fetchrow(
            "select xact_commit,xact_rollback,blks_read,blks_hit,deadlocks,temp_bytes "
            "from pg_stat_database where datname=current_database()"
        ))

    async def sample(self, db):
        if len(self.samples) >= 1800:
            return
        began = monotonic()
        rows = await db.fetch(
            "select state,wait_event_type,wait_event,count(*) as sessions,"
            "max(extract(epoch from clock_timestamp()-xact_start)) as oldest_xact_seconds "
            "from pg_stat_activity where datname=current_database() and pid<>pg_backend_pid() "
            "group by state,wait_event_type,wait_event"
        )
        states = []
        for row in rows:
            value = dict(row)
            age = value["oldest_xact_seconds"]
            value["oldest_xact_seconds"] = float(age) if age is not None else None
            states.append(value)
            key = "/".join(str(value[k]) for k in ("state", "wait_event_type", "wait_event"))
            self.waits[key] += value["sessions"]
        self.samples.append({
            "elapsed_seconds": monotonic() - self.started,
            "probe_seconds": monotonic() - began,
            "cpu": {name: cpu_counters(path) for name, path in self.paths().items()},
            "database_states": states,
        })

    async def finish(self, db):
        after = await self.database_counters(db)
        return {
            "elapsed_seconds": monotonic() - self.started,
            "sample_interval_seconds": 1, "sample_count": len(self.samples),
            "cpu_max": {name: (path / "cpu.max").read_text().strip()
                        for name, path in {"services": self.services,
                                           "generator": self.generator}.items()},
            "cpu_deltas": {
                name: {key: value - self.before[name].get(key, 0)
                       for key, value in cpu_counters(path).items()}
                for name, path in self.paths().items()
            },
            "database_counter_deltas": {key: value - self.db_before[key]
                                        for key, value in after.items()},
            "session_sample_counts": dict(self.waits), "samples": self.samples,
        }
