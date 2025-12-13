"""Backend light control: serial COMs, modes, and beat-synced fades."""
from __future__ import annotations

import threading
import time
from enum import Enum
from typing import Any, Optional, Tuple

import serial

RgbTuple = Tuple[int, int, int]


class LightMode(Enum):
    OFF = "off"
    ON = "on"
    FADE_SYNC = "fade_sync"
    AUTO_RGB_FADE = "auto_rgb_fade"
    BEAT_RGB_STEP = "beat_rgb_step"
    SLIDER_SLOW_FADE = "slider_slow_fade"
    FADE_SYNC_EVERY_4 = "fade_sync_every_4"
    STROBE = "strobe"


class LightController:
    """Handle serial LED commands and beat-synced fading (new on-device fade protocol)."""

    def __init__(self, baudrate: int = 115200, mirror: Optional[Any] = None) -> None:
        self._baudrate = baudrate
        self._com_port: Optional[str] = None
        self._serial: Optional[serial.Serial] = None
        self._mode: LightMode = LightMode.OFF
        self._color: RgbTuple = (0, 0, 0)
        self._lock = threading.Lock()
        self._last_beat_send_ts: Optional[float] = None
        self._fade_out_ms: int = 1000
        self._beat_counter: int = 0
        self._cycle_index: int = 0
        self._worker_thread: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        # Tunables for the new modes
        self._cycle_colors: tuple[RgbTuple, ...] = (
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
        )
        self._cycle_interval_sec: float = 2.0
        self._cycle_fade_in_ms: int = 2500
        self._cycle_fade_out_ms: int = 2500
        self._slider_fade_interval_sec: float = 2.5
        self._slider_fade_duration_ms: int = 1200
        self._strobe_interval_sec: float = 0.05
        self._strobe_fade_in_ms: int = 0
        self._strobe_fade_out_ms: int = 25
        # Optional mirror that visualizes the outgoing commands (UI simulator).
        self._mirror = mirror

    # ------------------------------ lifecycle ------------------------------ #
    def close(self) -> None:
        """Stop background work and release serial resources."""
        self._stop_worker()
        self._close_serial()

    def set_com_port(self, com_port: str) -> None:
        """Update COM port target; closes any existing connection."""
        if not com_port:
            raise ValueError("COM port is required")

        old_serial: Optional[serial.Serial] = None
        with self._lock:
            if com_port == self._com_port and self._serial and self._serial.is_open:
                return
            old_serial = self._serial
            self._serial = None
            self._com_port = com_port
        if old_serial:
            try:
                old_serial.close()
            except Exception:
                pass

    def set_mirror(self, mirror: Optional[Any]) -> None:
        """Update the mirror sink used to visualize outgoing serial commands."""
        self._mirror = mirror

    # ------------------------------- commands ------------------------------ #
    def set_mode(self, mode: LightMode) -> None:
        self._stop_worker()
        with self._lock:
            self._mode = mode
            self._beat_counter = 0
            self._cycle_index = 0
            self._last_beat_send_ts = None
        if mode == LightMode.OFF:
            self._send_off()
        elif mode == LightMode.ON:
            self._send_rgb(self._color, fade_in=0, fade_out=0)
        elif mode == LightMode.FADE_SYNC:
            # No immediate action; beat pulses will trigger fades.
            pass
        elif mode in (LightMode.AUTO_RGB_FADE, LightMode.SLIDER_SLOW_FADE, LightMode.STROBE):
            self._start_worker(mode)
        elif mode in (LightMode.BEAT_RGB_STEP, LightMode.FADE_SYNC_EVERY_4):
            # Beat-driven modes, nothing to send yet.
            pass

    def set_color(self, rgb: RgbTuple) -> None:
        clamped = tuple(max(0, min(int(v), 255)) for v in rgb)
        with self._lock:
            self._color = clamped  # type: ignore[assignment]
            mode = self._mode
        if mode == LightMode.ON:
            self._send_rgb(clamped, fade_in=0, fade_out=0)

    def send_static_color(self) -> None:
        """Force-send the current color to the lamp."""
        with self._lock:
            rgb = self._color
        self._send_rgb(rgb, fade_in=0, fade_out=0)

    def set_decay_seconds(self, seconds: float) -> None:
        """Update decay duration (seconds UI) used for beat-triggered fades (stored as ms)."""
        self.set_decay_ms(seconds * 1000.0)

    def set_decay_ms(self, milliseconds: float) -> None:
        """Update decay duration in milliseconds for beat-triggered fades."""
        with self._lock:
            ms = max(0.0, min(float(milliseconds), 10000.0))
            self._fade_out_ms = int(ms)

    # ------------------------------- beat sync ----------------------------- #
    def handle_beat(self, bpm_hint: Optional[float]) -> None:
        """Kick off a fade cycle aligned with an incoming beat."""
        now = time.time()
        # Some controllers emit duplicate beat notes; drop any within 100 ms.
        if self._last_beat_send_ts and (now - self._last_beat_send_ts) < 0.2:
            print(f"[light_controller {self._ts()}] beat suppressed (duplicate within 200ms)")
            return
        with self._lock:
            mode = self._mode
            color = self._color
            fade_out_ms = max(0, self._fade_out_ms)

        if mode == LightMode.FADE_SYNC:
            # Use user-configured decay; ignore BPM for decay timing. Units: ms.
            fade_in_ms = 0  # pop on the beat
            self._send_rgb(color, fade_in=fade_in_ms, fade_out=fade_out_ms)
            self._last_beat_send_ts = now
            return

        if mode == LightMode.BEAT_RGB_STEP:
            color = self._next_cycle_color()
            self._send_rgb(color, fade_in=0, fade_out=fade_out_ms)
            self._last_beat_send_ts = now
            return

        if mode == LightMode.FADE_SYNC_EVERY_4:
            with self._lock:
                self._beat_counter = (self._beat_counter + 1) % 8
                beat_index = self._beat_counter
            if beat_index != 0:
                return
            self._send_rgb(color, fade_in=0, fade_out=fade_out_ms)
            self._last_beat_send_ts = now
            return

    # ------------------------------- workers ------------------------------- #
    def _start_worker(self, mode: LightMode) -> None:
        self._worker_stop = threading.Event()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, args=(mode, self._worker_stop), daemon=True
        )
        self._worker_thread.start()

    def _stop_worker(self) -> None:
        self._worker_stop.set()
        thread = self._worker_thread
        if thread and thread.is_alive():
            thread.join(timeout=0.5)
        self._worker_thread = None

    def _worker_loop(self, mode: LightMode, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                if mode == LightMode.AUTO_RGB_FADE:
                    color = self._next_cycle_color()
                    fade_in = self._cycle_fade_in_ms
                    fade_out = self._cycle_fade_out_ms
                    wait_for = self._cycle_interval_sec
                    self._send_rgb(color, fade_in=fade_in, fade_out=fade_out)
                elif mode == LightMode.SLIDER_SLOW_FADE:
                    with self._lock:
                        color = self._color
                    fade_in = fade_out = self._slider_fade_duration_ms
                    wait_for = max(self._slider_fade_interval_sec, (fade_in + fade_out) / 1000.0)
                    self._send_rgb(color, fade_in=fade_in, fade_out=fade_out)
                elif mode == LightMode.STROBE:
                    with self._lock:
                        color = self._color
                    wait_for = self._strobe_interval_sec
                    self._send_rgb(color, fade_in=self._strobe_fade_in_ms, fade_out=self._strobe_fade_out_ms)
                else:
                    return
            except Exception as exc:  # pragma: no cover - defensive runtime log
                print(f"[light_controller {self._ts()}] worker error in {mode.value}: {exc}")
                wait_for = 1.0
            stop_event.wait(wait_for)

    def _next_cycle_color(self) -> RgbTuple:
        with self._lock:
            idx = self._cycle_index
            self._cycle_index = (self._cycle_index + 1) % len(self._cycle_colors)
            return self._cycle_colors[idx]

    # ------------------------------- transport ----------------------------- #
    def _ensure_serial(self) -> serial.Serial:
        with self._lock:
            ser = self._serial
            port = self._com_port
        if ser and ser.is_open:
            return ser
        if not port:
            raise RuntimeError("Select a COM port first.")

        ser = serial.Serial(port, self._baudrate, timeout=1, write_timeout=0.05)
        try:
            ser.dtr = False  # avoid resets on some boards
        except Exception:
            pass

        with self._lock:
            self._serial = ser
        return ser

    def _send_rgb(self, rgb: RgbTuple, fade_in: int, fade_out: int) -> None:
        r, g, b = (max(0, min(int(v), 255)) for v in rgb)
        fade_in = max(0, int(fade_in))
        fade_out = max(0, int(fade_out))
        cmd = f"RGB {int(r)} {int(g)} {int(b)} {fade_in} {fade_out}\n".encode("ascii")
        if self._mirror:
            try:
                self._mirror.handle_rgb((int(r), int(g), int(b)), fade_in, fade_out)
            except Exception as exc:  # pragma: no cover - UI helper should not kill serial
                print(f"[light_controller {self._ts()}] mirror error (rgb): {exc}")
        self._write_line(cmd)

    def _send_off(self) -> None:
        if self._mirror:
            try:
                self._mirror.handle_off()
            except Exception as exc:  # pragma: no cover
                print(f"[light_controller {self._ts()}] mirror error (off): {exc}")
        self._write_line(b"OFF\n")

    def _write_line(self, data: bytes) -> None:
        ser = self._ensure_serial()
        try:
            if getattr(ser, "out_waiting", 0) > 256:
                ser.reset_output_buffer()
            ser.write(data)
            self._log_send(data)
        except serial.SerialTimeoutException:
            ser.reset_output_buffer()
            ser.write(data)
            self._log_send(data)
        # Small yield to avoid hammering the driver if beats come very quickly.
        time.sleep(0.002)

    def _log_send(self, data: bytes) -> None:
        """Log each serial command as it is sent."""
        try:
            line = data.decode("ascii", errors="ignore").strip()
        except Exception:
            line = repr(data)
        print(f"[light_controller {self._ts()}] sent: {line}")

    def _ts(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime())

    def _clear_output_buffer(self) -> None:
        try:
            ser = self._ensure_serial()
            ser.reset_output_buffer()
        except Exception:
            pass

    def _close_serial(self) -> None:
        with self._lock:
            ser = self._serial
            self._serial = None
        if ser and ser.is_open:
            try:
                ser.close()
            except Exception:
                pass
