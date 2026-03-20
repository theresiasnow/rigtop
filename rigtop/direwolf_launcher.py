"""Launch and manage a Direwolf subprocess."""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from loguru import logger


class DirewolfLauncher:
    """Spawn ``direwolf`` and keep a handle so we can tear it down later."""

    def __init__(
        self,
        *,
        install_path: str = "c:\\direwolf",
        stderr_callback: Callable[[str], None] | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        self.install_path = Path(install_path)
        self.config_file: str | None = None
        self.stderr_callback = stderr_callback
        self.extra_args = extra_args or []
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_thread: threading.Thread | None = None

    # ------------------------------------------------------------------

    def _build_command(self) -> list[str]:
        exe = self.install_path / "direwolf.exe"
        if not exe.is_file():
            raise FileNotFoundError(
                f"direwolf.exe not found at {exe}. Check [direwolf] install_path in rigtop.toml."
            )
        if not self.config_file:
            raise RuntimeError(
                "No config_file set — call switch_config() or use :aprs on / :bbs on first."
            )
        conf = self.install_path / self.config_file
        if not conf.is_file():
            raise FileNotFoundError(f"Config file not found: {conf}.")
        cmd = [str(exe), "-c", str(conf)]
        cmd.extend(self.extra_args)
        return cmd

    # ------------------------------------------------------------------

    def start(self, settle: float = 2.0) -> None:
        """Start Direwolf and wait *settle* seconds for it to initialise."""
        cmd = self._build_command()
        logger.info("Starting Direwolf: {}", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            cwd=str(self.install_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        # Direwolf writes to stdout; merge via STDOUT redirect.
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
        if self._proc is None:
            return
        logger.info("Stopping Direwolf (pid {})", self._proc.pid)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Direwolf did not exit, killing")
            self._proc.kill()
        self._proc = None

    def _read_output(self) -> None:
        """Background thread: read Direwolf stdout line by line."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                logger.debug("direwolf: {}", line)
                if self.stderr_callback:
                    self.stderr_callback(line)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def active_config(self) -> str | None:
        """Return the config file name currently in use."""
        return self.config_file

    def switch_config(self, config_file: str) -> None:
        """Stop Direwolf, switch config, and restart."""
        conf = self.install_path / config_file
        if not conf.is_file():
            raise FileNotFoundError(f"Config file not found: {conf}")
        was_running = self.running
        if was_running:
            self.stop()
        self.config_file = config_file
        if was_running:
            self.start()
