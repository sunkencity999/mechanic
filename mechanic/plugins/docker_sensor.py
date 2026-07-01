"""Docker sensor — container state via `docker ps`.

Deliberately shells out to the docker CLI rather than depending on the docker-py SDK
(one less dependency, and the CLI is universally present where docker is used). No-op
(returns is_available()==False) when docker isn't installed or not on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from mechanic.plugins.base import SensorError

_SENSOR_NAME = "docker"
_DOCKER_BIN = "docker"


class DockerSensor:
    name = _SENSOR_NAME

    def is_available(self) -> bool:
        return shutil.which(_DOCKER_BIN) is not None

    def sample(self) -> dict:
        if not self.is_available():
            raise SensorError("docker not available on this host")
        try:
            proc = subprocess.run(
                [
                    _DOCKER_BIN, "ps",
                    "--format", "{{json .}}",
                    "--all",  # include stopped, so we can see state transitions
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError as exc:
            raise SensorError(f"docker binary missing: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SensorError("docker ps timed out (daemon unresponsive?)") from exc
        except OSError as exc:  # noqa: BLE001
            raise SensorError(f"docker ps failed: {exc}") from exc

        if proc.returncode != 0:
            # Most common: daemon not running. Treat as 'no containers' rather than an
            # error so a stopped docker doesn't spam SensorError every cycle.
            stderr = (proc.stderr or "").strip()
            daemon_down_msgs = (
                "Cannot connect to the Docker daemon",
                "Is the docker daemon running",
            )
            if any(msg in stderr for msg in daemon_down_msgs):
                return self._empty("daemon_down")
            raise SensorError(f"docker ps exited {proc.returncode}: {stderr[:200]}")

        containers = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        names = [c.get("Names", "") for c in containers]
        states = [c.get("State", "") for c in containers]
        n_running = sum(1 for s in states if s == "running")
        return {
            "n_containers": len(containers),
            "n_running": n_running,
            "n_stopped": len(containers) - n_running,
            "container_names": names,
            "states": states,
        }

    @staticmethod
    def _empty(reason: str) -> dict:
        return {
            "n_containers": 0,
            "n_running": 0,
            "n_stopped": 0,
            "container_names": [],
            "states": [],
            "note": reason,
        }


sensor = DockerSensor()
