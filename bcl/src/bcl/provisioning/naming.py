"""Naming helpers for K8s resources.

Maps internal entities (apps, queue managers) to RFC-1123 / RFC-1035
K8s names. These rules are enforced strictly because K8s rejects names
that look fine in human-typed YAML but violate the spec.

Rules:
  - DNS-1123 label: max 63 chars, lowercase, [a-z0-9-], must start/end alpha-num.
  - IBM MQ name: max 48 chars, [A-Z0-9._/] — uppercase by convention.
  - We adopt the WF prototype's pattern (qm-1, qm-2) but parameterize by app.

References:
  - Kubernetes naming: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/
  - IBM MQ object names: https://www.ibm.com/docs/en/ibm-mq/9.4?topic=mq-naming-rules
"""

from __future__ import annotations

import hashlib
import re

# RFC-1123 label: lowercase letters, digits, hyphens. Must start+end alnum.
_K8S_NAME_INVALID = re.compile(r"[^a-z0-9-]")
_K8S_NAME_LEADING_TRAILING = re.compile(r"^-+|-+$")
_K8S_NAME_MAX = 63

# IBM MQ object names: uppercase letters, digits, underscores, dots, slashes.
_MQ_NAME_INVALID = re.compile(r"[^A-Z0-9._/]")
_MQ_NAME_MAX = 48


def k8s_safe(value: str, *, prefix: str = "", max_len: int = _K8S_NAME_MAX) -> str:
    """Return an RFC-1123 label derived from `value`.

    If the result would be too long, deterministically truncate and append
    a short hash suffix so the name remains stable and unique-ish.

    Example:
        k8s_safe("APUMN/GC", prefix="qm-") -> "qm-apumn-gc"
        k8s_safe("a_very_long_application_id_that_exceeds_limits") -> "..."

    The returned name is always non-empty and starts with an alpha character.
    """
    if not value:
        raise ValueError("k8s_safe: empty value")

    # Lowercase, replace non-alnum-or-hyphen with hyphen
    normalized = _K8S_NAME_INVALID.sub("-", value.lower())
    # Collapse runs of hyphens
    normalized = re.sub(r"-+", "-", normalized)
    # Trim leading/trailing hyphens (would be RFC-violating)
    normalized = _K8S_NAME_LEADING_TRAILING.sub("", normalized)

    if not normalized:
        # Pathological input (e.g. "////"). Fall back to a hash.
        normalized = hashlib.sha1(value.encode()).hexdigest()[:8]

    candidate = f"{prefix}{normalized}" if prefix else normalized

    # Ensure starts with alpha for prefix-less case (RFC requires alnum start;
    # we go stricter to be safe with all consumers).
    if not candidate[0].isalpha():
        candidate = "x" + candidate

    if len(candidate) <= max_len:
        return candidate

    # Too long: deterministic truncate + hash suffix
    suffix = "-" + hashlib.sha1(value.encode()).hexdigest()[:6]
    head_len = max_len - len(suffix)
    return candidate[:head_len].rstrip("-") + suffix


def mq_qmgr_name(app_id: str) -> str:
    """Generate an IBM MQ queue manager name from an app_id.

    Rules:
      - IBM MQ names are uppercase, [A-Z0-9._/], max 48 chars.
      - Slashes are valid in MQ names but problematic in K8s labels;
        we replace them with underscores for the MQ name.
      - Append `_QM` suffix to make the role obvious in MQSC outputs.

    Examples:
        mq_qmgr_name("APUMN/GC") -> "APUMN_GC_QM"
        mq_qmgr_name("JUUD/C9")  -> "JUUD_C9_QM"
    """
    if not app_id:
        raise ValueError("mq_qmgr_name: empty app_id")

    upper = app_id.upper()
    # Slash → underscore for safety, then strip anything still invalid
    safe = upper.replace("/", "_")
    safe = _MQ_NAME_INVALID.sub("_", safe)
    safe = re.sub(r"_+", "_", safe).strip("_")
    candidate = f"{safe}_QM"

    if len(candidate) <= _MQ_NAME_MAX:
        return candidate

    # Truncate + hash suffix while keeping _QM at end
    suffix = "_" + hashlib.sha1(app_id.encode()).hexdigest()[:6].upper() + "_QM"
    head_len = _MQ_NAME_MAX - len(suffix)
    return safe[:head_len].rstrip("_") + suffix


def deployment_name(app_id: str) -> str:
    """K8s Deployment name for an app's queue manager pod."""
    return k8s_safe(app_id, prefix="qm-")


def service_name(app_id: str) -> str:
    """K8s Service name. Matches deployment for clarity."""
    return deployment_name(app_id)


def pvc_name(app_id: str) -> str:
    """K8s PVC name for an app's queue manager persistent storage."""
    return k8s_safe(app_id, prefix="qm-data-")


def secret_name(app_id: str) -> str:
    """K8s Secret name for an app's queue manager passwords."""
    return k8s_safe(app_id, prefix="qm-secret-")


def app_label(app_id: str) -> str:
    """Value for the standard `app` label on every K8s resource for this QM."""
    return deployment_name(app_id)


__all__ = [
    "k8s_safe",
    "mq_qmgr_name",
    "deployment_name",
    "service_name",
    "pvc_name",
    "secret_name",
    "app_label",
]
