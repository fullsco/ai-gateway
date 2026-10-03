import asyncio
import hashlib
import json
from datetime import UTC, datetime
import asyncpg
from fastapi.testclient import TestClient
from gateway.app import create_app
from gateway.config import Settings
from gateway.configuration.postgres import PostgresSnapshotRepository, create_pool
from gateway.configuration.snapshots import CachedConfiguration, configuration_checksum
from gateway.configuration.runtime_builder import RuntimeBuilder
from gateway.configuration.runtime_manager import RuntimeManager

TGT_URL = 'postgresql://postgres.nebbtrmwzbpmzvinhrch:8u8PR9KwxDDLEqw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres'
ENC_KEY = 'MzWlvYWVEGNaXxZqlHqliMblGTZKMKxIraxYdOW3bZQ='
PEPPER = 'HLlUhr4vW0NKCCbXIJpTGgjM81waQ0y8nio/bl6p9iQ='

async def main():
    print("==================================================")
    print("PHASE 6: VALIDATION AGAINST TARGET DATABASE")
    print("Target DB Ref: nebbtrmwzbpmzvinhrch")
    print("==================================================")

    # 1. Verify database connectivity
    print("\n[Check 1 & 2] Testing direct database connectivity & pool creation...")
    pool = await create_pool(TGT_URL)
    row = await pool.fetchrow("SELECT current_database(), current_user, version();")
    print(f"  Connected to DB: {row['current_database']} as {row['current_user']}")

    # 2. Verify config_versions & loading
    print("\n[Check 3 & 4] Testing config_versions table & loading published snapshot...")
    repo = PostgresSnapshotRepository(pool)
    cache = CachedConfiguration(repo)
    snapshot = await cache.refresh()
    print(f"  Published config loaded: version={snapshot.version}, checksum={snapshot.checksum[:16]}...")
    assert snapshot.version == 283, f"Expected version 283, got {snapshot.version}"

    # 3. Verify provider configuration & runtime initialization
    print("\n[Check 5 & 6] Initializing RuntimeManager with RuntimeBuilder...")
    builder = RuntimeBuilder(encryption_key=ENC_KEY, key_pepper=PEPPER)
    manager = RuntimeManager(cache, builder)
    runtime = await manager.refresh()
    print(f"  Runtime configured successfully: version={manager.version}")
    models = runtime.model_registry.list_enabled()
    print(f"  Models loaded ({len(models)}): {[m.id for m in models]}")
    print(f"  Credentials decrypted ({len(runtime.credentials)})")
    print(f"  Provider adapters active ({len(runtime.provider_model_adapters)})")
    assert len(models) >= 10
    assert len(runtime.credentials) == 50

    # 4. Verify model resolution and aliases
    print("\n[Check 9 & 10] Verifying model registry and aliases...")
    test_model = "claude-opus-5"
    m = runtime.model_registry.resolve(test_model)
    print(f"  Resolved '{test_model}': id={m.id}, aliases={list(m.aliases)}")
    assert m and m.id == test_model

    # 5. Verify Health and Ready endpoints with FastAPI TestClient
    print("\n[Check 7, 8, 11] Verifying /health and /ready endpoints via test app...")
    settings = Settings(
        database_url=TGT_URL,
        credential_encryption_key=ENC_KEY,
        key_pepper=PEPPER,
        environment="production",
        config_refresh_seconds=60,
        live_state_refresh_seconds=30,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        h_resp = client.get("/health")
        assert h_resp.status_code == 200 and h_resp.text == "ok", f"Health failed: {h_resp.status_code}"
        print("  GET /health -> 200 OK ('ok')")

        r_resp = client.get("/ready")
        assert r_resp.status_code == 200 and r_resp.json().get("status") == "ready", f"Ready failed: {r_resp.status_code}"
        print("  GET /ready -> 200 OK ({'status': 'ready'})")

        # Test unauthenticated /v1/models
        unauth_resp = client.get("/v1/models")
        assert unauth_resp.status_code == 401
        print("  GET /v1/models without auth -> 401 Unauthorized (Auth enforcement verified)")

    # 6. Verify database writes: write and retrieve a test request log row
    print("\n[Check 12 & 13] Verifying database writes to target...")
    test_req_id = f"test_val_{int(datetime.now(UTC).timestamp())}"
    await pool.execute("""
        INSERT INTO public.request_logs (
            id, protocol, requested_model, status, started_at, retry_count, fallback_count
        ) VALUES (
            $1, 'openai_chat_completions', 'gpt-5.6-sol', 'success', now(), 0, 0
        );
    """, test_req_id)
    written = await pool.fetchrow("SELECT id, status FROM public.request_logs WHERE id = $1", test_req_id)
    assert written and written["id"] == test_req_id
    print(f"  Successfully inserted and read back test request log: id={written['id']}, status={written['status']}")
    # Clean up test row
    await pool.execute("DELETE FROM public.request_logs WHERE id = $1", test_req_id)
    print("  Cleaned up test request log row.")

    # 7. Test configuration invalidation specifically
    print("\n[Check 14, 15, 16] Testing Configuration Invalidation & Refresh...")
    # Step A: Cache hit verification
    snap1 = await cache.refresh()
    snap2 = await cache.refresh()
    assert snap1 is snap2, "Unchanged config must return identical cached object!"
    print("  Step A: Metadata polling detected unchanged version -> Reused cached snapshot (0 payload download)")

    # Step B: Insert a test draft/published version safely with valid canonical checksum
    current_row = await pool.fetchrow("SELECT schema_version, payload FROM public.config_versions WHERE status = 'published';")
    raw_payload = current_row["payload"]
    test_payload = json.loads(raw_payload) if isinstance(raw_payload, str) else dict(raw_payload)
    test_payload["models"].append({
        "id": "test-validation-model",
        "capabilities": [],
        "context_window": None,
        "enabled": False,
        "aliases": []
    })
    new_checksum = configuration_checksum(test_payload)

    # Publish test version
    await pool.execute("UPDATE public.config_versions SET status = 'superseded' WHERE status = 'published';")
    test_version_id = await pool.fetchval("""
        INSERT INTO public.config_versions (status, schema_version, payload, checksum, published_at)
        VALUES ('published', $1, $2, $3, now())
        RETURNING id;
    """, current_row["schema_version"], json.dumps(test_payload), new_checksum)
    print(f"  Step B: Published temporary test config version id={test_version_id}, checksum={new_checksum[:16]}...")

    # Step C: Metadata polling detects new version and fetches full payload ONCE
    new_snap = await cache.refresh()
    assert new_snap.version == test_version_id, f"Expected version {test_version_id}, got {new_snap.version}"
    assert new_snap.checksum == new_checksum, f"Expected checksum {new_checksum}, got {new_snap.checksum}"
    print(f"  Step C: Metadata polling detected new version {new_snap.version} -> Successfully fetched new payload!")

    # Step D: Subsequent polling uses cached configuration
    cached_new = await cache.refresh()
    assert cached_new is new_snap, "Subsequent refresh must use cached snapshot!"
    print("  Step D: Subsequent polling tick reused cached snapshot (0 payload download)")

    # Step E: Restore original version 283
    await pool.execute("DELETE FROM public.config_versions WHERE id = $1;", test_version_id)
    await pool.execute("UPDATE public.config_versions SET status = 'published' WHERE id = 283;")
    fresh_cache = CachedConfiguration(repo)
    restored_snap = await fresh_cache.refresh()
    assert restored_snap.version == 283
    print("  Step E: Restored original version 283 as published and verified clean load!")

    await pool.close()
    print("\n==================================================")
    print("PHASE 6 VALIDATION COMPLETED: ALL 16 CHECKS PASSED")
    print("==================================================")

asyncio.run(main())
