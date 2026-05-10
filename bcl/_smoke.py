"""Phase 0 smoke test — run end-to-end in one shot.

Exercises every endpoint without needing a separate uvicorn process.
Useful for verifying after a fresh git pull or dependency change.

Run from VS Code: open this file, click the ▶ play button.
Run from terminal: `python _smoke.py`
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from httpx import ASGITransport, AsyncClient

from bcl.api.main import app


SAMPLE_TOPOLOGY = {
    "name": "smoke-source",
    "kind": "SOURCE",
    "flows": [
        {
            "flow_type": "Local",
            "producer_app_id": "APUMN/GC",
            "producer_app_name": "Loan Approval Service",
            "producer_neighbourhood": "Core Banking",
            "producer_queue_manager": "SRC_QM_CB",
            "producer_queue_name": "APUMN.LOAN.IN",
            "producer_queue_type": "Local",
            "consumer_app_id": "APUMN/GC",
            "consumer_app_name": "Loan Approval Service",
            "consumer_neighnourhood": "Core Banking",
            "consumer_queue_manager": "SRC_QM_CB",
            "consumer_queue_name": "APUMN.LOAN.IN",
            "consumer_queue_type": "Local",
        }
    ],
}


async def main() -> None:
    print("=" * 60)
    print("IntelliAI 2.0 — Phase 0 smoke test")
    print("=" * 60)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            # 1. Root
            r = await client.get("/")
            assert r.status_code == 200
            print(f"\n[1/6] GET /         → {r.status_code}")
            print(f"      {json.dumps(r.json(), indent=10)[10:].rstrip()}")

            # 2. Liveness
            r = await client.get("/health/live")
            assert r.status_code == 200
            print(f"\n[2/6] GET /health/live  → {r.status_code} {r.json()}")

            # 3. Readiness
            r = await client.get("/health/ready")
            assert r.status_code == 200
            j = r.json()
            print(f"\n[3/6] GET /health/ready → {r.status_code}")
            print(f"      status={j['status']}  db={j['db_reachable']}  "
                  f"lamport_clock={j['lamport_clock']}")

            # 4. Create topology
            r = await client.post(
                "/topologies",
                json=SAMPLE_TOPOLOGY,
                headers={"X-Actor": "smoke-test"},
            )
            assert r.status_code == 201, f"unexpected: {r.status_code} {r.text}"
            t = r.json()
            print(f"\n[4/6] POST /topologies → {r.status_code}")
            print(f"      created topology id={t['id']} name='{t['name']}' "
                  f"kind={t['kind']}")
            print(f"      qms={[q['qm_name'] for q in t['queue_managers']]}")

            # 5. List topologies
            r = await client.get("/topologies")
            assert r.status_code == 200
            print(f"\n[5/6] GET /topologies → {r.status_code} "
                  f"({len(r.json())} topologies)")
            for t in r.json():
                print(f"      - id={t['id']} name='{t['name']}' kind={t['kind']}")

            # 6. Audit log
            r = await client.get("/audit?include_total=true")
            assert r.status_code == 200
            j = r.json()
            print(f"\n[6/6] GET /audit → {r.status_code} "
                  f"({len(j['entries'])} entries, total={j['total_count']})")
            for e in j["entries"]:
                print(f"      LC={e['lamport_clock']:3}  {e['operation']:25}  "
                      f"success={e['success']}  actor={e['actor']}")

    print()
    print("=" * 60)
    print("All checks passed. The BCL foundation is live.")
    print("=" * 60)
    print()
    print("Next steps:")
    print("  1. Start the BCL for real:")
    print("       uvicorn bcl.api.main:app --reload --port 8080")
    print("  2. Open in browser:")
    print("       http://localhost:8080/docs    (Swagger UI)")
    print("       http://localhost:8080/redoc   (cleaner reference)")
    print("  3. Inspect SQLite DB:")
    print("       bcl/local-data/bcl.db")
    print()


if __name__ == "__main__":
    asyncio.run(main())
