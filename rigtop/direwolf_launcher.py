"""Launch and manage a Direwolf subprocess."""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger


def _default_install_path() -> str:
    return "c:\\direwolf" if sys.platform == "win32" else ""


class DirewolfLauncher:
    """Spawn ``direwolf`` and keep a handle so we can tear it down later."""

    def __init__(
        self,
        *,
        install_path: str = _default_install_path(),
        config_dir: str | Path | None = None,
        source_configs: dict[str, Path] | None = None,
        stderr_callback: Callable[[str], None] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.install_path = Path(install_path)
        # Directory where rigtop writes derived active configs (defaults to install_path).
        self._config_dir = Path(config_dir) if config_dir else self.install_path
        # profile → user-provided source config path (e.g. "aprs" → Path("c:/dw/dw-aprs.conf"))
        self.source_configs: dict[str, Path] = source_configs or {}
        self.stderr_callback = stderr_callback
        self.extra_args = extra_args or []
        self._proc: subprocess.Popen[bytes] | None = None
        self._pty_proc = None  # winpty PtyProcess, Windows only
        self._stderr_thread: threading.Thread | None = None
        self._active_config: Path | None = None  # derived config written by generate_active_config
        self._active_profile: str | None = None  # e.g. "aprs" or "bbs"

    # ------------------------------------------------------------------

    def _find_exe(self) -> str:
        """Locate the direwolf binary: install_path first, then PATH."""
        for name in ("direwolf.exe", "direwolf"):
            candidate = self.install_path / name
            if candidate.is_file():
                return str(candidate)
        found = shutil.which("direwolf")
        if found:
            return found
        raise FileNotFoundError(
            f"direwolf not found in {self.install_path} or on PATH. "
            f"Check [direwolf] install_path in rigtop.toml."
        )

    def _build_command(self) -> list[str]:
        exe = self._find_exe()
        if self._active_config is not None:
            conf = self._active_config
        else:
            raise RuntimeError(
                "No config set — call switch_config() or use :aprs on / :bbs on first."
            )
        if not conf.is_file():
            raise FileNotFoundError(f"Config file not found: {conf}.")
        cmd = [exe, "-t", "0", "-c", str(conf)]
        cmd.extend(self.extra_args)
        return cmd

    # ------------------------------------------------------------------

    def generate_active_config(self, profile: str, beacon_enabled: bool = True) -> Path:
        """Derive an active config from the source, writing it to *config_dir*.

        When *beacon_enabled* is False, TBEACON lines are commented out so
        Direwolf does not send RF position beacons.  The user's source file is
        never modified.  Returns the path of the written active config.
        """
        src = self.source_configs.get(profile)
        if src is None:
            src = self.install_path / f"direwolf-{profile}.conf"
        if not src.is_file():
            raise FileNotFoundError(
                f"Direwolf source config not found: {src}. "
                f"Set [direwolf] {profile}_config in rigtop.toml "
                f"or place the file in {self.install_path}."
            )
        text = src.read_text(encoding="utf-8")
        lines = []
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.upper().startswith("TBEACON") and not stripped.startswith("#"):
                if not beacon_enabled:
                    line = "# [rigtop beacon off] " + line
            elif stripped.startswith("# [rigtop beacon off] ") and beacon_enabled:
                line = line.split("# [rigtop beacon off] ", 1)[1]
            lines.append(line)
        out = self._config_dir / f"direwolf-{profile}-active.conf"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self._active_config = out
        self._active_profile = profile
        logger.info(
            "Wrote active Direwolf config: {} (TBEACON {})",
            out,
            "enabled" if beacon_enabled else "disabled",
        )
        return out

    def start(self, settle: float = 2.0) -> None:
        """Start Direwolf and wait *settle* seconds for it to initialise."""
        cmd = self._build_command()
        logger.info("Starting Direwolf: {}", " ".join(cmd))
        if self.stderr_callback:
            self.stderr_callback(f"[rigtop] launching: {' '.join(cmd)}")

        if sys.platform == "win32":
            self._start_pty(cmd, settle)
        else:
            self._start_pipe(cmd, settle)

    def _start_pty(self, cmd: list[str], settle: float) -> None:
        """Windows: spawn via ConPTY so Direwolf gets a real console handle."""
        try:
            import winpty  # pywinpty
        except ImportError:
            if self.stderr_callback:
                self.stderr_callback(
                    "[rigtop] pywinpty not installed — falling back to pipe "
                    "(Direwolf console output may not appear)"
                )
            self._start_pipe(cmd, settle)
            return

        # winpty.PtyProcess.spawn wants a single string or list
        self._pty_proc = winpty.PtyProcess.spawn(
            cmd,
            cwd=str(self.install_path),
            dimensions=(50, 220),  # rows x cols (enough for Direwolf's wide output)
        )
        self._stderr_thread = threading.Thread(
            target=self._read_pty,
            daemon=True,
        )
        self._stderr_thread.start()
        time.sleep(settle)
        if not self._pty_proc.isalive():
            exitcode = self._pty_proc.exitstatus
            self._pty_proc = None
            raise RuntimeError(
                f"Direwolf exited immediately (exit code {exitcode}). Command: {' '.join(cmd)}"
            )
        logger.info("Direwolf running via ConPTY")

    def _start_pipe(self, cmd: list[str], settle: float) -> None:
        """Non-Windows (or winpty fallback): spawn with stdout pipe."""
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.install_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
        if self._proc.stdout:
            self._stderr_thread = threading.Thread(
                target=self._read_output,
                daemon=True,
            )
            self._stderr_thread.start()
        time.sleep(settle)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"Direwolf exited immediately (return code {self._proc.returncode}). "
                f"Command: {' '.join(cmd)}"
            )
        logger.info("Direwolf running (pid {})", self._proc.pid)

    def stop(self) -> None:
        """Terminate Direwolf gracefully."""
        if self._pty_proc is not None:
            logger.info("Stopping Direwolf (PTY)")
            try:
                self._pty_proc.terminate()
            except Exception:
                pass
            self._pty_proc = None
        if self._proc is not None:
            logger.info("Stopping Direwolf (pid {})", self._proc.pid)
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("Direwolf did not exit, killing")
                self._proc.kill()
            self._proc = None

    def _read_pty(self) -> None:
        """Background thread: read from the ConPTY handle line by line."""
        proc = self._pty_proc
        if proc is None:
            return
        logger.debug("_read_pty thread started")
        if self.stderr_callback:
            self.stderr_callback("[rigtop] reader thread started, waiting for output…")
        buf = ""
        while proc.isalive():
            try:
                chunk = proc.read(4096)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            # Emit complete lines; hold the last incomplete fragment
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if line:
                    logger.debug("direwolf: {}", line)
                    if self.stderr_callback:
                        self.stderr_callback(line)
        # Flush any remaining partial line
        if buf.strip():
            if self.stderr_callback:
                self.stderr_callback(buf.rstrip("\r\n"))
        if self.stderr_callback:
            self.stderr_callback("[rigtop] Direwolf process ended")

    def _read_output(self) -> None:
        """Background thread: read Direwolf stdout line by line (pipe mode)."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        logger.debug("_read_output thread started")
        if self.stderr_callback:
            self.stderr_callback("[rigtop] reader thread started, waiting for output…")
        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.debug("direwolf: {}", line)
                if self.stderr_callback:
                    self.stderr_callback(line)
        if self.stderr_callback:
            self.stderr_callback("[rigtop] Direwolf process ended")

    @property
    def running(self) -> bool:
        if self._pty_proc is not None:
            return self._pty_proc.isalive()
        return self._proc is not None and self._proc.poll() is None

    @property
    def active_config(self) -> str | None:
        """Return the active profile name (e.g. 'aprs', 'bbs')."""
        return self._active_profile

    def switch_config(self, profile: str, beacon_enabled: bool = True) -> None:
        """Generate an active config for *profile* and restart Direwolf if running."""
        was_running = self.running
        if was_running:
            self.stop()
        self.generate_active_config(profile, beacon_enabled=beacon_enabled)
        if was_running:
            self.start()
