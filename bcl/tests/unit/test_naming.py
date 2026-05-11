"""Unit tests for bcl.provisioning.naming."""

from __future__ import annotations

import pytest

from bcl.provisioning import naming


class TestK8sSafe:
    def test_simple_input(self) -> None:
        assert naming.k8s_safe("APUMN/GC", prefix="qm-") == "qm-apumn-gc"

    def test_no_prefix(self) -> None:
        result = naming.k8s_safe("APUMN/GC")
        assert result.startswith("a")  # was uppercase → lowercase 'a'
        assert "-" in result

    def test_collapses_repeated_hyphens(self) -> None:
        assert naming.k8s_safe("a__b//c", prefix="qm-") == "qm-a-b-c"

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert naming.k8s_safe("/abc/", prefix="qm-") == "qm-abc"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            naming.k8s_safe("")

    def test_pathological_input_uses_hash(self) -> None:
        result = naming.k8s_safe("////", prefix="qm-")
        assert result.startswith("qm-")
        assert len(result) > len("qm-")  # has hash suffix

    def test_long_input_truncated_with_hash_suffix(self) -> None:
        long = "a" * 100
        result = naming.k8s_safe(long, prefix="qm-")
        assert len(result) <= 63
        # Hash suffix makes it deterministic
        result2 = naming.k8s_safe(long, prefix="qm-")
        assert result == result2

    def test_starts_with_alpha_for_numeric_input(self) -> None:
        # K8s requires DNS labels start with alphanumeric; we enforce alpha
        result = naming.k8s_safe("123abc")
        assert result[0].isalpha()


class TestMqQmgrName:
    def test_simple_app_id(self) -> None:
        assert naming.mq_qmgr_name("APUMN/GC") == "APUMN_GC_QM"

    def test_already_uppercase(self) -> None:
        assert naming.mq_qmgr_name("JUUD/C9") == "JUUD_C9_QM"

    def test_lowercase_input_upcased(self) -> None:
        assert naming.mq_qmgr_name("simpleapp") == "SIMPLEAPP_QM"

    def test_invalid_chars_replaced_with_underscore(self) -> None:
        assert naming.mq_qmgr_name("a!@#b") == "A_B_QM"

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError):
            naming.mq_qmgr_name("")

    def test_long_input_truncated_keeping_qm_suffix(self) -> None:
        long = "X" * 60
        result = naming.mq_qmgr_name(long)
        assert len(result) <= 48
        assert result.endswith("_QM")


class TestResourceNames:
    def test_deployment_name(self) -> None:
        assert naming.deployment_name("APUMN/GC") == "qm-apumn-gc"

    def test_service_name_matches_deployment(self) -> None:
        # Convention: Service has same name as Deployment for clarity
        app = "APUMN/GC"
        assert naming.service_name(app) == naming.deployment_name(app)

    def test_pvc_name_has_data_suffix(self) -> None:
        assert naming.pvc_name("APUMN/GC") == "qm-data-apumn-gc"

    def test_secret_name_has_secret_suffix(self) -> None:
        assert naming.secret_name("APUMN/GC") == "qm-secret-apumn-gc"

    def test_app_label_matches_deployment(self) -> None:
        # Label selector relies on this equality
        app = "APUMN/GC"
        assert naming.app_label(app) == naming.deployment_name(app)
