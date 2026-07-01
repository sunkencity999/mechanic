"""Tests for the sensor plugin protocol and the three v1 sensors.

Every sensor must: expose name/is_available()/sample(), return a flat JSON-serializable
dict, and gracefully no-op (is_available()==False) when its backend is missing. Tests
fake the backends (psutil, subprocess, urllib) so they run anywhere without docker or
ollama installed.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from mechanic.plugins import base
from mechanic.plugins.docker_sensor import DockerSensor
from mechanic.plugins.ollama_sensor import OllamaSensor
from mechanic.plugins.os_sensor import OsSensor

# ---- protocol / registry ----------------------------------------------------


def test_all_sensors_satisfy_protocol():
    for sensor_cls in [OsSensor, DockerSensor, OllamaSensor]:
        inst = sensor_cls()
        assert hasattr(inst, "name")
        assert callable(inst.is_available)
        assert callable(inst.sample)


def test_sensor_names_are_stable_strings():
    assert OsSensor().name == "os"
    assert DockerSensor().name == "docker"
    assert OllamaSensor().name == "ollama"


def test_registry_discovers_all_three():
    from mechanic.plugins import registry as reg

    names = {s.name for s in reg.all()}
    assert {"os", "docker", "ollama"} <= names


# ---- os sensor --------------------------------------------------------------


def test_os_sensor_is_available_when_psutil_present():
    # psutil is a hard dep, so it's always available
    assert OsSensor().is_available() is True


def test_os_sensor_sample_shape_and_json_serializable():
    s = OsSensor()
    sample = s.sample()
    assert isinstance(sample, dict)
    # must be JSON serializable (the store persists as json.dumps)
    json.dumps(sample)
    # documented keys
    assert "cpu_pct" in sample
    assert "mem_pct" in sample
    assert "load_avg_1m" in sample
    assert "n_procs" in sample
    # numeric values are floats/ints, not None
    assert isinstance(sample["cpu_pct"], (int, float))
    assert isinstance(sample["mem_pct"], (int, float))


def test_os_sensor_sample_is_flat_dict():
    """Top-level keys only — no nested dicts that would complicate baselining."""
    s = OsSensor()
    sample = s.sample()
    for k, v in sample.items():
        assert not isinstance(v, (dict, list)), f"{k} is nested; sensors must be flat"


def test_os_sensor_handles_psutil_failure():
    """If psutil raises, sample() should not crash the sampler — it returns {} or raises
    a SensorError the sampler catches. Either way, no uncaught exception escapes."""
    s = OsSensor()
    with patch("psutil.cpu_percent", side_effect=RuntimeError("boom")):
        # We allow either: return {} gracefully, or raise a typed SensorError.
        # The sampler isolates per-sensor errors, so raising is acceptable here.
        try:
            result = s.sample()
            assert result == {} or isinstance(result, dict)
        except base.SensorError:
            pass  # acceptable — sampler handles it


# ---- docker sensor ----------------------------------------------------------


def test_docker_sensor_unavailable_when_docker_missing():
    s = DockerSensor()
    with patch("mechanic.plugins.docker_sensor.shutil.which", return_value=None):
        assert s.is_available() is False


def test_docker_sensor_available_when_docker_present():
    s = DockerSensor()
    with patch("mechanic.plugins.docker_sensor.shutil.which", return_value="/usr/local/bin/docker"):
        # also needs a working `docker ps` — but is_available checks the binary only
        assert s.is_available() is True


def _docker_present():
    """Patches for 'docker binary present' + a mockable subprocess.run."""
    which_patch = patch(
        "mechanic.plugins.docker_sensor.shutil.which",
        return_value="/usr/local/bin/docker",
    )
    run_patch = patch("mechanic.plugins.docker_sensor.subprocess.run")
    return which_patch, run_patch


def test_docker_sensor_unavailable_when_docker_daemon_down():
    """is_available returning True (binary present) but sample() failing daemon-down
    must raise SensorError, not a raw exception."""
    s = DockerSensor()
    which_patch, run_patch = _docker_present()
    with which_patch, run_patch as mock_run:
        mock_run.side_effect = FileNotFoundError("docker not runnable")
        with pytest.raises(base.SensorError):
            s.sample()


def test_docker_sensor_sample_shape():
    s = DockerSensor()
    fake_output = (
        '{"Names":"web","State":"running","Ports":"0.0.0.0:80->80/tcp"}\n'
        '{"Names":"db","State":"running","Ports":""}\n'
    )
    which_patch, run_patch = _docker_present()
    with which_patch, run_patch as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output, stderr="")
        sample = s.sample()
    assert sample["n_containers"] == 2
    assert sample["n_running"] == 2
    assert set(sample["container_names"]) == {"web", "db"}
    json.dumps(sample)  # serializable


def test_docker_sensor_empty_daemon():
    s = DockerSensor()
    which_patch, run_patch = _docker_present()
    with which_patch, run_patch as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        sample = s.sample()
    assert sample["n_containers"] == 0
    assert sample["container_names"] == []


def test_docker_sensor_sample_raises_when_not_available():
    s = DockerSensor()
    with patch("mechanic.plugins.docker_sensor.shutil.which", return_value=None):
        with pytest.raises(base.SensorError):
            s.sample()


# ---- ollama sensor ----------------------------------------------------------


def test_ollama_sensor_unavailable_when_ollama_missing():
    s = OllamaSensor()
    with patch("mechanic.plugins.ollama_sensor.shutil.which", return_value=None):
        assert s.is_available() is False


def test_ollama_sensor_available_when_ollama_present():
    s = OllamaSensor()
    with patch("mechanic.plugins.ollama_sensor.shutil.which", return_value="/usr/local/bin/ollama"):
        assert s.is_available() is True


def _ollama_present(**urlopen_overrides):
    """Context manager pair: make the ollama binary 'present' and stub the API call."""
    which_patch = patch(
        "mechanic.plugins.ollama_sensor.shutil.which",
        return_value="/usr/local/bin/ollama",
    )
    urlopen_patch = patch(
        "mechanic.plugins.ollama_sensor.urllib.request.urlopen",
        **urlopen_overrides,
    )
    return which_patch, urlopen_patch


def test_ollama_sensor_sample_shape():
    s = OllamaSensor()
    fake_api = {
        "models": [
            {"name": "qwen3:32b", "size": 19000000000, "digest": "abc"},
            {"name": "llama3.2:3b", "size": 2000000000, "digest": "def"},
        ]
    }
    which_patch, urlopen_patch = _ollama_present()
    with which_patch, urlopen_patch as mock_open:
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(fake_api).encode()
        cm.__exit__.return_value = False
        mock_open.return_value = cm
        sample = s.sample()
    assert sample["n_models_loaded"] == 2
    assert set(sample["loaded_models"]) == {"qwen3:32b", "llama3.2:3b"}
    json.dumps(sample)


def test_ollama_sensor_daemon_down_returns_none_models():
    """If ollama is installed but the daemon isn't running, sample() reflects 0 models
    rather than crashing — a stopped daemon is a normal state, not an error."""
    s = OllamaSensor()
    which_patch, urlopen_patch = _ollama_present(side_effect=ConnectionRefusedError)
    with which_patch, urlopen_patch:
        sample = s.sample()
    assert sample["n_models_loaded"] == 0
    assert sample["loaded_models"] == []


def test_ollama_sensor_sample_raises_when_not_available():
    s = OllamaSensor()
    with patch("mechanic.plugins.ollama_sensor.shutil.which", return_value=None):
        with pytest.raises(base.SensorError):
            s.sample()
