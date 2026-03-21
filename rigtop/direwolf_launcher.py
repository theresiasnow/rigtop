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
        self._pty_proc = None           # winpty PtyProcess, Windows only
        self._stderr_thread: threading.Thread | None = None

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
        if not self.config_file:
            raise RuntimeError(
                "No config_file set — call switch_config() or "
                "use :aprs on / :bbs on first."
            )
        conf = self.install_path / self.config_file
        if not conf.is_file():
            raise FileNotFoundError(
                f"Config file not found: {conf}."
            )
        cmd = [exe, "-t", "0", "-c", str(conf)]
        cmd.extend(self.extra_args)
        return cmd

    # ------------------------------------------------------------------

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
            dimensions=(50, 220),   # rows x cols (enough for Direwolf's wide output)
        )
        self._stderr_thread = threading.Thread(
            target=self._read_pty, daemon=True,
        )
        self._stderr_thread.start()
        time.sleep(settle)
        if not self._pty_proc.isalive():
            exitcode = self._pty_proc.exitstatus
            self._pty_proc = None
            raise RuntimeError(
                f"Direwolf exited immediately (exit code {exitcode}). "
                f"Command: {' '.join(cmd)}"
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
                target=self._read_output, daemon=True,
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
