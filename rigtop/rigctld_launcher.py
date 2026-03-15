"""Launch and manage a rigctld subprocess."""

from __future__ import annotations

import shutil
import subprocess
import time

from loguru import logger

# Map Python log-level names to rigctld -v flag count.
_VERBOSITY: dict[str, int] = {
    "ERROR": 0,
    "WARNING": 1,
    "INFO": 3,
    "DEBUG": 5,
}


class RigctldLauncher:
    """Spawn ``rigctld`` and keep a handle so we can tear it down later."""

    def __init__(
        self,
        *,
        model: int = 3085,
        serial_port: str = "COM9",
        baud_rate: int = 19200,
        listen_host: str = "127.0.0.1",
        listen_port: int = 4532,
        log_level: str = "WARNING",
        extra_args: list[str] | None = None,
    ) -> None:
        self.model = model
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.log_level = log_level.upper()
        self.extra_args = extra_args or []
        self._proc: subprocess.Popen[bytes] | None = None

    # ------------------------------------------------------------------

    def _build_command(self) -> list[str]:
        exe = shutil.which("rigctld")
        if exe is None:
            raise FileNotFoundError(
                "rigctld not found on PATH. Install Hamlib and ensure rigctld is available."
            )
        cmd = [
            exe,
            "-m", str(self.model),
            "-r", self.serial_port,
            "-s", str(self.baud_rate),
            "-T", self.listen_host,
            "-t", str(self.listen_port),
        ]
        v_count = _VERBOSITY.get(self.log_level, 1)
        if v_count:
            cmd.append("-" + "v" * v_count)
        cmd.extend(self.extra_args)
        return cmd

    # ------------------------------------------------------------------

    def start(self, settle: float = 1.0) -> None:
        """Start rigctld and wait *settle* seconds for it to be ready."""
        cmd = self._build_command()
        logger.info("Starting rigctld: {}", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Give rigctld a moment to bind the TCP port.
        time.sleep(settle)
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"rigctld exited immediately (return code {self._proc.returncode}). "
                f"Command: {' '.join(cmd)}"
            )
        logger.info("rigctld running (pid {})", self._proc.pid)

    def stop(self) -> None:
        """Terminate rigctld gracefully."""
        if self._proc is None:
            return
        logger.info("Stopping rigctld (pid {})", self._proc.pid)
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("rigctld did not exit, killing")
            self._proc.kill()
        self._proc = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __enter__(self) -> RigctldLauncher:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
