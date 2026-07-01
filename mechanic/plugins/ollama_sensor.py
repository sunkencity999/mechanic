"""Ollama sensor — which models are currently loaded, via the local API.

Probes the running Ollama daemon at 127.0.0.1:11434/api/ps. is_available() checks for
the ollama binary on PATH (cheap); sample() hits the API. A stopped daemon is a normal
state, not an error — sample() returns 0 loaded models in that case.
"""

from __future__ import annotations

import json
import shutil
import urllib.request

from mechanic.plugins.base import SensorError

_SENSOR_NAME = "ollama"
_OLLAMA_BIN = "ollama"
_OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
_TIMEOUT = 5


class OllamaSensor:
    name = _SENSOR_NAME

    def is_available(self) -> bool:
        return shutil.which(_OLLAMA_BIN) is not None

    def sample(self) -> dict:
        if not self.is_available():
            raise SensorError("ollama not available on this host")
        try:
            req = urllib.request.Request(_OLLAMA_PS_URL, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                body = resp.read()
        except ConnectionRefusedError:
            # Daemon not running — normal, report zero loaded models.
            return self._empty("daemon_down")
        except Exception as exc:  # noqa: BLE001 - timeout, HTTP error, etc.
            raise SensorError(f"ollama api unreachable: {exc}") from exc

        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SensorError(f"ollama api returned non-JSON: {exc}") from exc

        models = data.get("models", []) or []
        names = [m.get("name", "") for m in models]
        sizes = [int(m.get("size", 0) or 0) for m in models]
        return {
            "n_models_loaded": len(models),
            "loaded_models": names,
            "loaded_vram_gb": round(sum(sizes) / 1024**3, 3),
            "daemon_up": True,
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "n_models_loaded": 0,
            "loaded_models": [],
            "loaded_vram_gb": 0.0,
            "daemon_up": False,
            "note": reason,
        }


sensor = OllamaSensor()
