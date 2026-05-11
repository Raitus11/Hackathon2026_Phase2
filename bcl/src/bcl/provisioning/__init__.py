"""IntelliAI 2.0 — provisioning subsystem.

Renders K8s manifests (PVC, Secret, Deployment, Service) for each queue
manager in a topology, applies them in order via the `oc` CLI, waits for
Deployment Available=True, and writes a full audit-trail of every step.

Public API:
    engine.start_provision_run(...)
    engine.get_run(...)
    engine.list_runs_for_topology(...)
    K8sClient
    K8sResult
"""

from __future__ import annotations
