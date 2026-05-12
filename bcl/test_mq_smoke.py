"""
MqClient smoke test against a real OCP pod.

Validates that the Layer 2 primitive (MqClient.apply_mqsc) works
end-to-end against a running queue manager. Does not go through the
BCL HTTP API — calls MqClient directly.

Update POD and QM below before running.
"""
import asyncio
import sys
sys.path.insert(0, "src")
from bcl.provisioning.mq_client import MqClient


# UPDATE these to match `oc get pods -l "intelliai2doto/owner=bcl"`
# and `oc exec <pod> -- dspmq`
POD = "qm-src-qm-cb-AAAAA-BBBBB"
QM = "SRC_QM_CB_QM"
NAMESPACE = "roco-dev"


async def main() -> None:
    c = MqClient(default_namespace=NAMESPACE)
    print(f"Binary: {c.binary}")
    print(f"Target: pod={POD}, qm={QM}")
    print()

    print("Test 1 - Single DEFINE")
    print("-" * 60)
    r = await c.apply_mqsc(
        qm_name=QM,
        pod_name=POD,
        mqsc_text="DEFINE QLOCAL('CLIENT.SMOKE.TEST') REPLACE",
    )
    print(f"  SUMMARY: {r.summary()}")
    print(f"  all_succeeded: {r.all_succeeded}")
    for cmd in r.per_command:
        print(f"  line {cmd.line_number}: {cmd.amq_code} ({cmd.severity}) {cmd.detail[:60]}")
    print()

    print("Test 2 - Batch with one syntax error")
    print("-" * 60)
    r = await c.apply_mqsc(
        qm_name=QM,
        pod_name=POD,
        mqsc_text="""DEFINE QLOCAL('CLIENT.BATCH.A') REPLACE
DEFINE QLOCAL('CLIENT.BATCH.B') REPLACE
DEFINE BOGUS('SHOULD.FAIL')
DEFINE QLOCAL('CLIENT.BATCH.C') REPLACE""",
    )
    print(f"  SUMMARY: {r.summary()}")
    print(f"  has_failures: {r.has_failures}")
    print(f"  all_succeeded: {r.all_succeeded}")
    for cmd in r.per_command:
        ok = "OK" if cmd.success else "FAIL"
        print(f"  line {cmd.line_number} [{ok}]: {cmd.amq_code} {cmd.detail[:60]}")
    print()

    print("Test 3 - DISPLAY the queues we created")
    print("-" * 60)
    r = await c.apply_mqsc(
        qm_name=QM,
        pod_name=POD,
        mqsc_text="""DISPLAY QLOCAL('CLIENT.SMOKE.TEST')
DISPLAY QLOCAL('CLIENT.BATCH.A')
DISPLAY QLOCAL('CLIENT.BATCH.C')""",
    )
    print(f"  SUMMARY: {r.summary()}")
    print(f"  per_command count: {len(r.per_command)}")
    for cmd in r.per_command:
        print(f"  line {cmd.line_number}: {cmd.amq_code} {cmd.detail[:60]}")
    print()

    print("Test 4 - Cleanup")
    print("-" * 60)
    r = await c.apply_mqsc(
        qm_name=QM,
        pod_name=POD,
        mqsc_text="""DELETE QLOCAL('CLIENT.SMOKE.TEST')
DELETE QLOCAL('CLIENT.BATCH.A')
DELETE QLOCAL('CLIENT.BATCH.B')
DELETE QLOCAL('CLIENT.BATCH.C')""",
    )
    print(f"  SUMMARY: {r.summary()}")
    print(f"  all_succeeded: {r.all_succeeded}")


if __name__ == "__main__":
    asyncio.run(main())