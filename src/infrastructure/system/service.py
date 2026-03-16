from __future__ import annotations

import subprocess
from pathlib import Path

SERVICE_FILE_CONTENT = """\
[Unit]
Description=claw-ctrl — OpenClaw & LLM management daemon
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/projects/claw-ctrl
ExecStart=/usr/bin/python3 /root/projects/claw-ctrl/src/main.py daemon
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claw-ctrl

[Install]
WantedBy=multi-user.target
"""


class ClawCtrlService:
    """Manages claw-ctrl's own systemctl service."""

    SERVICE_NAME = "claw-ctrl"
    SERVICE_PATH = Path("/etc/systemd/system/claw-ctrl.service")
    EXEC_PATH = Path("/root/projects/claw-ctrl/src/main.py")

    def install(self) -> None:
        """Write the service file and enable the service."""
        self.SERVICE_PATH.write_text(SERVICE_FILE_CONTENT)
        subprocess.run(
            ["systemctl", "daemon-reload"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["systemctl", "enable", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )

    def uninstall(self) -> None:
        """Stop, disable, delete the service file, and reload daemon."""
        subprocess.run(
            ["systemctl", "stop", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["systemctl", "disable", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )
        if self.SERVICE_PATH.exists():
            self.SERVICE_PATH.unlink()
        subprocess.run(
            ["systemctl", "daemon-reload"],
            capture_output=True,
            check=False,
        )

    def start(self) -> None:
        subprocess.run(
            ["systemctl", "start", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )

    def stop(self) -> None:
        subprocess.run(
            ["systemctl", "stop", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )

    def restart(self) -> None:
        subprocess.run(
            ["systemctl", "restart", self.SERVICE_NAME],
            capture_output=True,
            check=False,
        )

    def status(self) -> dict:
        """Return a dict with keys: active, enabled, since, pid."""
        result = {
            "active": False,
            "enabled": False,
            "since": None,
            "pid": None,
        }
        try:
            # Check active state
            active_result = subprocess.run(
                ["systemctl", "is-active", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
            result["active"] = active_result.stdout.strip() == "active"

            # Check enabled state
            enabled_result = subprocess.run(
                ["systemctl", "is-enabled", self.SERVICE_NAME],
                capture_output=True,
                text=True,
                check=False,
            )
            result["enabled"] = enabled_result.stdout.strip() == "enabled"

            # Get detailed status for since/pid
            show_result = subprocess.run(
                [
                    "systemctl",
                    "show",
                    self.SERVICE_NAME,
                    "--property=ActiveEnterTimestamp,MainPID",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            for line in show_result.stdout.splitlines():
                if line.startswith("ActiveEnterTimestamp="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        result["since"] = val
                elif line.startswith("MainPID="):
                    val = line.split("=", 1)[1].strip()
                    try:
                        pid = int(val)
                        result["pid"] = pid if pid > 0 else None
                    except ValueError:
                        pass
        except Exception:
            pass
        return result

    def is_installed(self) -> bool:
        return self.SERVICE_PATH.exists()
