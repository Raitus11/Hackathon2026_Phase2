"""Jinja2 template renderer for K8s manifests.

Pure function: same inputs always produce identical YAML output.
This makes audit-log content deterministic per Lamport step and makes
provisioning testable without a real cluster.

Every byte sent to `oc apply` is also captured in the audit log payload,
so a rollback can reconstruct exact prior state.
"""

from __future__ import annotations

import base64
import secrets as stdlib_secrets
import string
from dataclasses import dataclass
from importlib import resources as importlib_resources
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from bcl.config import get_settings
from bcl.provisioning import naming


_PASSWORD_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class QMRenderInputs:
    """Inputs the provisioning engine passes to the renderer for one QM.

    `run_id` and `lamport_clock` are stamped into annotations of every
    resource so audits chain together cleanly.
    """

    app_id: str          # e.g. "APUMN/GC"
    qm_name: str         # e.g. "APUMN_GC_QM"  (already normalized)
    topology_id: int
    run_id: str
    lamport_clock: int
    admin_password: str
    app_password: str


@dataclass(frozen=True)
class RenderedManifests:
    """The four YAML strings to apply, in apply order.

    Order matters: PVC and Secret must exist before the Deployment can mount
    them; the Service can be created in parallel with the Deployment but
    we apply it last so callers see Service DNS appear only after the pod
    is wired up.
    """

    pvc: str
    secret: str
    deployment: str
    service: str

    def in_apply_order(self) -> list[tuple[str, str]]:
        """Returns list of (resource_kind, yaml_text) pairs in apply order."""
        return [
            ("PersistentVolumeClaim", self.pvc),
            ("Secret", self.secret),
            ("Deployment", self.deployment),
            ("Service", self.service),
        ]


def _templates_dir() -> Path:
    """Locate the templates directory relative to this module."""
    return Path(__file__).resolve().parent / "templates"


def _env() -> Environment:
    """Construct the Jinja2 environment.

    StrictUndefined: rendering fails loudly on a missing variable rather
    than silently emitting an empty string into YAML. This catches bugs at
    render time rather than at apply time.
    """
    return Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def generate_password(length: int = 32) -> str:
    """Generate a cryptographically random password.

    Uses `secrets.choice` (CSPRNG). Characters are URL-safe alphanumeric;
    we deliberately exclude punctuation to avoid shell-escaping concerns
    when the password ends up in MQSC `DEFINE` lines later.
    """
    return "".join(stdlib_secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def render_for_qm(inputs: QMRenderInputs) -> RenderedManifests:
    """Render all four K8s manifests for a single queue manager.

    The output is YAML text (not parsed dicts) — `oc apply -f -` consumes
    YAML directly, and we preserve formatting for human-readable audit
    log payloads.
    """
    settings = get_settings()
    env = _env()

    depl = naming.deployment_name(inputs.app_id)
    svc = naming.service_name(inputs.app_id)
    pvc = naming.pvc_name(inputs.app_id)
    sec = naming.secret_name(inputs.app_id)
    app_lbl = naming.app_label(inputs.app_id)

    # K8s labels must conform to RFC-1123 (DNS-1123 label) — same rules as
    # resource names. The raw app_id may contain "/" which is illegal.
    # Use the deployment name as the label value.
    app_id_label = naming.k8s_safe(inputs.app_id)

    base_ctx = {
        "namespace": settings.namespace,
        "topology_id": inputs.topology_id,
        "run_id": inputs.run_id,
        "lamport_clock": inputs.lamport_clock,
        "app_label": app_lbl,
        "app_id_label": app_id_label,
        "qm_name": inputs.qm_name,
        "pvc_name": pvc,
        "secret_name": sec,
        "deployment_name": depl,
        "service_name": svc,
        "mq_image": settings.mq_image,
        "listener_port": settings.mq_listener_port,
        "web_port": settings.mq_web_port,
        "cpu_request": settings.mq_pod_cpu_request,
        "cpu_limit": settings.mq_pod_cpu_limit,
        "memory_request": settings.mq_pod_memory_request,
        "memory_limit": settings.mq_pod_memory_limit,
        "storage_class": "sc-ontap-nas",
        "storage_size": "1Gi",
        "admin_password_b64": base64.b64encode(
            inputs.admin_password.encode("utf-8")
        ).decode("ascii"),
        "app_password_b64": base64.b64encode(
            inputs.app_password.encode("utf-8")
        ).decode("ascii"),
    }

    pvc_yaml = env.get_template("pvc.yaml.j2").render(base_ctx)
    secret_yaml = env.get_template("secret.yaml.j2").render(base_ctx)
    deployment_yaml = env.get_template("deployment.yaml.j2").render(base_ctx)
    service_yaml = env.get_template("service.yaml.j2").render(base_ctx)

    return RenderedManifests(
        pvc=pvc_yaml,
        secret=secret_yaml,
        deployment=deployment_yaml,
        service=service_yaml,
    )


def assert_yaml_valid(yaml_text: str, expected_kind: str) -> dict:
    """Parse YAML and confirm it's well-formed with the expected kind.

    Returns the parsed dict for callers that want to inspect (e.g. tests).
    Raises ValueError on parse failure or kind mismatch — lets template bugs
    fail at render time rather than at `oc apply` time.
    """
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"rendered manifest is not valid YAML: {e}") from e

    if not isinstance(parsed, dict):
        raise ValueError(
            f"rendered manifest did not produce a dict; got {type(parsed).__name__}"
        )

    if parsed.get("kind") != expected_kind:
        raise ValueError(
            f"rendered manifest kind mismatch: expected {expected_kind!r}, "
            f"got {parsed.get('kind')!r}"
        )

    return parsed


__all__ = [
    "QMRenderInputs",
    "RenderedManifests",
    "generate_password",
    "render_for_qm",
    "assert_yaml_valid",
]
