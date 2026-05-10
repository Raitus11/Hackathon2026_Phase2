# Running IntelliAI 2.0 — Phase 0 Demo

This is what you can run right now. The BCL FastAPI server is fully functional;
the UI is coming in the next code drop.

## Prereqs

- Python 3.12.x with the venv at `bcl/.venv` activated
- All deps installed via `pip install -e ".[dev]"` from `bcl/`

If you haven't done that yet, see `RUN_LOCALLY.md` setup section.

## Option 1 — Start the server, browse the API

The simplest demo. Open VS Code's terminal in `bcl/` with the venv active:

```powershell
uvicorn bcl.api.main:app --reload --port 8080
```

You'll see something like:

```
INFO:     Will watch for changes in these directories: ['D:\Hackathon2026Phase2\intelliai2doto\bcl']
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
INFO:     Started reloader process
INFO:bcl.api.main:starting intelliai-2-bcl v0.1.0 in dev mode (namespace=roco-dev)
INFO:bcl.api.main:database schema verified (10 tables)
INFO:bcl.api.main:Lamport clock bootstrapped to 0
INFO:     Application startup complete.
```

Now open these in your browser:

- **http://localhost:8080/docs** — Swagger UI. Click endpoints, try them, see live responses.
- **http://localhost:8080/redoc** — Cleaner API reference doc.
- **http://localhost:8080/health/live** — Returns `{"status":"ok"}`
- **http://localhost:8080/health/ready** — Returns full system health JSON.
- **http://localhost:8080/** — Returns service info (team name, version, links).
- **http://localhost:8080/openapi.json** — The OpenAPI 3.1 contract.

`Ctrl+C` to stop. The `--reload` flag means saving any `.py` file restarts the server.

## Option 2 — Run the smoke test (no browser needed)

A single-shot end-to-end exercise of every endpoint. Outputs to terminal.

In VS Code: open `bcl/_smoke.py` and click the ▶ play button.
Or in terminal:

```powershell
python _smoke.py
```

Expected output:

```
============================================================
IntelliAI 2.0 — Phase 0 smoke test
============================================================

[1/6] GET /         → 200
        "service": "intelliai-2-bcl",
          "product": "IntelliAI 2.0",
          "team": "intelliAI2DotO",
          "version": "0.1.0",
          "docs": "/docs",
          "openapi": "/openapi.json"
}

[2/6] GET /health/live  → 200 {'status': 'ok'}

[3/6] GET /health/ready → 200
      status=healthy  db=True  lamport_clock=0

[4/6] POST /topologies → 201
      created topology id=1 name='smoke-source' kind=SOURCE
      qms=['SRC_QM_CB']

[5/6] GET /topologies → 200 (1 topologies)
      - id=1 name='smoke-source' kind=SOURCE

[6/6] GET /audit → 200 (1 entries, total=1)
      LC=  1  TOPOLOGY_CREATED           success=True  actor=smoke-test

============================================================
All checks passed. The BCL foundation is live.
============================================================
```

If you re-run, the topology will already exist and POST returns 409.
Delete `bcl/local-data/bcl.db` to reset.

## Option 3 — Drive it with curl from a 3rd terminal

While `uvicorn` is running in one terminal, in another:

```powershell
# Health
curl http://localhost:8080/health/live
curl http://localhost:8080/health/ready

# Create a topology
curl -X POST http://localhost:8080/topologies `
  -H "Content-Type: application/json" `
  -H "X-Actor: raitus" `
  -d '{
    "name": "demo-source",
    "kind": "SOURCE",
    "flows": [{
      "flow_type": "Local",
      "producer_app_id": "APUMN/GC",
      "producer_app_name": "Loan Approval",
      "producer_neighbourhood": "Core Banking",
      "producer_queue_manager": "SRC_QM_CB",
      "producer_queue_name": "APUMN.LOAN.IN",
      "producer_queue_type": "Local",
      "consumer_app_id": "APUMN/GC",
      "consumer_app_name": "Loan Approval",
      "consumer_neighnourhood": "Core Banking",
      "consumer_queue_manager": "SRC_QM_CB",
      "consumer_queue_name": "APUMN.LOAN.IN",
      "consumer_queue_type": "Local"
    }]
  }'

# List topologies
curl http://localhost:8080/topologies

# Audit log
curl http://localhost:8080/audit?include_total=true

# Single audit entry
curl http://localhost:8080/audit/1
```

Note PowerShell uses backtick (`` ` ``) for line-continuation, not backslash.

## What's in the database

After running any of the above, a SQLite file exists at:

```
bcl/local-data/bcl.db
```

10 tables, populated with whatever you've POSTed. To inspect:

- VS Code: install the **SQLite Viewer** extension, click the file in the tree
- CLI: `sqlite3 bcl/local-data/bcl.db ".tables"`
- Python: `import sqlite3; conn = sqlite3.connect("local-data/bcl.db"); ...`

Reset the DB by deleting the file. The schema re-creates on next startup.

## What this demonstrates

| Brief moat | Working today | Evidence |
|---|---|---|
| BCL as system of record | Yes | All ops go through `/topologies`, audit-logged automatically |
| Lamport-clocked audit trail | Yes | Every state change increments LC, persisted in `audit_log` table |
| Guardrails on every write | Partially | Pydantic enforces flow consistency, MQ name format, type compat. Full 10-constraint module comes next drop. |
| OpenAPI contract | Yes | `docs/openapi.json` has 7 paths, 12 schemas |
| Production-quality ops | Foundation only | `/health/live` and `/health/ready` work, structured logs work, K8s probes wireable |
| Topology persistence | Yes | Source/target spec accepted, parsed, persisted, queryable |
| MQ pod provisioning | Not yet | Next code drop |
| UI Control Plane | Not yet | Next code drop |

## What this does NOT do yet

- Provision real MQ pods on OCP (next drop)
- Render a UI (next drop — Next.js shell + Overview tab)
- Run the migration state machine
- Talk to MQ
- Have any agents

These come incrementally over the next ~5 drops. Phase 0 is the foundation
that everything else hooks into; if it didn't run, nothing else would.

## If something doesn't work

**`uvicorn: command not found`** — venv isn't active. `.\.venv\Scripts\Activate.ps1`

**`ModuleNotFoundError: No module named 'bcl'`** — venv is active but you didn't `pip install -e ".[dev]"`. Run that.

**`Address already in use`** — port 8080 is taken. Use `--port 8081`.

**Server starts but `/health/ready` says `db=False`** — the SQLite file path is wrong. Check `bcl/local-data/` directory exists (the lifespan code creates it; if running from outside `bcl/`, paths get weird).

**Anything else** — paste the error to me, I'll diagnose.
