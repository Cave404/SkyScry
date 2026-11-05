"""Subprocess lifecycle and stream management for `rtl_433`."""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from typing import Callable, Iterator, Optional

from src.config import SDRConfig, resolve_rtl433_binary

logger = logging.getLogger(__name__)


class SDRStreamError(RuntimeError):
    """Raised when the rtl_433 binary cannot be found or the RTL-SDR hardware fails."""


class SDRStream:
    """Manages the `rtl_433` subprocess lifecycle with non-blocking stderr drain and clean USB release."""

    def __init__(
        self,
        sdr_config: Optional[SDRConfig] = None,
        binary_override: Optional[str] = None,
    ) -> None:
        self.config = sdr_config or SDRConfig()
        self.binary_override = binary_override
        self._process: Optional[subprocess.Popen[str]] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._stop_requested: bool = False
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Return True if the underlying `rtl_433` child process is currently alive."""
        with self._lock:
            return self._process is not None and self._process.poll() is None

    @property
    def last_error(self) -> Optional[str]:
        """Return the most recent hardware or subprocess diagnostic message, if any."""
        return self._last_error

    def resolve_binary(self) -> str:
        """Locate the `rtl_433` binary or raise an actionable SDRStreamError."""
        chosen = self.binary_override or self.config.binary_path
        resolved = resolve_rtl433_binary(chosen)
        if not resolved:
            raise SDRStreamError(
                "Could not locate the `rtl_433` executable. "
                "Install `rtl_433` on your system PATH, place `rtl_433-rtlsdr.exe` / `rtl_433` "
                "in `./bin/`, or set `sdr.binary_path` in `config/config.yaml` "
                "(or `SKYSCRY_RTL433_BIN` in your environment)."
            )
        return resolved

    def build_command(self) -> list[str]:
        """Construct the command-line invocation for `rtl_433`."""
        binary = self.resolve_binary()
        cmd = [
            binary,
            "-f",
            self.config.frequency,
            "-d",
            str(self.config.device_index),
            "-F",
            "json",
        ]
        if self.config.extra_args:
            cmd.extend(self.config.extra_args)
        return cmd

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Continuously read stderr on a daemon thread so pipe buffers never deadlock."""
        if proc.stderr is None:
            return
        try:
            for raw_line in iter(proc.stderr.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                lower = line.lower()
                if any(
                    err_token in lower
                    for err_token in (
                        "libusb_error",
                        "usb_open error",
                        "no supported devices found",
                        "usb_claim_interface error",
                        "rtlsdr_demod_write_reg failed",
                        "rtlsdr_write_reg failed",
                    )
                ):
                    logger.error("[rtl_433] %s", line)
                else:
                    logger.debug("[rtl_433] %s", line)
        except Exception as exc:
            if not self._stop_requested:
                logger.debug("stderr reader exited: %s", exc)

    def _diagnose_failure(self, return_code: int) -> str:
        """Produce a clear, actionable hardware diagnostic from stderr output and exit code."""
        joined = "\n".join(self._stderr_tail)
        lower = joined.lower()

        if "libusb_error_not_found" in lower or "no supported devices found" in lower:
            return (
                f"RTL-SDR hardware not detected (exit code {return_code}). "
                "Verify the USB dongle is plugged in. On Windows, ensure the WinUSB driver "
                "is installed via Zadig for 'Bulk-In, Interface (Interface 0)'."
            )
        if "libusb_error_access" in lower or "usb_claim_interface error" in lower:
            return (
                f"RTL-SDR USB access denied or device busy (exit code {return_code}). "
                "Ensure no other rtl_433/SDR# process is running. On Linux, verify udev rules "
                "and that the `dvb_usb_rtl28xxu` kernel driver is blacklisted."
            )
        if self._stderr_tail:
            last_msg = self._stderr_tail[-1]
            return f"rtl_433 exited unexpectedly with code {return_code}: {last_msg}"
        return f"rtl_433 exited unexpectedly with code {return_code}."

    def start(self) -> None:
        """Spawn the `rtl_433` child process and start the background stderr drain thread."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return

            cmd = self.build_command()
            self._stderr_tail.clear()
            self._last_error = None
            logger.info("Starting RTL-SDR stream: %s", " ".join(cmd))

            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
            except FileNotFoundError as exc:
                raise SDRStreamError(f"Failed to execute rtl_433 binary: {exc}") from exc
            except OSError as exc:
                raise SDRStreamError(f"OS error starting rtl_433 subprocess: {exc}") from exc

            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(self._process,),
                name="rtl433-stderr-drain",
                daemon=True,
            )
            self._stderr_thread.start()

    def stream_lines(
        self,
        on_diagnostic: Optional[Callable[[str], None]] = None,
    ) -> Iterator[str]:
        """Yield raw stdout lines from `rtl_433`, supporting optional auto-reconnect backoff."""
        self._stop_requested = False

        while not self._stop_requested:
            self.start()
            proc = self._process
            if proc is None or proc.stdout is None:
                break

            try:
                for line in iter(proc.stdout.readline, ""):
                    if self._stop_requested:
                        break
                    stripped = line.strip()
                    if stripped:
                        yield stripped
            except KeyboardInterrupt:
                self.stop()
                raise
            finally:
                return_code = proc.poll()
                if return_code is None and self._stop_requested:
                    self.stop()

            if self._stop_requested:
                break

            exit_code = proc.wait()
            diagnostic = self._diagnose_failure(exit_code)
            self._last_error = diagnostic
            if on_diagnostic:
                on_diagnostic(diagnostic)
            else:
                logger.error(diagnostic)

            if not self.config.auto_reconnect:
                raise SDRStreamError(diagnostic)

            delay = max(1.0, self.config.reconnect_delay_seconds)
            logger.warning("Reconnecting to RTL-SDR in %.1f seconds...", delay)
            time.sleep(delay)

    def stop(self) -> None:
        """Gracefully terminate `rtl_433` and force-kill if it does not exit within timeout."""
        self._stop_requested = True
        with self._lock:
            proc = self._process
            self._process = None

        if proc is None:
            return

        if proc.poll() is None:
            logger.info("Stopping rtl_433 subprocess (PID %s)...", proc.pid)
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                logger.warning("rtl_433 (PID %s) did not terminate within 3s; killing process.", proc.pid)
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except Exception:
                    pass
            except Exception as exc:
                logger.debug("Error during rtl_433 termination: %s", exc)

        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

        if self._stderr_thread is not None and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=1.0)
        self._stderr_thread = None

    def __enter__(self) -> SDRStream:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()

