from __future__ import annotations

import re
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Callable

from entities.llm import LlmFrameworkEntity, ModelEntity

_DEFAULT_GGUF_DIR = Path("/root/models/gguf")

_LLAMA_SERVER_NAMES = ["llama-server", "llama.cpp/server", "llama-cpp-server"]

_SERVICE_FILE = Path("/etc/systemd/system/llama-cpp.service")


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class LlamaCppAdapter:
    """Adapter for managing the llama.cpp LLM framework."""

    def is_installed(self) -> bool:
        for name in _LLAMA_SERVER_NAMES:
            if shutil.which(name) is not None:
                return True
        common_paths = [
            Path("/usr/local/bin/llama-server"),
            Path("/usr/bin/llama-server"),
            Path.home() / "llama.cpp" / "server",
            Path.home() / "llama.cpp" / "llama-server",
            Path("/opt/llama.cpp/server"),
        ]
        return any(p.exists() for p in common_paths)

    def _find_binary(self) -> str | None:
        for name in _LLAMA_SERVER_NAMES:
            found = shutil.which(name)
            if found:
                return found
        common_paths = [
            Path("/usr/local/bin/llama-server"),
            Path("/usr/bin/llama-server"),
            Path.home() / "llama.cpp" / "server",
            Path.home() / "llama.cpp" / "llama-server",
            Path("/opt/llama.cpp/server"),
        ]
        for p in common_paths:
            if p.exists():
                return str(p)
        return None

    def is_running(self, port: int) -> bool:
        return _check_port("127.0.0.1", port)

    def get_running_port(self) -> int | None:
        """Detect the port of the currently running llama-server from /proc cmdline."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "llama-server"],
                capture_output=True, text=True, check=False,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if not pid_str:
                    continue
                try:
                    raw = Path(f"/proc/{pid_str}/cmdline").read_bytes()
                    parts = [p.decode(errors="replace") for p in raw.split(b"\x00") if p]
                    for i, part in enumerate(parts):
                        if part in ("--port", "-p") and i + 1 < len(parts):
                            return int(parts[i + 1])
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def get_effective_model_dir(self, entity: LlmFrameworkEntity) -> Path:
        """Return the effective model scan directory."""
        if entity.model_dir:
            return Path(entity.model_dir)
        return _DEFAULT_GGUF_DIR

    def update_service(
        self,
        model_path: str,
        profile: "ModelRunProfile",  # type: ignore[name-defined]  # noqa: F821
        extra_args: list[str],
        port: int,
        host: str = "127.0.0.1",
    ) -> None:
        """
        Rewrite ExecStart in llama-cpp.service with the given model + profile params,
        then daemon-reload and restart the service.
        Preserves existing -t / -b / -ub values from the current service file.
        """
        if not _SERVICE_FILE.exists():
            raise FileNotFoundError(f"Service file not found: {_SERVICE_FILE}")

        content = _SERVICE_FILE.read_text()

        # Preserve threading/batch params from existing ExecStart
        t_val, b_val, ub_val = "12", "1024", "512"
        for line in content.splitlines():
            if line.strip().startswith("ExecStart="):
                parts = line.strip()[len("ExecStart="):].split()
                for i, p in enumerate(parts):
                    if p == "-t" and i + 1 < len(parts):
                        t_val = parts[i + 1]
                    elif p == "-b" and i + 1 < len(parts):
                        b_val = parts[i + 1]
                    elif p in ("-ub", "--ubatch-size") and i + 1 < len(parts):
                        ub_val = parts[i + 1]

        binary = self._find_binary() or "llama-server"
        model_alias = Path(model_path).name  # basename, e.g. "Qwen3.5-35B...gguf"
        cmd_parts = [
            binary,
            "-m", model_path,
            "-a", model_alias,          # alias = basename, so API model field matches
            "--host", host,
            "--port", str(port),
            "-c", str(profile.context_size),
            "-ngl", str(profile.gpu_layers),
            "-t", t_val,
            "-b", b_val,
            "-ub", ub_val,
        ] + extra_args

        new_exec = "ExecStart=" + " ".join(cmd_parts)
        new_lines = [
            new_exec if ln.strip().startswith("ExecStart=") else ln
            for ln in content.splitlines()
        ]
        tmp = _SERVICE_FILE.with_suffix(".service.tmp")
        tmp.write_text("\n".join(new_lines) + "\n")
        tmp.replace(_SERVICE_FILE)

        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)
        subprocess.run(["systemctl", "restart", "llama-cpp.service"], capture_output=True, check=False)

    def start(self, entity: LlmFrameworkEntity) -> None:
        """Start llama-server via systemctl if service file exists, else skip.
        Never spawns a bare Popen — systemd is the single source of truth."""
        if _SERVICE_FILE.exists():
            subprocess.run(["systemctl", "start", "llama-cpp.service"],
                           capture_output=True, check=False)
        # Kill any orphan processes not managed by systemd
        self._kill_orphans()

    def stop(self) -> None:
        """Stop llama-server: systemctl stop (prevents Restart=always re-spawn) + kill orphans."""
        if _SERVICE_FILE.exists():
            subprocess.run(["systemctl", "stop", "llama-cpp.service"],
                           capture_output=True, check=False)
        self._kill_orphans()

    def _kill_orphans(self) -> None:
        """Kill any llama-server processes not owned by systemd (orphans from Popen)."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "llama-server"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                return
            service_pid_str = subprocess.run(
                ["systemctl", "show", "llama-cpp.service", "--property=MainPID", "--value"],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            service_pid = int(service_pid_str) if service_pid_str.isdigit() else 0
            for pid_str in result.stdout.strip().splitlines():
                pid = int(pid_str)
                if pid != service_pid:
                    subprocess.run(["kill", str(pid)], capture_output=True, check=False)
        except Exception:
            pass

    def list_models(self, model_dir: Path | None = None) -> list[ModelEntity]:
        """Scan model directory for .gguf files."""
        scan_dir = model_dir if model_dir is not None else _DEFAULT_GGUF_DIR
        models: list[ModelEntity] = []
        if not scan_dir.exists():
            return models
        try:
            for gguf_file in sorted(scan_dir.glob("**/*.gguf")):
                size_bytes: int | None = None
                try:
                    size_bytes = gguf_file.stat().st_size
                except Exception:
                    pass
                models.append(
                    ModelEntity(
                        name=gguf_file.name,
                        size_bytes=size_bytes,
                        framework="llama.cpp",
                        full_path=str(gguf_file),
                    )
                )
        except Exception:
            pass
        return models

    def delete_model(self, name: str, model_dir: Path | None = None) -> None:
        """Delete a .gguf model file from the model directory."""
        scan_dir = model_dir if model_dir is not None else _DEFAULT_GGUF_DIR
        for gguf_file in scan_dir.glob("**/*.gguf"):
            if gguf_file.name == name:
                gguf_file.unlink()
                return

    def move_models(
        self,
        entity: LlmFrameworkEntity,
        new_dir: str,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        """
        Move all .gguf files from current model dir to new_dir:
        1. Copy each file to new location.
        2. Delete originals only after all copies succeed.
        Raises on copy error so old files are never deleted prematurely.
        """
        new_path = Path(new_dir)
        new_path.mkdir(parents=True, exist_ok=True)

        current_dir = self.get_effective_model_dir(entity)

        if current_dir == new_path:
            entity.model_dir = str(new_path)
            return

        files_to_move: list[Path] = []
        if current_dir.exists():
            files_to_move = sorted(current_dir.glob("**/*.gguf"))

        total = len(files_to_move)
        successfully_copied: list[Path] = []

        for i, gguf_file in enumerate(files_to_move):
            dest = new_path / gguf_file.name
            if progress_cb:
                progress_cb(f"Copying {i + 1}/{total}: {gguf_file.name}")
            shutil.copy2(str(gguf_file), str(dest))  # raises on error
            successfully_copied.append(gguf_file)

        # All copies succeeded — now delete originals
        for gguf_file in successfully_copied:
            if progress_cb:
                progress_cb(f"Removing old: {gguf_file.name}")
            try:
                gguf_file.unlink()
            except Exception:
                pass

        entity.model_dir = str(new_path)

    def get_active_model_name(self) -> str | None:
        """Return the model filename currently loaded by the running llama-server."""
        cmd = get_running_llamaserver_cmd()
        if not cmd:
            return None
        for i, part in enumerate(cmd):
            if part == "-m" and i + 1 < len(cmd):
                return Path(cmd[i + 1]).name
        return None

    def refresh(self, entity: LlmFrameworkEntity) -> LlmFrameworkEntity:
        """Update entity with current state, auto-detecting port from running process."""
        entity.is_installed = self.is_installed()
        # Always probe the actual running port — overrides any stale default
        running_port = self.get_running_port()
        if running_port:
            entity.port = running_port
        entity.is_running = self.is_running(entity.port)
        entity.active_model = self.get_active_model_name() if entity.is_running else None
        model_dir = self.get_effective_model_dir(entity)
        entity.available_models = self.list_models(model_dir)
        return entity


# ---------------------------------------------------------------------------
# VRAM / profile test helpers (module-level, no adapter state needed)
# ---------------------------------------------------------------------------

# Fatal: model could not be loaded at all
_OOM_FATAL_PATTERNS = [
    "failed to initialize the context",
    "common_init_result: failed",
    "common_init_from_params: failed",
    "graph_reserve: failed",
]

# Warning: cudaMalloc errors that appear but model continues (layers fall to CPU)
_OOM_WARN_PATTERNS = [
    "cudamalloc failed: out of memory",
    "failed to allocate cuda",
    "ggml_gallocr_reserve_n_impl: failed",
]

# Keep combined for legacy callers
_OOM_PATTERNS = _OOM_FATAL_PATTERNS + _OOM_WARN_PATTERNS


def check_vram() -> dict:
    """
    Query nvidia-smi for current VRAM usage.
    Returns dict with keys used_mb, total_mb, free_mb, or {} if unavailable.
    """
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}
        parts = result.stdout.strip().splitlines()[0].split(",")
        return {
            "used_mb": int(parts[0].strip()),
            "total_mb": int(parts[1].strip()),
            "free_mb": int(parts[2].strip()),
        }
    except Exception:
        return {}


def _parse_perf(output: str) -> tuple[float | None, float | None]:
    """
    Extract (prompt_tps, generation_tps) from llama-cli output.
    Handles two formats:
      1. Bracket summary:  [ Prompt: 203.6 t/s | Generation: 70.8 t/s ]
      2. Verbose perf log: llama_perf_context_print:  prompt eval time = ... (xx.xx tokens per second)
    Returns None for each value if not found.
    """
    prompt_tps: float | None = None
    eval_tps: float | None = None

    # Format 1 — bracket summary line (takes priority, more accurate)
    m = re.search(
        r"\[\s*Prompt:\s*([\d.]+)\s*t/s\s*\|\s*Generation:\s*([\d.]+)\s*t/s",
        output, re.IGNORECASE,
    )
    if m:
        return float(m.group(1)), float(m.group(2))

    # Format 2 — llama_perf_context_print verbose lines
    for line in output.splitlines():
        ll = line.lower()
        m2 = re.search(r"([\d.]+)\s*tokens per second", line, re.IGNORECASE)
        if not m2:
            continue
        if "prompt eval time" in ll:
            prompt_tps = float(m2.group(1))
        elif "eval time" in ll:
            eval_tps = float(m2.group(1))

    return prompt_tps, eval_tps


def get_running_llamaserver_cmd() -> list[str] | None:
    """
    Return the full argv of the currently running llama-server process,
    or None if no llama-server is detected.
    This lets us restart with identical flags (port, model, etc.) after a test.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "llama-server"],
            capture_output=True, text=True, check=False,
        )
        for pid_str in result.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if not pid_str:
                continue
            try:
                raw = Path(f"/proc/{pid_str}/cmdline").read_bytes()
                parts = [p.decode(errors="replace") for p in raw.split(b"\x00") if p]
                if parts:
                    return parts
            except Exception:
                continue
    except Exception:
        pass
    return None


def _free_vram_before_test(progress_cb: Callable[[str], None] | None = None) -> None:
    """Kill any running llama-cli / llama-server processes to free VRAM before test."""
    targets = ["llama-cli", "llama-server", "llama.cpp/server", "llama-cpp-server"]
    killed = False
    for name in targets:
        result = subprocess.run(
            ["pkill", "-f", name], capture_output=True, check=False
        )
        if result.returncode == 0:
            killed = True
    if killed:
        if progress_cb:
            progress_cb("Killed existing llama processes — waiting 3s for VRAM to free ...")
        import time as _t
        _t.sleep(3)


def test_profile_load(
    model_path: str,
    profile: "ModelRunProfile",  # type: ignore[name-defined]  # noqa: F821
    progress_cb: Callable[[str], None] | None = None,
    on_proc: Callable | None = None,
    timeout: int = 180,
) -> tuple[bool, str]:
    """
    Load the model with the profile's settings and a short prompt, then detect:
      - CLEAN: no OOM messages at all  (ideal)
      - WARN:  cudaMalloc warnings but model loaded  (some layers fell to CPU)
      - FAIL:  model could not initialize at all
    Streams llama-cli stderr in real time so the user can watch progress.
    Returns (success, message); success=False only on hard FAIL.
    """
    import threading as _th
    import time as _time

    # 1. Kill existing llama processes and free VRAM
    _free_vram_before_test(progress_cb)

    # 2. Check VRAM state after freeing
    vram = check_vram()
    if vram:
        used, total, free = vram["used_mb"], vram["total_mb"], vram["free_mb"]
        pct = round(used / total * 100) if total else 0
        if progress_cb:
            progress_cb(
                f"VRAM: {used} MB / {total} MB used ({pct}%)  —  {free} MB free"
            )
        if pct >= 50 and progress_cb:
            progress_cb(
                f"WARNING: VRAM still {pct}% occupied — results may be affected"
            )
    else:
        if progress_cb:
            progress_cb("nvidia-smi unavailable — cannot verify VRAM state")

    # 3. Find llama-cli binary
    llama_cli = (
        shutil.which("llama-cli")
        or shutil.which("llama-cli-cuda")
        or shutil.which("main")
    )
    if not llama_cli:
        return False, "llama-cli not found in PATH"

    # Cap at 256 so thinking models (e.g. GLM) can finish their <think> phase
    # and produce the stats line; small enough to keep the test fast.
    test_n = min(profile.max_tokens, 256)

    cmd = [
        llama_cli,
        "-m", model_path,
        "-c", str(profile.context_size),
        "-ngl", str(profile.gpu_layers),
        "--temp", str(profile.temp),
        "--top-p", str(profile.top_p),
        "--repeat-penalty", str(profile.repeat_penalty),
        "-n", str(test_n),
        "-p", "Hi",
    ]

    if progress_cb:
        progress_cb(
            f"Loading  ctx={profile.context_size}  ngl={profile.gpu_layers}  "
            f"n={test_n} ..."
        )

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except Exception as exc:
        return False, f"Failed to start llama-cli: {exc}"

    if on_proc is not None:
        on_proc(proc)

    # Write /exit to the stdin pipe immediately.
    # The conversation loop reads it after generating the -p response and exits.
    # Writing before the model loads is fine — it sits in the OS pipe buffer.
    try:
        proc.stdin.write("/exit\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception:
        pass

    # Collect output for analysis; stream stderr live
    stderr_lines: list[str] = []
    stdout_lines: list[str] = []
    _last_line_ts: list[float] = [_time.monotonic()]

    def _read_stderr() -> None:
        for raw in proc.stderr:
            try:
                line = raw.rstrip()
                if line:
                    stderr_lines.append(line)
                    _last_line_ts[0] = _time.monotonic()
                    if progress_cb:
                        display = line if len(line) <= 110 else line[:107] + "..."
                        try:
                            progress_cb(display)
                        except Exception:
                            pass
            except Exception:
                pass

    def _read_stdout() -> None:
        # Collect all stdout silently — model thinking/response text is not
        # useful progress and calling progress_cb for every line slows the
        # thread enough that the 5s join timeout expires before the stats line
        # (which arrives at the very end) is appended.
        # Only surface the stats summary line so the user sees it live.
        for raw in proc.stdout:
            try:
                line = raw.rstrip()
                if line:
                    stdout_lines.append(line)
                    _last_line_ts[0] = _time.monotonic()
                    if progress_cb and re.search(r"\[\s*Prompt:", line):
                        try:
                            progress_cb(line)
                        except Exception:
                            pass
            except Exception:
                pass

    def _ticker() -> None:
        start = _time.monotonic()
        while proc.poll() is None:
            _time.sleep(1)
            if proc.poll() is not None:
                break
            elapsed = _time.monotonic() - start
            if _time.monotonic() - _last_line_ts[0] > 3.0 and progress_cb:
                progress_cb(f"Loading ... {elapsed:.0f}s  (k = stop)")

    t_err = _th.Thread(target=_read_stderr, daemon=True)
    t_out = _th.Thread(target=_read_stdout, daemon=True)
    t_tick = _th.Thread(target=_ticker, daemon=True)
    t_err.start()
    t_out.start()
    t_tick.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        t_err.join(timeout=3)
        t_out.join(timeout=3)
        return True, (
            f"Timed out after {timeout}s — model may still be loading. "
            f"ctx={profile.context_size}  ngl={profile.gpu_layers}"
        )

    # Process has exited — pipe write-end is closed, readers finish on EOF.
    # No timeout needed; they complete in milliseconds.
    t_err.join()
    t_out.join()

    combined_raw = "\n".join(stderr_lines + stdout_lines)
    combined_low = combined_raw.lower()

    # Check for hard failure (model could not initialize)
    fatal_lines = [
        l for l in combined_raw.splitlines()
        if any(p in l.lower() for p in _OOM_FATAL_PATTERNS)
    ]
    if fatal_lines:
        return False, (
            "FAIL — model could not load (OOM):\n" + "\n".join(fatal_lines[:6])
        )

    if proc.returncode not in (0, None):
        tail = combined_raw.strip().splitlines()[-5:]
        return False, f"Process exited {proc.returncode}:\n" + "\n".join(tail)

    # Check for OOM warnings (model loaded but with VRAM pressure)
    warn_lines = [
        l for l in combined_raw.splitlines()
        if any(p in l.lower() for p in _OOM_WARN_PATTERNS)
    ]

    # Parse t/s
    prompt_tps, eval_tps = _parse_perf(combined_raw)
    perf_parts: list[str] = []
    if prompt_tps is not None:
        perf_parts.append(f"prefill {prompt_tps:.1f} t/s")
    if eval_tps is not None:
        perf_parts.append(f"generation {eval_tps:.1f} t/s")
    perf_str = "  |  " + " / ".join(perf_parts) if perf_parts else ""

    if warn_lines:
        return True, (
            f"WARN — loaded with OOM warnings (some layers may have fallen to CPU):\n"
            + "\n".join(warn_lines[:4])
            + f"\nctx={profile.context_size}  ngl={profile.gpu_layers}{perf_str}"
        )

    return True, f"CLEAN — no OOM  ctx={profile.context_size}  ngl={profile.gpu_layers}{perf_str}"
