from __future__ import annotations

import socket
import subprocess
from datetime import datetime

from entities.openclaw import OpenClawEntity
from infrastructure.openclaw.config_repo import _detect_pid, get_resource_usage


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a TCP connection to host:port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class OpenClawAdapter:
    """Manages the lifecycle of a local openclaw instance."""

    def refresh(self, entity: OpenClawEntity) -> OpenClawEntity:
        """Update online/pid/cpu/mem fields on entity."""
        pid = _detect_pid()
        entity.pid = pid

        if pid is not None:
            cpu, mem = get_resource_usage(pid)
            entity.cpu_percent = cpu
            entity.mem_mb = mem

        # Check gateway health
        if entity.gateway.host and entity.gateway.port:
            entity.online = _check_port(entity.gateway.host, entity.gateway.port)
        elif pid is not None:
            entity.online = True
        else:
            entity.online = False

        return entity

    def start(self, entity: OpenClawEntity) -> OpenClawEntity:
        """Start the openclaw gateway."""
        try:
            subprocess.run(
                ["openclaw", "gateway", "start"],
                capture_output=True,
                text=True,
                check=False,
            )
            entity.last_restart_time = datetime.now().isoformat()
            entity.recent_error = None
        except FileNotFoundError:
            entity.recent_error = "openclaw binary not found"
        except Exception as e:
            entity.recent_error = str(e)
        return self.refresh(entity)

    def stop(self, entity: OpenClawEntity) -> OpenClawEntity:
        """Stop the openclaw gateway."""
        try:
            subprocess.run(
                ["openclaw", "gateway", "stop"],
                capture_output=True,
                text=True,
                check=False,
            )
            entity.recent_error = None
        except FileNotFoundError:
            entity.recent_error = "openclaw binary not found"
        except Exception as e:
            entity.recent_error = str(e)
        return self.refresh(entity)

    def restart(self, entity: OpenClawEntity) -> OpenClawEntity:
        """Restart the openclaw gateway."""
        try:
            subprocess.run(
                ["openclaw", "gateway", "restart"],
                capture_output=True,
                text=True,
                check=False,
            )
            entity.last_restart_time = datetime.now().isoformat()
            entity.recent_error = None
        except FileNotFoundError:
            entity.recent_error = "openclaw binary not found"
        except Exception as e:
            entity.recent_error = str(e)
        return self.refresh(entity)
