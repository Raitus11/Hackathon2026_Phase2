"""Unit tests for bcl.provisioning.render."""

from __future__ import annotations

import yaml

from bcl.provisioning.render import (
    QMRenderInputs,
    assert_yaml_valid,
    generate_password,
    render_for_qm,
)


def _inputs(app_id: str = "APUMN/GC") -> QMRenderInputs:
    return QMRenderInputs(
        app_id=app_id,
        qm_name="APUMN_GC_QM",
        topology_id=42,
        run_id="b2f3aef9-1234-5678-90ab-cdef12345678",
        lamport_clock=15,
        admin_password="admin-pw-test",
        app_password="app-pw-test",
    )


class TestGeneratePassword:
    def test_default_length(self) -> None:
        pw = generate_password()
        assert len(pw) == 32

    def test_custom_length(self) -> None:
        assert len(generate_password(16)) == 16

    def test_random(self) -> None:
        # Vanishingly small chance of collision
        assert generate_password() != generate_password()

    def test_alphanumeric_only(self) -> None:
        pw = generate_password(64)
        assert pw.isalnum()


class TestRenderForQm:
    def test_pvc_well_formed(self) -> None:
        m = render_for_qm(_inputs())
        pvc = assert_yaml_valid(m.pvc, "PersistentVolumeClaim")
        assert pvc["metadata"]["name"] == "qm-data-apumn-gc"
        assert pvc["spec"]["storageClassName"] == "sc-ontap-nas"
        assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"
        assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]

    def test_secret_base64_encoded(self) -> None:
        import base64
        m = render_for_qm(_inputs())
        sec = assert_yaml_valid(m.secret, "Secret")
        assert sec["type"] == "Opaque"
        assert set(sec["data"].keys()) == {"mqAdminPassword", "mqAppPassword"}
        # Decode and verify
        assert base64.b64decode(sec["data"]["mqAdminPassword"]).decode() == "admin-pw-test"
        assert base64.b64decode(sec["data"]["mqAppPassword"]).decode() == "app-pw-test"

    def test_deployment_uses_secret_file_envs(self) -> None:
        m = render_for_qm(_inputs())
        dep = assert_yaml_valid(m.deployment, "Deployment")
        container = dep["spec"]["template"]["spec"]["containers"][0]
        env_names = {e["name"] for e in container["env"]}
        # We use the _FILE variants, NOT plaintext _PASSWORD env vars
        assert "MQ_ADMIN_PASSWORD_FILE" in env_names
        assert "MQ_APP_PASSWORD_FILE" in env_names
        assert "MQ_ADMIN_PASSWORD" not in env_names
        assert "MQ_APP_PASSWORD" not in env_names

    def test_deployment_has_pvc_mount(self) -> None:
        m = render_for_qm(_inputs())
        dep = assert_yaml_valid(m.deployment, "Deployment")
        spec = dep["spec"]["template"]["spec"]
        vol_names = {v["name"] for v in spec["volumes"]}
        assert vol_names == {"mq-passwords", "mq-data"}
        # mq-data must be a PVC reference
        mq_data = next(v for v in spec["volumes"] if v["name"] == "mq-data")
        assert "persistentVolumeClaim" in mq_data
        assert mq_data["persistentVolumeClaim"]["claimName"] == "qm-data-apumn-gc"

    def test_deployment_has_required_probes(self) -> None:
        m = render_for_qm(_inputs())
        dep = assert_yaml_valid(m.deployment, "Deployment")
        container = dep["spec"]["template"]["spec"]["containers"][0]
        assert container["startupProbe"]["exec"]["command"] == ["chkmqstarted"]
        assert container["livenessProbe"]["exec"]["command"] == ["chkmqhealthy"]
        assert container["readinessProbe"]["exec"]["command"] == ["chkmqready"]

    def test_deployment_disables_istio(self) -> None:
        m = render_for_qm(_inputs())
        dep = assert_yaml_valid(m.deployment, "Deployment")
        ann = dep["spec"]["template"]["metadata"]["annotations"]
        assert ann["sidecar.istio.io/inject"] == "false"

    def test_service_clusterip_with_named_ports(self) -> None:
        m = render_for_qm(_inputs())
        svc = assert_yaml_valid(m.service, "Service")
        assert svc["spec"]["type"] == "ClusterIP"
        port_names = {p["name"] for p in svc["spec"]["ports"]}
        assert port_names == {"mq-listener", "mqweb-https"}

    def test_all_resources_share_labels(self) -> None:
        m = render_for_qm(_inputs())
        for kind, yaml_text in m.in_apply_order():
            parsed = yaml.safe_load(yaml_text)
            labels = parsed["metadata"]["labels"]
            assert labels["intelliai2doto/owner"] == "bcl"
            assert labels["intelliai2doto/managed-by"] == "provisioning-engine"
            assert labels["intelliai2doto/topology-id"] == "42"
            assert labels["intelliai2doto/qm-name"] == "APUMN_GC_QM"
            assert labels["app"] == "qm-apumn-gc"

    def test_no_password_leaked_into_non_secret_yaml(self) -> None:
        m = render_for_qm(_inputs())
        # Secret has the (base64-encoded) values; nothing else should
        for kind, yaml_text in m.in_apply_order():
            if kind == "Secret":
                continue
            assert "admin-pw-test" not in yaml_text, f"plaintext leak in {kind}"
            assert "app-pw-test" not in yaml_text, f"plaintext leak in {kind}"

    def test_render_is_deterministic(self) -> None:
        inp = _inputs()
        m1 = render_for_qm(inp)
        m2 = render_for_qm(inp)
        for (k1, y1), (k2, y2) in zip(m1.in_apply_order(), m2.in_apply_order()):
            assert k1 == k2
            assert y1 == y2

    def test_apply_order(self) -> None:
        m = render_for_qm(_inputs())
        order = [k for k, _ in m.in_apply_order()]
        assert order == ["PersistentVolumeClaim", "Secret", "Deployment", "Service"]


class TestK8sNameLengthLimits:
    """K8s rejects names that violate RFC-1123 length limits at apply time.

    We catch them at render time instead by ensuring the renderer never
    produces a manifest with an over-long name.
    """

    def test_long_app_id_truncated(self) -> None:
        m = render_for_qm(_inputs(app_id="x" * 100))
        for _, yaml_text in m.in_apply_order():
            parsed = yaml.safe_load(yaml_text)
            assert len(parsed["metadata"]["name"]) <= 63
