"""Subprocess lifecycle and stream management for `rtl_433` with USB stall recovery and sysfs driver unbind."""

from __future__ import annotations

import collections
import logging
import os
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from src.config import SDRConfig, resolve_rtl433_binary

logger = logging.getLogger(__name__)

# Windows NTSTATUS 0xC0000005 (Access Violation) triggered by libusb bulk transfer stall (status 4)
WINDOWS_ACCESS_VIOLATION_CODE = 3221225477
WINDOWS_ACCESS_VIOLATION_SIGNED = -1073741819


class SDRStreamError(RuntimeError):
    """Raised when the rtl_433 binary cannot be found or the RTL-SDR hardware fails."""


def fix_usb_permissions() -> int:
    """Scan /dev/bus/usb/*/* and chmod device nodes to 0o666 (rw-rw-rw-).

    Ensures unprivileged threads or container worker processes can access
    the RTL-SDR USB endpoints without triggering `usb_open error -4`.
    Returns the count of USB device nodes modified.
    """
    if os.name != "posix":
        return 0

    dev_usb = Path("/dev/bus/usb")
    if not dev_usb.exists():
        return 0

    modified = 0
    try:
        for bus_dir in dev_usb.iterdir():
            if bus_dir.is_dir():
                for dev_node in bus_dir.iterdir():
                    try:
                        os.chmod(dev_node, 0o666)
                        modified += 1
                    except Exception as exc:
                        logger.debug("[USB PERMS] Could not chmod %s: %s", dev_node, exc)
    except Exception as exc:
        logger.debug("[USB PERMS] Error scanning /dev/bus/usb: %s", exc)

    if modified > 0:
        logger.info("[USB PERMS] Set 0666 permissions on %d USB device node(s) under /dev/bus/usb.", modified)
    return modified


def unbind_and_reset_rtlsdr_usb() -> bool:
    """Scan Linux sysfs to unbind dvb_usb_rtl28xxu and reset Realtek RTL-SDR USB dongles.

    Works inside Docker containers (with /sys and /dev mounted) even when rmmod
    fails due to built-in kernel modules or lack of CAP_SYS_MODULE.
    """
    if os.name != "posix":
        return False

    recovered = False

    # 1. Sysfs driver unbind from dvb_usb_rtl28xxu and related drivers
    for driver_name in ("dvb_usb_rtl28xxu", "rtl2832", "rtl2830", "dvb_usb_v2"):
        driver_dir = Path(f"/sys/bus/usb/drivers/{driver_name}")
        unbind_file = driver_dir / "unbind"
        if driver_dir.exists() and unbind_file.exists():
            try:
                for entry in driver_dir.iterdir():
                    # Bound interface device IDs contain ':' (e.g. '1-1:1.0', '2-3:1.0')
                    if ":" in entry.name:
                        try:
                            logger.info("[USB SYSFS] Unbinding device %s from kernel driver %s...", entry.name, driver_name)
                            with open(unbind_file, "w", encoding="utf-8") as f:
                                f.write(f"{entry.name}\n")
                            recovered = True
                        except Exception as exc:
                            logger.debug("[USB SYSFS] Could not unbind %s from %s: %s", entry.name, driver_name, exc)
            except Exception as exc:
                logger.debug("[USB SYSFS] Error scanning driver directory %s: %s", driver_dir, exc)

    # Fallback attempt rmmod if available
    try:
        subprocess.run(
            ["rmmod", "dvb_usb_rtl28xxu", "rtl2832", "rtl2830"],
            capture_output=True,
            timeout=3.0,
            check=False,
        )
    except Exception:
        pass

    # 2. Locate Realtek RTL2832U dongles (idVendor=0bda, idProduct in 2838, 2832, 2831)
    usb_devices_dir = Path("/sys/bus/usb/devices")
    if usb_devices_dir.exists():
        try:
            for dev_entry in usb_devices_dir.iterdir():
                vendor_file = dev_entry / "idVendor"
                product_file = dev_entry / "idProduct"
                if not (vendor_file.is_file() and product_file.is_file()):
                    continue
                try:
                    v_id = vendor_file.read_text(encoding="utf-8").strip().lower()
                    p_id = product_file.read_text(encoding="utf-8").strip().lower()
                except Exception:
                    continue

                if v_id in ("0bda", "bda") and p_id in ("2838", "2832", "2831"):
                    logger.info("[USB RESET] Located RTL-SDR dongle at %s (0bda:%s)", dev_entry.name, p_id)

                    # A. Check and unbind any kernel driver directly attached to this device's interfaces
                    for iface_entry in dev_entry.iterdir():
                        if ":" in iface_entry.name:
                            driver_link = iface_entry / "driver"
                            if driver_link.exists() and driver_link.is_symlink():
                                unbind_target = driver_link / "unbind"
                                if unbind_target.exists():
                                    try:
                                        logger.info(
                                            "[USB SYSFS] Unbinding interface %s from driver %s...",
                                            iface_entry.name,
                                            driver_link.resolve().name,
                                        )
                                        with open(unbind_target, "w", encoding="utf-8") as f:
                                            f.write(f"{iface_entry.name}\n")
                                        recovered = True
                                    except Exception as exc:
                                        logger.debug("[USB SYSFS] Interface unbind failed for %s: %s", iface_entry.name, exc)

                    # B. Determine device node path (/dev/bus/usb/<bus>/<dev>) and chmod 0666
                    dev_path: Optional[str] = None
                    bus_file = dev_entry / "busnum"
                    dev_file = dev_entry / "devnum"
                    if bus_file.is_file() and dev_file.is_file():
                        try:
                            bus_num = int(bus_file.read_text(encoding="utf-8").strip())
                            dev_num = int(dev_file.read_text(encoding="utf-8").strip())
                            dev_path = f"/dev/bus/usb/{bus_num:03d}/{dev_num:03d}"
                            if os.path.exists(dev_path):
                                try:
                                    os.chmod(dev_path, 0o666)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # C. Toggle authorized (0 -> wait 0.5s -> 1) to force software port re-initialization
                    auth_file = dev_entry / "authorized"
                    if auth_file.is_file():
                        try:
                            logger.info("[USB RESET] Toggling USB authorized on %s to force port re-initialization...", dev_entry.name)
                            auth_file.write_text("0\n", encoding="utf-8")
                            time.sleep(0.5)
                            auth_file.write_text("1\n", encoding="utf-8")
                            time.sleep(0.5)
                            recovered = True
                        except Exception as exc:
                            logger.debug("[USB RESET] Toggling authorized failed: %s", exc)

                    # Re-check and re-chmod dev_path if bus/dev numbers refreshed
                    if bus_file.is_file() and dev_file.is_file():
                        try:
                            bus_num = int(bus_file.read_text(encoding="utf-8").strip())
                            dev_num = int(dev_file.read_text(encoding="utf-8").strip())
                            dev_path = f"/dev/bus/usb/{bus_num:03d}/{dev_num:03d}"
                            if os.path.exists(dev_path):
                                try:
                                    os.chmod(dev_path, 0o666)
                                except Exception:
                                    pass
                        except Exception:
                            pass

                    # D. Hardware port reset via ioctl(USBDEVFS_RESET) on /dev/bus/usb/<bus>/<dev>
                    if dev_path and os.path.exists(dev_path):
                        try:
                            import fcntl
                            # USBDEVFS_RESET = ord('U') << 8 | 20 = 21780
                            USBDEVFS_RESET = 21780
                            fd = os.open(dev_path, os.O_WRONLY)
                            try:
                                fcntl.ioctl(fd, USBDEVFS_RESET, 0)
                                logger.info("[USB RESET] ioctl(USBDEVFS_RESET) succeeded on %s", dev_path)
                                recovered = True
                            finally:
                                os.close(fd)
                        except Exception as exc:
                            logger.debug("[USB RESET] ioctl USBDEVFS_RESET failed on %s: %s", dev_path, exc)
                            # Fallback: try usbreset executable if installed
                            try:
                                subprocess.run(["usbreset", dev_path], capture_output=True, timeout=3.0, check=False)
                            except Exception:
                                pass

                    # Ensure node remains 0666 after ioctl reset
                    if dev_path and os.path.exists(dev_path):
                        try:
                            os.chmod(dev_path, 0o666)
                        except Exception:
                            pass
        except Exception as exc:
            logger.debug("[USB RESET] Error scanning /sys/bus/usb/devices: %s", exc)

    # 3. Always ensure device nodes have 0666 permissions across all buses
    fix_usb_permissions()

    return recovered


class SDRStream:
    """Manages the `rtl_433` subprocess lifecycle with non-blocking queued readers, single thread lock, and USB stall recovery."""

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
        self._is_streaming: bool = False
        self._is_reconnecting: bool = False
        self._is_permission_denied: bool = False
        self._last_exit_code: Optional[int] = None
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
    def is_streaming(self) -> bool:
        """Return True if stream_lines() is actively running in the ingestion worker thread."""
        return self._is_streaming

    @property
    def is_reconnecting(self) -> bool:
        """Return True if stream_lines() is currently waiting in a reconnect backoff delay."""
        return self._is_reconnecting

    @property
    def is_usb_permission_denied(self) -> bool:
        """Return True if the SDR stream failed due to USB permission denial (usb_open error -4) or code 2."""
        if self._is_permission_denied:
            return True
        if self._last_exit_code == 2:
            return True
        err = (self._last_error or "").lower()
        if any(tok in err for tok in ("permission denied", "usb_open error", "error -4", "libusb_error_access", "usb access denied")):
            return True
        joined_tail = " ".join(self._stderr_tail).lower()
        if any(tok in joined_tail for tok in ("error -4", "permission denied", "usb_open error")):
            return True
        return False

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
        """Construct the command-line invocation for `rtl_433` with UTC timestamps, signal levels, and verbose error logging."""
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
            "-F",
            "log",
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

    @staticmethod
    def _attempt_unbind_dvb_driver() -> None:
        """Attempt to unbind conflicting Linux kernel DVB driver (dvb_usb_rtl28xxu) via sysfs and rmmod."""
        if os.name != "posix":
            return
        unbind_and_reset_rtlsdr_usb()

    def _cleanup_orphaned_processes(self) -> None:
        """Terminate active child process and kill any orphaned rtl_433 processes, waiting 1.5s for libusb release."""
        if self._process is not None:
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    self._process.wait(timeout=1.5)
            except Exception:
                try:
                    self._process.kill()
                    self._process.wait(timeout=1.0)
                except Exception:
                    pass
            self._process = None

        if os.name == "posix":
            try:
                subprocess.run(["pkill", "-9", "-f", "rtl_433"], capture_output=True, check=False)
            except Exception:
                pass
        elif os.name == "nt":
            self._cleanup_zombie_processes_windows()

        # Wait 1.5 seconds so libusb and kernel completely release the USB device handle and endpoints
        time.sleep(1.5)

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

                # With '-F json -F log', rtl_433 outputs JSON telemetry alongside diagnostic log messages
                if stripped.startswith("{"):
                    try:
                        self._line_queue.put_nowait(stripped)
                    except queue.Full:
                        # Drop oldest queued frame if consumer lags so libusb bulk transfers never stall
                        try:
                            self._line_queue.get_nowait()
                            self._line_queue.put_nowait(stripped)
                        except queue.Empty:
                            pass
                else:
                    # Log message from -F log: store in stderr tail for diagnosis and log to console
                    self._stderr_tail.append(stripped)
                    lower = stripped.lower()
                    if any(
                        tok in lower
                        for tok in (
                            "error -4",
                            "usb_open error",
                            "libusb_error_access",
                            "permission denied",
                            "usb access denied",
                            "usb_claim_interface error -6",
                        )
                    ):
                        self._is_permission_denied = True
                    if any(err_token in lower for err_token in ("error", "failed", "libusb", "usb_open", "usb_claim", "no supported devices")):
                        logger.error("[rtl_433 log] %s", stripped)
                    else:
                        logger.debug("[rtl_433 log] %s", stripped)
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
                    tok in lower
                    for tok in (
                        "error -4",
                        "usb_open error",
                        "libusb_error_access",
                        "permission denied",
                        "usb access denied",
                        "usb_claim_interface error -6",
                    )
                ):
                    self._is_permission_denied = True

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
        """Produce a clear, actionable hardware diagnostic from stderr/log output and exit code."""
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
                "is installed via Zadig for 'Bulk-In, Interface (Interface 0)'. On Linux/TrueNAS, "
                "verify /dev/bus/usb is mounted in docker-compose.yml."
            )
        if (
            self._is_permission_denied
            or "libusb_error_access" in lower
            or "usb_claim_interface error" in lower
            or "usb_open error" in lower
            or "permission denied" in lower
            or return_code == 2
        ):
            last_msg = self._stderr_tail[-1] if self._stderr_tail else "USB interface busy or permission denied"
            return (
                f"RTL-SDR USB access denied or device busy (exit code {return_code}: {last_msg}). "
                "Ensure no other rtl_433/SDR process is running. On Linux/TrueNAS, verify "
                "the container has privileged: true, /dev/bus/usb is mounted, and dvb_usb_rtl28xxu is unbound."
            )

        if self._stderr_tail:
            last_msg = self._stderr_tail[-1]
            return f"rtl_433 exited unexpectedly with code {return_code}: {last_msg}"
        return f"rtl_433 exited unexpectedly with code {return_code}."

    def start(self) -> None:
        """Kill any zombie/orphaned processes, unbind drivers, fix USB node permissions, and spawn the `rtl_433` child process."""
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return

            self._cleanup_orphaned_processes()

            # Always perform sysfs unbind, reset, and permission fix pre-flight
            unbind_and_reset_rtlsdr_usb()
            fix_usb_permissions()

            # Clear any leftover items in the queue
            while not self._line_queue.empty():
                try:
                    self._line_queue.get_nowait()
                except queue.Empty:
                    break

            cmd = self.build_command()
            self._stderr_tail.clear()
            self._usb_stall_detected = False
            self._is_permission_denied = False
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
        self._is_streaming = True
        consecutive_stalls = 0

        try:
            while not self._stop_requested:
                self._is_reconnecting = False
                self.start()
                with self._lock:
                    proc = self._process
                if proc is None:
                    break

                session_started_at = time.monotonic()
                last_packet_at = time.monotonic()
                try:
                    while not self._stop_requested:
                        try:
                            item = self._line_queue.get(timeout=0.5)
                            last_packet_at = time.monotonic()
                        except queue.Empty:
                            if proc.poll() is not None:
                                break
                            # Inactivity Watchdog: If rtl_433 subprocess produces zero packets for >5 minutes (300s)
                            if time.monotonic() - last_packet_at > 300.0:
                                logger.warning(
                                    "[SDR WATCHDOG] rtl_433 produced zero packets for >5 minutes (possible USB bus stall). Forcing restart..."
                                )
                                unbind_and_reset_rtlsdr_usb()
                                try:
                                    proc.terminate()
                                    proc.wait(timeout=2.0)
                                except Exception:
                                    try:
                                        proc.kill()
                                    except Exception:
                                        pass
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
                self._last_exit_code = exit_code
                session_uptime = time.monotonic() - session_started_at
                diagnostic = self._diagnose_failure(exit_code)
                self._last_error = diagnostic

                is_usb_stall = (
                    exit_code in (WINDOWS_ACCESS_VIOLATION_CODE, WINDOWS_ACCESS_VIOLATION_SIGNED)
                    or self._usb_stall_detected
                    or exit_code == 2
                )

                # Hardware USB reset on exit code 2 or USB stall
                if exit_code == 2 or is_usb_stall:
                    logger.warning("[SDR] rtl_433 exited with code %s (USB error/stall); performing sysfs unbind & hardware USB port reset.", exit_code)
                    unbind_and_reset_rtlsdr_usb()
                    fix_usb_permissions()

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
                    delay = min(15.0, max(8.0, base_delay + (consecutive_stalls * 2.0)))
                else:
                    delay = min(15.0, base_delay + (consecutive_stalls * 1.5))

                self._is_reconnecting = True
                logger.info("Reconnecting to RTL-SDR in %.1f seconds...", delay)

                # Sleep in short increments so stop() or shutdown interrupts promptly
                sleep_end = time.monotonic() + delay
                while time.monotonic() < sleep_end and not self._stop_requested:
                    time.sleep(0.5)
        finally:
            self._is_reconnecting = False
            self._is_streaming = False

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

    def restart(self) -> None:
        """Force stop and restart the underlying rtl_433 subprocess under a thread-safe lifecycle lock."""
        with self._lock:
            logger.info("[SDR] Restart requested.")
            self._stop_requested = True
            proc = self._process
            self._process = None

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass

        # Perform hardware USB reset, sysfs driver unbind, and node permission fix
        unbind_and_reset_rtlsdr_usb()
        fix_usb_permissions()

        with self._lock:
            self._stop_requested = False

        self.start()

    def reset_usb_and_restart(self) -> dict[str, Any]:
        """Forcefully terminate rtl_433, clear zombies, unbind conflicting drivers, fix permissions, and restart stream."""
        logger.info("[SDR] Manual USB reset and reconnect initiated...")
        self.stop()
        self._cleanup_orphaned_processes()
        sysfs_ok = unbind_and_reset_rtlsdr_usb()
        perms_count = fix_usb_permissions()
        self._is_permission_denied = False
        self._last_exit_code = None
        self._last_error = None
        try:
            self.start()
            return {
                "status": "success",
                "message": f"USB reset completed (sysfs_reset={sysfs_ok}, chmod_nodes={perms_count}). RTL-SDR stream restarted.",
            }
        except Exception as exc:
            logger.error("[SDR] Error restarting stream after USB reset: %s", exc)
            return {
                "status": "error",
                "message": f"USB reset performed, but restart encountered error: {exc}",
            }

    def __enter__(self) -> SDRStream:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()
