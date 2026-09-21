import asyncio
import asyncpg
import json

DB_URL = "postgresql://postgres.bxpclohykdkbhnwizuop:8u8PR9KwxDDLEqw@aws-0-eu-central-1.pooler.supabase.com:5432/postgres"

async def run_audit():
    conn = await asyncpg.connect(DB_URL)
    report = {}

    # 1. Extensions
    exts = await conn.fetch("SELECT extname, extversion FROM pg_extension ORDER BY extname;")
    report["extensions"] = [{"name": r["extname"], "version": r["extversion"]} for r in exts]

    # 2. Schemas
    schemas = await conn.fetch("SELECT schema_name FROM information_schema.schemata ORDER BY schema_name;")
    report["schemas"] = [r["schema_name"] for r in schemas]

    # 3. Enums
    enums = await conn.fetch("""
        SELECT n.nspname as schema, t.typname as enum_name, array_agg(e.enumlabel ORDER BY e.enumsortorder) as values
        FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        GROUP BY n.nspname, t.typname
        ORDER BY n.nspname, t.typname;
    """)
    report["enums"] = [{"schema": r["schema"], "name": r["enum_name"], "values": list(r["values"])} for r in enums]

    # 4. Domains
    domains = await conn.fetch("""
        SELECT n.nspname as schema, t.typname as domain_name, pg_catalog.format_type(t.typbasetype, t.typtypmod) as base_type
        FROM pg_type t
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE t.typtype = 'd' AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY n.nspname, t.typname;
    """)
    report["domains"] = [{"schema": r["schema"], "name": r["domain_name"], "type": r["base_type"]} for r in domains]

    # 5. Tables in public (and supabase_migrations)
    tables = await conn.fetch("""
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema IN ('public', 'supabase_migrations')
          AND table_type = 'BASE TABLE'
        ORDER BY table_schema, table_name;
    """)
    report["tables"] = []
    for t in tables:
        schema = t["table_schema"]
        tname = t["table_name"]
        
        # columns
        cols = await conn.fetch("""
            SELECT column_name, data_type, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = $1 AND table_name = $2
            ORDER BY ordinal_position;
        """, schema, tname)
        
        # row count
        count = await conn.fetchval(f'SELECT count(*) FROM "{schema}"."{tname}";')
        
        # primary key
        pks = await conn.fetch("""
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = $1 AND tc.table_name = $2
            ORDER BY kcu.ordinal_position;
        """, schema, tname)
        
        # foreign keys
        fks = await conn.fetch("""
            SELECT
                tc.constraint_name,
                kcu.column_name,
                ccu.table_schema AS foreign_table_schema,
                ccu.table_name AS foreign_table_name,
                ccu.column_name AS foreign_column_name,
                rc.delete_rule,
                rc.update_rule
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            JOIN information_schema.referential_constraints rc
              ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.constraint_column_usage ccu
              ON ccu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = $1 AND tc.table_name = $2;
        """, schema, tname)
        
        # unique constraints
        uniques = await conn.fetch("""
            SELECT tc.constraint_name, array_agg(kcu.column_name ORDER BY kcu.ordinal_position) as cols
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
              AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'UNIQUE'
              AND tc.table_schema = $1 AND tc.table_name = $2
            GROUP BY tc.constraint_name;
        """, schema, tname)
        
        # check constraints
        checks = await conn.fetch("""
            SELECT tc.constraint_name, cc.check_clause
            FROM information_schema.table_constraints tc
            JOIN information_schema.check_constraints cc
              ON tc.constraint_name = cc.constraint_name
            WHERE tc.constraint_type = 'CHECK'
              AND tc.table_schema = $1 AND tc.table_name = $2
              AND tc.constraint_name NOT LIKE '%_not_null';
        """, schema, tname)

        # indexes
        indexes = await conn.fetch("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname = $1 AND tablename = $2
            ORDER BY indexname;
        """, schema, tname)

        # triggers
        triggers = await conn.fetch("""
            SELECT trigger_name, action_timing, event_manipulation, action_statement
            FROM information_schema.triggers
            WHERE event_object_schema = $1 AND event_object_table = $2
            ORDER BY trigger_name;
        """, schema, tname)

        # RLS enabled?
        rls = await conn.fetchval("""
            SELECT rowsecurity FROM pg_tables WHERE schemaname = $1 AND tablename = $2;
        """, schema, tname)

        # RLS policies
        policies = await conn.fetch("""
            SELECT polname, polcmd, polroles::regrole[] as roles, pg_get_expr(polqual, polrelid) as using_expr, pg_get_expr(polwithcheck, polrelid) as check_expr
            FROM pg_policy
            WHERE polrelid = (quote_ident($1) || '.' || quote_ident($2))::regclass;
        """, schema, tname)

        report["tables"].append({
            "schema": schema,
            "table": tname,
            "row_count": count,
            "row_security": rls,
            "columns": [{"name": c["column_name"], "type": c["data_type"], "udt": c["udt_name"], "nullable": c["is_nullable"], "default": c["column_default"]} for c in cols],
            "primary_key": [p["column_name"] for p in pks],
            "foreign_keys": [{
                "name": f["constraint_name"],
                "column": f["column_name"],
                "foreign_table": f"{f['foreign_table_schema']}.{f['foreign_table_name']}",
                "foreign_column": f["foreign_column_name"],
                "on_delete": f["delete_rule"],
                "on_update": f["update_rule"]
            } for f in fks],
            "unique_constraints": [{
                "name": u["constraint_name"],
                "columns": list(u["cols"])
            } for u in uniques],
            "check_constraints": [{
                "name": ch["constraint_name"],
                "clause": ch["check_clause"]
            } for ch in checks],
            "indexes": [{"name": i["indexname"], "def": i["indexdef"]} for i in indexes],
            "triggers": [{"name": tr["trigger_name"], "timing": tr["action_timing"], "event": tr["event_manipulation"], "statement": tr["action_statement"]} for tr in triggers],
            "policies": [{"name": pol["polname"], "command": pol["polcmd"], "roles": str(pol["roles"]), "using": pol["using_expr"], "check": pol["check_expr"]} for pol in policies]
        })

    # 6. Sequences
    seqs = await conn.fetch("""
        SELECT sequence_schema, sequence_name, data_type, start_value, minimum_value, maximum_value, increment
        FROM information_schema.sequences
        WHERE sequence_schema IN ('public', 'supabase_migrations')
        ORDER BY sequence_schema, sequence_name;
    """)
    report["sequences"] = [dict(s) for s in seqs]

    # 7. Views & Materialized Views
    views = await conn.fetch("""
        SELECT table_schema, table_name, view_definition
        FROM information_schema.views
        WHERE table_schema IN ('public')
        ORDER BY table_schema, table_name;
    """)
    report["views"] = [dict(v) for v in views]

    matviews = await conn.fetch("""
        SELECT schemaname, matviewname, definition
        FROM pg_matviews
        WHERE schemaname IN ('public')
        ORDER BY schemaname, matviewname;
    """)
    report["materialized_views"] = [dict(mv) for mv in matviews]

    # 8. Functions and Procedures in public
    funcs = await conn.fetch("""
        SELECT
            n.nspname as schema,
            p.proname as name,
            pg_get_function_identity_arguments(p.oid) as identity_args,
            pg_get_function_result(p.oid) as result_type,
            p.prokind as kind,
            p.prosecdef as security_definer,
            pg_get_functiondef(p.oid) as definition
        FROM pg_proc p
        JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
        ORDER BY p.proname;
    """)
    report["functions"] = [{
        "schema": f["schema"],
        "name": f["name"],
        "args": f["identity_args"],
        "result_type": f["result_type"],
        "kind": f["kind"],
        "security_definer": f["security_definer"],
        "definition": f["definition"]
    } for f in funcs]

    # 9. Auth users
    auth_users = await conn.fetch("SELECT count(*) FROM auth.users;")
    report["auth_users_count"] = auth_users[0][0]

    # 10. Storage buckets and objects
    try:
        buckets = await conn.fetch("SELECT id, name, public FROM storage.buckets;")
        objects = await conn.fetch("SELECT count(*) FROM storage.objects;")
        report["storage"] = {"buckets": [dict(b) for b in buckets], "objects_count": objects[0][0]}
    except Exception as e:
        report["storage"] = {"error": str(e)}

    # 11. Vault secrets
    try:
        vault_secrets = await conn.fetch("SELECT count(*) FROM vault.secrets;")
        report["vault_secrets_count"] = vault_secrets[0][0]
    except Exception as e:
        report["vault_secrets_count"] = str(e)

    # 12. Realtime publication tables
    try:
        realtime_pub = await conn.fetch("""
            SELECT schemaname, tablename FROM pg_publication_tables WHERE pubname = 'supabase_realtime';
        """)
        report["realtime_tables"] = [dict(r) for r in realtime_pub]
    except Exception as e:
        report["realtime_tables"] = str(e)

    # 13. Grants on public tables and functions
    grants = await conn.fetch("""
        SELECT grantee, table_schema, table_name, privilege_type
        FROM information_schema.role_table_grants
        WHERE table_schema = 'public'
        ORDER BY table_name, grantee, privilege_type;
    """)
    report["table_grants"] = [dict(g) for g in grants]

    func_grants = await conn.fetch("""
        SELECT grantee, routine_schema, routine_name, privilege_type
        FROM information_schema.routine_privileges
        WHERE routine_schema = 'public'
        ORDER BY routine_name, grantee, privilege_type;
    """)
    report["routine_grants"] = [dict(fg) for fg in func_grants]

    # 14. Roles in database
    roles = await conn.fetch("""
        SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin
        FROM pg_roles
        WHERE rolname NOT LIKE 'pg_%'
        ORDER BY rolname;
    """)
    report["roles"] = [dict(r) for r in roles]

    await conn.close()

    with open("/root/ai-gateway/scripts/source_db_audit.json", "w") as f:
        json.dump(report, f, indent=2, default=str)
    print("Audit written to /root/ai-gateway/scripts/source_db_audit.json")

asyncio.run(run_audit())
