"""Subprocess lifecycle and stream management for `rtl_433` with Windows WinUSB stall recovery."""

from __future__ import annotations

import collections
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Callable, Iterator, Optional

from src.config import SDRConfig, resolve_rtl433_binary

logger = logging.getLogger(__name__)

# Windows NTSTATUS 0xC0000005 (Access Violation) triggered by libusb bulk transfer stall (status 4)
WINDOWS_ACCESS_VIOLATION_CODE = 3221225477
WINDOWS_ACCESS_VIOLATION_SIGNED = -1073741819


class SDRStreamError(RuntimeError):
    """Raised when the rtl_433 binary cannot be found or the RTL-SDR hardware fails."""


class SDRStream:
    """Manages the `rtl_433` subprocess lifecycle with non-blocking queued readers and USB stall recovery."""

    def __init__(
        self,
        sdr_config: Optional[SDRConfig] = None,
        binary_override: Optional[str] = None,
    ) -> None:
        self.config = sdr_config or SDRConfig()
        self.binary_override = binary_override
        self._process: Optional[subprocess.Popen[str]] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._line_queue: queue.Queue[Optional[str]] = queue.Queue(maxsize=4096)
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._stop_requested: bool = False
        self._usb_stall_detected: bool = False
        self._suppressed_reg_errors: int = 0
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
        """Construct the command-line invocation for `rtl_433` with UTC timestamps and RF signal levels."""
        binary = self.resolve_binary()
        cmd = [
            binary,
            "-f",
            self.config.frequency,
            "-d",
            str(self.config.device_index),
            "-M",
            "time:utc",
            "-M",
            "level",
            "-F",
            "json",
        ]
        if self.config.extra_args:
            cmd.extend(self.config.extra_args)
        return cmd

    @staticmethod
    def _cleanup_zombie_processes_windows() -> None:
        """Kill any orphaned `rtl_433-rtlsdr.exe` or `rtl_433.exe` holding the WinUSB handle on Windows."""
        if os.name != "nt":
            return
        for exe_name in ("rtl_433-rtlsdr.exe", "rtl_433.exe"):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", exe_name, "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3.0,
                    check=False,
                )
            except Exception:
                pass

    def _drain_stdout(self, proc: subprocess.Popen[str]) -> None:
        """Continuously drain `stdout` on a dedicated daemon thread so OS pipe buffers never block `rtl_433`."""
        if proc.stdout is None:
            return
        try:
            for raw_line in iter(proc.stdout.readline, ""):
                if self._stop_requested:
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    self._line_queue.put_nowait(stripped)
                except queue.Full:
                    # Drop oldest queued frame if consumer lags so libusb bulk transfers never stall
                    try:
                        self._line_queue.get_nowait()
                        self._line_queue.put_nowait(stripped)
                    except queue.Empty:
                        pass
        except Exception as exc:
            if not self._stop_requested:
                logger.debug("stdout reader exited: %s", exc)
        finally:
            try:
                self._line_queue.put_nowait(None)
            except queue.Full:
                pass

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        """Continuously drain `stderr`, consolidating repetitive `-9` pipe errors and triggering fast recovery."""
        if proc.stderr is None:
            return
        try:
            for raw_line in iter(proc.stderr.readline, ""):
                line = raw_line.strip()
                if not line:
                    continue
                self._stderr_tail.append(line)
                lower = line.lower()

                # Consolidate repetitive LIBUSB_ERROR_PIPE (-9) register write spam
                if (
                    "rtlsdr_demod_write_reg failed with -9" in lower
                    or "rtlsdr_write_reg failed with -9" in lower
                    or "r82xx_write: i2c wr failed=-9" in lower
                ):
                    self._suppressed_reg_errors += 1
                    self._usb_stall_detected = True
                    if self._suppressed_reg_errors == 1:
                        logger.warning(
                            "[rtl_433] USB control pipe error (-9 / LIBUSB_ERROR_PIPE) detected; "
                            "suppressing repetitive register error spam and scheduling clean USB reset."
                        )
                    continue

                if "cb transfer status: 4" in lower or "LIBUSB_TRANSFER_STALL" in line:
                    self._usb_stall_detected = True
                    logger.warning(
                        "[rtl_433] USB bulk transfer stall (status 4) detected; "
                        "terminating process cleanly for WinUSB endpoint recovery."
                    )
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    continue

                if any(
                    err_token in lower
                    for err_token in (
                        "libusb_error",
                        "usb_open error",
                        "no supported devices found",
                        "usb_claim_interface error",
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

        if (
            return_code in (WINDOWS_ACCESS_VIOLATION_CODE, WINDOWS_ACCESS_VIOLATION_SIGNED)
            or self._usb_stall_detected
            or "cb transfer status: 4" in lower
        ):
            extra = (
                f" ({self._suppressed_reg_errors} repetitive -9 register errors suppressed)"
                if self._suppressed_reg_errors > 0
                else ""
            )
            return (
                f"Windows WinUSB bulk transfer stall recovered (exit code {return_code}){extra}. "
                "Allowing USB endpoint to settle before re-initializing tuner."
            )

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
        """Kill any zombie processes and spawn the `rtl_433` child process with dedicated reader threads."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return

            self._cleanup_zombie_processes_windows()

            # Clear any leftover items in the queue
            while not self._line_queue.empty():
                try:
                    self._line_queue.get_nowait()
                except queue.Empty:
                    break

            cmd = self.build_command()
            self._stderr_tail.clear()
            self._usb_stall_detected = False
            self._suppressed_reg_errors = 0
            self._last_error = None
            logger.info("Starting RTL-SDR stream: %s", " ".join(cmd))

            creationflags = 0
            if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
                creationflags = subprocess.CREATE_NO_WINDOW

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
                    creationflags=creationflags,
                )
            except FileNotFoundError as exc:
                raise SDRStreamError(f"Failed to execute rtl_433 binary: {exc}") from exc
            except OSError as exc:
                raise SDRStreamError(f"OS error starting rtl_433 subprocess: {exc}") from exc

            self._stdout_thread = threading.Thread(
                target=self._drain_stdout,
                args=(self._process,),
                name="rtl433-stdout-drain",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(self._process,),
                name="rtl433-stderr-drain",
                daemon=True,
            )
            self._stdout_thread.start()
            self._stderr_thread.start()

    def stream_lines(
        self,
        on_diagnostic: Optional[Callable[[str], None]] = None,
    ) -> Iterator[str]:
        """Yield queued stdout lines from `rtl_433` with progressive USB stall recovery."""
        self._stop_requested = False
        consecutive_stalls = 0

        while not self._stop_requested:
            self.start()
            proc = self._process
            if proc is None:
                break

            session_started_at = time.monotonic()
            try:
                while not self._stop_requested:
                    try:
                        item = self._line_queue.get(timeout=0.5)
                    except queue.Empty:
                        if proc.poll() is not None:
                            break
                        continue

                    if item is None:
                        break
                    yield item
            except KeyboardInterrupt:
                self.stop()
                raise
            finally:
                if proc.poll() is None and self._stop_requested:
                    self.stop()

            if self._stop_requested:
                break

            exit_code = proc.wait()
            session_uptime = time.monotonic() - session_started_at
            diagnostic = self._diagnose_failure(exit_code)
            self._last_error = diagnostic

            is_usb_stall = (
                exit_code in (WINDOWS_ACCESS_VIOLATION_CODE, WINDOWS_ACCESS_VIOLATION_SIGNED)
                or self._usb_stall_detected
            )

            if on_diagnostic:
                on_diagnostic(diagnostic)
            elif is_usb_stall:
                logger.warning(diagnostic)
            else:
                logger.error(diagnostic)

            if not self.config.auto_reconnect:
                raise SDRStreamError(diagnostic)

            if session_uptime > 60.0:
                consecutive_stalls = 0
            else:
                consecutive_stalls += 1

            base_delay = max(2.0, self.config.reconnect_delay_seconds)
            if is_usb_stall:
                # Progressive 8-12s recovery window so Windows WinUSB releases the halted bulk endpoint
                delay = min(15.0, max(8.0, base_delay + (consecutive_stalls * 2.0)))
            else:
                delay = min(15.0, base_delay + (consecutive_stalls * 1.5))

            logger.info("Reconnecting to RTL-SDR in %.1f seconds...", delay)
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
                proc.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                logger.warning("rtl_433 (PID %s) did not terminate within 2.5s; killing process.", proc.pid)
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

        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None and t.is_alive():
                t.join(timeout=1.0)
        self._stdout_thread = None
        self._stderr_thread = None

    def __enter__(self) -> SDRStream:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()
