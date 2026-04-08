"""
Tests for Platt scaling calibration.
"""
import pytest
import json
from pathlib import Path

import app.services.calibration as calibration_module
from app.services.calibration import (
    apply_platt_calibration,
    set_platt_params,
    get_platt_params,
    clear_platt_params,
    fit_platt_calibration,
    load_platt_from_disk,
)


@pytest.fixture(autouse=True)
def reset_calibration():
    """Clear in-memory Platt params before each test."""
    clear_platt_params()
    yield
    clear_platt_params()


class TestApplyPlattCalibration:
    def test_without_params_returns_raw_divided_by_100(self):
        result = apply_platt_calibration(65)
        assert result == pytest.approx(0.65, abs=0.001)

    def test_without_params_zero_confidence(self):
        result = apply_platt_calibration(0)
        assert result == pytest.approx(0.0, abs=0.001)

    def test_without_params_full_confidence(self):
        result = apply_platt_calibration(100)
        assert result == pytest.approx(1.0, abs=0.001)

    def test_with_params_returns_sigmoid(self):
        set_platt_params(a=1.0, b=0.0)
        # logistic(1.0 * 0.65 + 0.0) = sigmoid(0.65) ≈ 0.657
        result = apply_platt_calibration(65)
        assert 0.0 < result < 1.0

    def test_with_params_output_clamped_to_valid_range(self):
        set_platt_params(a=10.0, b=5.0)
        result = apply_platt_calibration(100)
        assert 0.0 <= result <= 1.0

    def test_higher_confidence_gives_higher_probability(self):
        set_platt_params(a=2.0, b=0.0)
        low = apply_platt_calibration(40)
        high = apply_platt_calibration(80)
        assert high > low


class TestSetAndGetPlattParams:
    def test_set_params_persists_to_memory(self):
        set_platt_params(1.5, -0.3)
        a, b = get_platt_params()
        assert a == pytest.approx(1.5)
        assert b == pytest.approx(-0.3)

    def test_clear_params_resets_to_none(self):
        set_platt_params(1.5, -0.3)
        clear_platt_params()
        a, b = get_platt_params()
        assert a is None
        assert b is None

    def test_set_params_writes_json_file(self, tmp_path):
        # tmp_path is already patched via conftest autouse fixture
        set_platt_params(2.0, 0.5)
        from app.core.config import settings
        platt_file = settings.data_dir / "platt_calibration.json"
        assert platt_file.exists()
        data = json.loads(platt_file.read_text())
        assert data["a"] == pytest.approx(2.0)
        assert data["b"] == pytest.approx(0.5)


class TestLoadPlattFromDisk:
    def test_load_from_disk_restores_params(self, tmp_path):
        from app.core.config import settings
        # Write JSON to the patched data dir
        platt_file = settings.data_dir / "platt_calibration.json"
        platt_file.write_text(json.dumps({"a": 3.0, "b": -1.0}))

        load_platt_from_disk()
        a, b = get_platt_params()
        assert a == pytest.approx(3.0)
        assert b == pytest.approx(-1.0)

    def test_load_missing_file_does_nothing(self):
        load_platt_from_disk()
        a, b = get_platt_params()
        assert a is None
        assert b is None

    def test_load_invalid_json_does_nothing(self, tmp_path):
        from app.core.config import settings
        platt_file = settings.data_dir / "platt_calibration.json"
        platt_file.write_text("not json {{{")

        load_platt_from_disk()
        a, b = get_platt_params()
        assert a is None
        assert b is None


class TestFitPlattCalibration:
    def test_too_few_samples_returns_none(self):
        result = fit_platt_calibration([0.6, 0.7, 0.5], [1, 0, 1])
        assert result is None

    def test_enough_samples_returns_tuple(self):
        confs = [0.3, 0.4, 0.5, 0.5, 0.6, 0.6, 0.7, 0.7, 0.8, 0.9, 0.9, 0.95]
        outs = [0,   0,   0,   1,   0,   1,   1,   1,   1,   1,   0,   1  ]
        result = fit_platt_calibration(confs, outs)
        assert result is not None
        assert len(result) == 2
        a, b = result
        assert isinstance(a, float)
        assert isinstance(b, float)
