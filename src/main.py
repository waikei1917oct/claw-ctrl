#!/usr/bin/env python3
"""
claw-ctrl — entry point

Usage:
    python3 main.py tui        # Launch TUI
    python3 main.py daemon     # Run as background daemon (for systemctl)
    python3 main.py status     # Print current status as JSON
    python3 main.py service install|uninstall|start|stop|restart|status
"""
import argparse
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(prog="claw-ctrl")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("tui", help="Launch terminal UI")
    sub.add_parser("daemon", help="Run as background daemon")
    sub.add_parser("status", help="Print status as JSON")

    svc = sub.add_parser("service", help="Manage claw-ctrl systemctl service")
    svc.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])

    args = parser.parse_args()

    if args.command == "tui":
        from tui.app import run_tui
        run_tui()

    elif args.command == "daemon":
        # daemon mode: start background polling loop
        import time
        import signal
        from infrastructure.llm.hub import LlmFrameworkHub
        from infrastructure.openclaw.config_repo import OpenClawConfigRepo
        from infrastructure.openclaw.adapter import OpenClawAdapter

        running = True

        def _stop(sig, frame):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)

        from infrastructure.system.state_store import StateStore

        hub = LlmFrameworkHub()
        repo = OpenClawConfigRepo()
        adapter = OpenClawAdapter()
        store = StateStore()

        print("[claw-ctrl daemon] started", flush=True)
        while running:
            try:
                frameworks = hub.refresh_all()
                entity = repo.load()
                adapter.refresh(entity)
                store.save(
                    openclaw={
                        "online": entity.online,
                        "version": entity.version,
                        "gateway": entity.gateway.endpoint,
                        "pid": entity.pid,
                        "cpu_percent": entity.cpu_percent,
                        "mem_mb": entity.mem_mb,
                        "install_path": str(entity.install_path),
                        "config_path": entity.config_path,
                        "recent_error": entity.recent_error,
                    },
                    llm=[
                        {
                            "name": f.name,
                            "running": f.is_running,
                            "installed": f.is_installed,
                            "port": f.port,
                            "active_model": f.active_model,
                            "models": [m.name for m in f.available_models],
                        }
                        for f in frameworks
                    ],
                )
            except Exception as e:
                print(f"[claw-ctrl daemon] error: {e}", flush=True)
            time.sleep(30)
        print("[claw-ctrl daemon] stopped", flush=True)

    elif args.command == "status":
        import json
        from infrastructure.llm.hub import LlmFrameworkHub
        from infrastructure.openclaw.config_repo import OpenClawConfigRepo
        from infrastructure.openclaw.adapter import OpenClawAdapter

        hub = LlmFrameworkHub()
        repo = OpenClawConfigRepo()
        adapter = OpenClawAdapter()

        entity = repo.load()
        adapter.refresh(entity)
        frameworks = hub.refresh_all()

        out = {
            "openclaw": {
                "online": entity.online,
                "version": entity.version,
                "gateway": entity.gateway.endpoint,
                "pid": entity.pid,
            },
            "llm": [
                {
                    "name": f.name,
                    "running": f.is_running,
                    "installed": f.is_installed,
                    "port": f.port,
                }
                for f in frameworks
            ],
        }
        print(json.dumps(out, indent=2))

    elif args.command == "service":
        from infrastructure.system.service import ClawCtrlService

        svc_mgr = ClawCtrlService()
        action = args.action

        if action == "install":
            svc_mgr.install()
            print("claw-ctrl service installed and enabled.")
        elif action == "uninstall":
            svc_mgr.uninstall()
            print("claw-ctrl service removed.")
        elif action == "start":
            svc_mgr.start()
            print("claw-ctrl service started.")
        elif action == "stop":
            svc_mgr.stop()
            print("claw-ctrl service stopped.")
        elif action == "restart":
            svc_mgr.restart()
            print("claw-ctrl service restarted.")
        elif action == "status":
            import json
            print(json.dumps(svc_mgr.status(), indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
