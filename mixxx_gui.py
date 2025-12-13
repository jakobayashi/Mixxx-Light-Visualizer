"""Simple Tkinter GUI for the Mixxx light listener.

It lets you pick a MIDI input port and a serial COM port, then shows the
incoming beat/BPM information decoded by mixxx_listener.MixxxLightDecoder.
"""
import json
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Optional, Tuple

import mido
import serial.tools.list_ports

import light_controller
import mixxx_listener


CONFIG_PATH = Path(__file__).with_name("mode_slider_config.json")
DEFAULT_RGB = (0, 0, 0)
DEFAULT_DECAY_MS = 1000.0
MODE_SLIDER_REQUIREMENTS = {
    light_controller.LightMode.ON: {"rgb": True, "decay": False},
    light_controller.LightMode.OFF: {"rgb": False, "decay": False},
    light_controller.LightMode.FADE_SYNC: {"rgb": True, "decay": True},
    light_controller.LightMode.AUTO_RGB_FADE: {"rgb": False, "decay": False},
    light_controller.LightMode.BEAT_RGB_STEP: {"rgb": False, "decay": True},
    light_controller.LightMode.SLIDER_SLOW_FADE: {"rgb": True, "decay": False},
    light_controller.LightMode.FADE_SYNC_EVERY_4: {"rgb": True, "decay": True},
    light_controller.LightMode.STROBE: {"rgb": True, "decay": False},
}


class ModeSettingsStore:
    """Persist per-mode slider values (RGB + decay) to JSON."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                self._data = {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except FileNotFoundError:
            self._data = self._default_data()
            self.save()
        except Exception as exc:
            print(f"[config] Failed to load {self.path}: {exc}")
            self._data = self._default_data()

    def _default_data(self) -> dict[str, dict[str, object]]:
        data: dict[str, dict[str, object]] = {}
        for mode, needs in MODE_SLIDER_REQUIREMENTS.items():
            entry: dict[str, object] = {}
            if needs["rgb"]:
                entry["rgb"] = list(DEFAULT_RGB)
            if needs["decay"]:
                entry["decay_ms"] = DEFAULT_DECAY_MS
            if entry:
                data[mode.value] = entry
        return data

    def save(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
        except Exception as exc:
            print(f"[config] Failed to save {self.path}: {exc}")

    def get(self, mode: light_controller.LightMode) -> dict[str, object]:
        return dict(self._data.get(mode.value, {}))

    def update(
        self,
        mode: light_controller.LightMode,
        rgb: Optional[tuple[int, int, int]] = None,
        decay_ms: Optional[float] = None,
    ) -> None:
        entry = dict(self._data.get(mode.value, {}))
        if rgb is not None:
            entry["rgb"] = [max(0, min(int(v), 255)) for v in rgb]
        if decay_ms is not None:
            entry["decay_ms"] = max(0.0, float(decay_ms))
        if entry:
            self._data[mode.value] = entry
        elif mode.value in self._data:
            # Clean up empty entries.
            self._data.pop(mode.value, None)
        self.save()


class _ChannelFadeSimulator:
    """Mirror of the Arduino ChannelFade logic (per channel fade in/out)."""

    def __init__(self) -> None:
        self.target = 0
        self.fade_in_ms = 0
        self.fade_out_ms = 0
        self.phase_start_ms = 0.0
        self.phase: str = "idle"  # idle | in | out

    def reset(self) -> None:
        self.target = 0
        self.fade_in_ms = 0
        self.fade_out_ms = 0
        self.phase_start_ms = 0.0
        self.phase = "idle"

    def start(self, target: int, fade_in_ms: int, fade_out_ms: int, now_ms: float) -> None:
        self.target = max(0, min(int(target), 255))
        self.fade_in_ms = max(0, int(fade_in_ms))
        self.fade_out_ms = max(0, int(fade_out_ms))
        self.phase_start_ms = now_ms
        self.phase = "out" if self.fade_in_ms == 0 else "in"

    def advance(self, now_ms: float) -> Optional[int]:
        if self.phase == "idle":
            return None

        if self.phase == "in":
            if self.fade_in_ms == 0:
                self.phase_start_ms = now_ms
                self.phase = "out" if self.fade_out_ms > 0 else "idle"
                return self.target

            progress = (now_ms - self.phase_start_ms) / float(self.fade_in_ms)
            progress = max(0.0, min(progress, 1.0))
            if progress >= 1.0:
                self.phase_start_ms = now_ms
                self.phase = "out" if self.fade_out_ms > 0 else "idle"
                return self.target
            return int(self.target * progress)

        if self.phase == "out":
            if self.fade_out_ms == 0:
                self.phase = "idle"
                return self.target

            progress = (now_ms - self.phase_start_ms) / float(self.fade_out_ms)
            progress = max(0.0, min(progress, 1.0))
            if progress >= 1.0:
                self.reset()
                return 0
            level = 1.0 - progress
            return int(self.target * level)

        return None


class LampStateMirror:
    """Simulate the on-device fade logic so the GUI can mirror lamp output."""

    def __init__(self) -> None:
        self._fades = [_ChannelFadeSimulator() for _ in range(3)]
        self._current = [0, 0, 0]
        self._lock = threading.Lock()
        self._last_command: dict[str, object] = {
            "rgb": (0, 0, 0),
            "fade_in_ms": 0,
            "fade_out_ms": 0,
            "state": "off",
        }

    def handle_rgb(self, rgb: Tuple[int, int, int], fade_in_ms: int, fade_out_ms: int) -> None:
        """Mirror the Arduino startFadeFromSerial behaviour."""
        r, g, b = (max(0, min(int(v), 255)) for v in rgb)
        fade_in_ms = max(0, int(fade_in_ms))
        fade_out_ms = max(0, int(fade_out_ms))
        now_ms = time.monotonic() * 1000.0

        with self._lock:
            # Both zero => immediate set (channels with zero stay unchanged, matching firmware quirk).
            if fade_in_ms == 0 and fade_out_ms == 0:
                for fade in self._fades:
                    fade.reset()
                if r > 0:
                    self._current[0] = r
                if g > 0:
                    self._current[1] = g
                if b > 0:
                    self._current[2] = b
            else:
                if r > 0:
                    self._fades[0].start(r, fade_in_ms, fade_out_ms, now_ms)
                if g > 0:
                    self._fades[1].start(g, fade_in_ms, fade_out_ms, now_ms)
                if b > 0:
                    self._fades[2].start(b, fade_in_ms, fade_out_ms, now_ms)

                # Mirror the immediate firmware updateFades() run.
                self._advance_locked(now_ms)

            self._last_command = {
                "rgb": (r, g, b),
                "fade_in_ms": fade_in_ms,
                "fade_out_ms": fade_out_ms,
                "state": "rgb",
            }

    def handle_off(self) -> None:
        with self._lock:
            for fade in self._fades:
                fade.reset()
            self._current = [0, 0, 0]
        self._last_command = {"rgb": (0, 0, 0), "fade_in_ms": 0, "fade_out_ms": 0, "state": "off"}

    def tick(self) -> tuple[tuple[int, int, int], dict[str, object]]:
        """Advance fades based on real time and return the current RGB + last command."""
        now_ms = time.monotonic() * 1000.0
        with self._lock:
            self._advance_locked(now_ms)
            return (tuple(self._current), dict(self._last_command))

    def _advance_locked(self, now_ms: float) -> None:
        next_vals = list(self._current)
        changed = False
        for idx, fade in enumerate(self._fades):
            val = fade.advance(now_ms)
            if val is not None and val != next_vals[idx]:
                next_vals[idx] = val
                changed = True
        if changed:
            self._current = next_vals


class MixxxGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Mixxx Light Visualizer")

        self.decoder = mixxx_listener.MixxxLightDecoder()
        self.state_lock = threading.Lock()
        self.state_snapshot = {}
        self.last_beat_time: Optional[float] = None
        self.running = False
        self.listen_thread: Optional[threading.Thread] = None
        self.lamp_mirror = LampStateMirror()
        self.light_controller = light_controller.LightController(mirror=self.lamp_mirror)
        self.mode_var = tk.StringVar(value=light_controller.LightMode.OFF.value)
        self.decay_var = tk.DoubleVar(value=1000.0)  # milliseconds
        self.settings_store = ModeSettingsStore(CONFIG_PATH)
        self._suppress_slider_events = False
        self._suppress_decay_event = False
        self._visual_last_rgb: tuple[int, int, int] = (0, 0, 0)

        self._build_ui()
        self.refresh_ports()
        self._poll_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _build_ui(self) -> None:
        padding = {"padx": 10, "pady": 6}

        ports_frame = ttk.LabelFrame(self.root, text="Connections")
        ports_frame.grid(row=0, column=0, sticky="ew", **padding)
        ports_frame.columnconfigure(1, weight=1)

        ttk.Label(ports_frame, text="MIDI port").grid(row=0, column=0, sticky="w")
        self.midi_combo = ttk.Combobox(ports_frame, state="readonly")
        self.midi_combo.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        ttk.Label(ports_frame, text="COM port").grid(row=1, column=0, sticky="w")
        self.com_combo = ttk.Combobox(ports_frame, state="readonly")
        self.com_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0))
        self.com_combo.bind("<<ComboboxSelected>>", self._on_com_port_change)

        actions = ttk.Frame(ports_frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        actions.columnconfigure(0, weight=1)

        self.start_btn = ttk.Button(actions, text="Start listening", command=self.start_listening)
        self.start_btn.grid(row=0, column=0, sticky="w")

        self.refresh_btn = ttk.Button(actions, text="Refresh ports", command=self.refresh_ports)
        self.refresh_btn.grid(row=0, column=1, sticky="e", padx=(8, 0))

        status_frame = ttk.LabelFrame(self.root, text="Mixxx state")
        status_frame.grid(row=1, column=0, sticky="ew", **padding)
        status_frame.columnconfigure(1, weight=1)

        ttk.Label(status_frame, text="Reported BPM").grid(row=0, column=0, sticky="w")
        self.reported_bpm_label = ttk.Label(status_frame, text="--")
        self.reported_bpm_label.grid(row=0, column=1, sticky="w")

        ttk.Label(status_frame, text="Calculated BPM").grid(row=1, column=0, sticky="w")
        self.calculated_bpm_label = ttk.Label(status_frame, text="--")
        self.calculated_bpm_label.grid(row=1, column=1, sticky="w")

        ttk.Label(status_frame, text="Current deck").grid(row=2, column=0, sticky="w")
        self.deck_label = ttk.Label(status_frame, text="--")
        self.deck_label.grid(row=2, column=1, sticky="w")

        ttk.Label(status_frame, text="Beat status").grid(row=3, column=0, sticky="w")
        self.beat_indicator = ttk.Label(status_frame, text="(waiting)")
        self.beat_indicator.grid(row=3, column=1, sticky="w")

        led_frame = ttk.LabelFrame(self.root, text="Serial LED")
        led_frame.grid(row=2, column=0, sticky="ew", **padding)
        led_frame.columnconfigure(1, weight=1)

        self.rgb_vars: dict[str, tk.IntVar] = {
            "R": tk.IntVar(value=0),
            "G": tk.IntVar(value=0),
            "B": tk.IntVar(value=0),
        }
        self.rgb_sliders: dict[str, tk.Scale] = {}
        self.rgb_slider_colors: dict[str, dict[str, str]] = {
            "R": {"enabled": "#ffcccc", "disabled": "#ebebeb"},
            "G": {"enabled": "#ccffcc", "disabled": "#ebebeb"},
            "B": {"enabled": "#ccccff", "disabled": "#ebebeb"},
        }
        self.rgb_value_labels: dict[str, ttk.Label] = {}

        for idx, color in enumerate(("R", "G", "B")):
            ttk.Label(led_frame, text=f"{color}:").grid(row=idx, column=0, sticky="w")
            scale = tk.Scale(
                led_frame,
                from_=0,
                to=255,
                orient="horizontal",
                variable=self.rgb_vars[color],
                command=lambda val, c=color: self._on_slider_change(c, int(float(val))),
                showvalue=False,
                troughcolor=self.rgb_slider_colors[color]["enabled"],
                sliderrelief="raised",
            )
            scale.grid(row=idx, column=1, sticky="ew", padx=(8, 0))
            self.rgb_sliders[color] = scale
            val_label = ttk.Label(led_frame, text="0")
            val_label.grid(row=idx, column=2, sticky="e", padx=(8, 0))
            self.rgb_value_labels[color] = val_label

        self.send_btn = ttk.Button(led_frame, text="Send to COM port", command=self.send_rgb_to_serial)
        self.send_btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        mode_frame = ttk.LabelFrame(self.root, text="Mode")
        mode_frame.grid(row=3, column=0, sticky="ew", **padding)
        mode_options = [
            ("Light on", light_controller.LightMode.ON.value),
            ("Light off", light_controller.LightMode.OFF.value),
            ("Light fade sync", light_controller.LightMode.FADE_SYNC.value),
            ("Auto RGB fade (no beat)", light_controller.LightMode.AUTO_RGB_FADE.value),
            ("Beat RGB step", light_controller.LightMode.BEAT_RGB_STEP.value),
            ("Slider slow fade", light_controller.LightMode.SLIDER_SLOW_FADE.value),
            ("Beat every 4th", light_controller.LightMode.FADE_SYNC_EVERY_4.value),
            ("Strobe", light_controller.LightMode.STROBE.value),
        ]
        for idx, (label, mode) in enumerate(mode_options):
            row, col = divmod(idx, 3)
            ttk.Radiobutton(
                mode_frame,
                text=label,
                value=mode,
                variable=self.mode_var,
                command=self._on_mode_change,
            ).grid(row=row, column=col, sticky="w", padx=(0, 8), pady=(0, 4))
        for col in range(3):
            mode_frame.columnconfigure(col, weight=1)

        fade_frame = ttk.LabelFrame(self.root, text="Fade adjustments")
        fade_frame.grid(row=4, column=0, sticky="ew", **padding)
        ttk.Label(fade_frame, text="Decay (ms)").grid(row=0, column=0, sticky="w")
        self.decay_label = ttk.Label(fade_frame, text=f"{int(self.decay_var.get())} ms")
        self.decay_label.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.decay_slider = tk.Scale(
            fade_frame,
            from_=0.0,
            to=5000.0,
            resolution=10.0,  # finer than 100ms
            orient="horizontal",
            variable=self.decay_var,
            command=lambda val: self._on_decay_change(float(val)),
            showvalue=False,
            troughcolor="#d4e8ff",
            sliderrelief="raised",
        )
        self.decay_slider.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        fade_frame.columnconfigure(1, weight=1)

        preview_frame = ttk.LabelFrame(self.root, text="RGB preview")
        preview_frame.grid(row=5, column=0, sticky="ew", **padding)
        for idx, color in enumerate(("R", "G", "B")):
            ttk.Label(preview_frame, text=f"{color}:").grid(row=0, column=idx * 2, sticky="e", padx=(0, 4))
            label = ttk.Label(preview_frame, text="0")
            label.grid(row=0, column=idx * 2 + 1, sticky="w")
            self.rgb_value_labels[f"preview_{color}"] = label

        visual_frame = ttk.LabelFrame(self.root, text="Lamp visualizer (mirrors serial)")
        visual_frame.grid(row=6, column=0, sticky="ew", **padding)
        visual_frame.columnconfigure(1, weight=1)

        self.visual_canvas = tk.Canvas(visual_frame, width=160, height=80, highlightthickness=1, highlightbackground="#888")
        self.visual_rect = self.visual_canvas.create_rectangle(10, 10, 150, 70, outline="#444", fill="#000000")
        self.visual_canvas.grid(row=0, column=0, rowspan=2, sticky="w")

        self.visual_rgb_label = ttk.Label(visual_frame, text="Lamp RGB: 0, 0, 0")
        self.visual_rgb_label.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.visual_fade_label = ttk.Label(visual_frame, text="Fade in/out: -- / -- ms")
        self.visual_fade_label.grid(row=1, column=1, sticky="w", padx=(10, 0))

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(self.root, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=7, column=0, sticky="ew", **padding)

        # Initialize controller decay with slider value
        self._on_decay_change(self.decay_var.get())
        self._update_slider_states_for_mode(light_controller.LightMode.OFF)

    def refresh_ports(self) -> None:
        midi_ports = ["<none>"] + mido.get_input_names()
        com_ports = ["<none>"] + [p.device for p in serial.tools.list_ports.comports()]

        self.midi_combo["values"] = midi_ports
        self.com_combo["values"] = com_ports

        self.midi_combo.set("<none>")
        self.com_combo.set("<none>")

    def start_listening(self) -> None:
        if self.running:
            self.stop_listening()
            return

        port_name = self.midi_combo.get()
        if not port_name or port_name.startswith("<none"):
            self.set_status("Select a MIDI port first.")
            return

        self.running = True
        self.decoder = mixxx_listener.MixxxLightDecoder()
        self.last_beat_time = None
        self.listen_thread = threading.Thread(target=self._listen_loop, args=(port_name,), daemon=True)
        self.listen_thread.start()
        self.update_start_button()
        self.set_status(f"Listening on {port_name}")

    def stop_listening(self) -> None:
        self.running = False
        self.update_start_button()
        self.set_status("Stopped")

    def _listen_loop(self, port_name: str) -> None:
        try:
            with mido.open_input(port_name) as port:
                while self.running:
                    for msg in port.iter_pending():
                        is_beat = msg.type == "note_on" and msg.note == mixxx_listener.NOTE_BEAT
                        beat_bpm_hint: Optional[float] = None
                        self.decoder.handle(msg)
                        with self.state_lock:
                            st = self.decoder.state
                            self.state_snapshot = {
                                "reported_bpm": st.reported_bpm,
                                "calculated_bpm": st.calculated_bpm,
                                "deck": st.current_deck,
                            }
                            if is_beat:
                                self.last_beat_time = time.time()
                                beat_bpm_hint = st.reported_bpm or st.calculated_bpm
                        if is_beat:
                            try:
                                self.light_controller.handle_beat(beat_bpm_hint)
                            except Exception as exc:
                                self.root.after(0, lambda msg=f"Serial error: {exc}": self.set_status(msg))
                    time.sleep(0.01)
        except Exception as exc:
            self.root.after(0, lambda: self.set_status(f"Error: {exc}"))
        finally:
            self.running = False
            self.root.after(0, self.update_start_button)

    def _poll_ui(self) -> None:
        with self.state_lock:
            snap = dict(self.state_snapshot)
            beat_time = self.last_beat_time

        

        now = time.time()

        if beat_time and (now - beat_time) < 1.00:
            self.beat_indicator.config(text=f"beat incoming", foreground="green")
            self.reported_bpm_label.config(self._fmt_value(snap.get("reported_bpm")))
            self.calculated_bpm_label.config(self._fmt_value(snap.get("calculated_bpm")))
            self.deck_label.config(text=str(snap.get("deck") or "--"))
        else:
            self.beat_indicator.config(text="no beat", foreground="red")
            self.reported_bpm_label.config(text="...", foreground="gray")
            self.calculated_bpm_label.config(text="...", foreground="gray")
            self.deck_label.config(text="...", foreground="gray")

        self._update_visualizer()
        self.root.after(33, self._poll_ui)

    def _fmt_value(self, value) -> dict:
        if value is None:
            return {"text": "--"}
        if isinstance(value, float):
            return {"text": f"{value:.1f}"}
        return {"text": str(value)}

    def _get_current_mode(self) -> Optional[light_controller.LightMode]:
        try:
            return light_controller.LightMode(self.mode_var.get())
        except ValueError:
            return None

    def _mode_uses_rgb(self, mode: light_controller.LightMode) -> bool:
        needs = MODE_SLIDER_REQUIREMENTS.get(mode)
        return bool(needs and needs.get("rgb"))

    def _mode_uses_decay(self, mode: light_controller.LightMode) -> bool:
        needs = MODE_SLIDER_REQUIREMENTS.get(mode)
        return bool(needs and needs.get("decay"))

    def _persist_rgb_if_needed(self) -> None:
        mode = self._get_current_mode()
        if mode and self._mode_uses_rgb(mode):
            self.settings_store.update(mode, rgb=self._get_rgb_tuple())

    def _persist_decay_if_needed(self, value: float) -> None:
        mode = self._get_current_mode()
        if mode and self._mode_uses_decay(mode):
            self.settings_store.update(mode, decay_ms=value)

    def _update_slider_states_for_mode(self, mode: light_controller.LightMode) -> None:
        uses_rgb = self._mode_uses_rgb(mode)
        uses_decay = self._mode_uses_decay(mode)

        rgb_state = tk.NORMAL if uses_rgb else tk.DISABLED
        for color, slider in self.rgb_sliders.items():
            slider.config(
                state=rgb_state,
                troughcolor=self.rgb_slider_colors[color]["enabled" if uses_rgb else "disabled"],
                sliderrelief="raised" if uses_rgb else "flat",
            )
        self.send_btn.config(state=rgb_state)

        decay_state = tk.NORMAL if uses_decay else tk.DISABLED
        self.decay_slider.config(
            state=decay_state,
            troughcolor="#d4e8ff" if uses_decay else "#ebebeb",
            sliderrelief="raised" if uses_decay else "flat",
        )

    def _apply_saved_settings_for_mode(self, mode: light_controller.LightMode) -> None:
        settings = self.settings_store.get(mode)
        if self._mode_uses_rgb(mode):
            rgb = self._extract_rgb(settings.get("rgb"))
            self._set_rgb_sliders(rgb)
            self.settings_store.update(mode, rgb=rgb)
        if self._mode_uses_decay(mode):
            decay_ms = self._extract_decay_ms(settings.get("decay_ms"))
            self._set_decay_slider(decay_ms)
            self.settings_store.update(mode, decay_ms=decay_ms)

    def _set_rgb_sliders(self, rgb: tuple[int, int, int]) -> None:
        clamped = tuple(max(0, min(int(v), 255)) for v in rgb)
        self._suppress_slider_events = True
        try:
            for color, val in zip(("R", "G", "B"), clamped):
                self.rgb_vars[color].set(val)
                if color in self.rgb_value_labels:
                    self.rgb_value_labels[color].config(text=str(val))
                preview_key = f"preview_{color}"
                if preview_key in self.rgb_value_labels:
                    self.rgb_value_labels[preview_key].config(text=str(val))
        finally:
            self._suppress_slider_events = False
        self._update_controller_color()

    def _set_decay_slider(self, value: float) -> None:
        val = max(0.0, min(float(value), 5000.0))
        self._suppress_decay_event = True
        try:
            self.decay_var.set(val)
            self.decay_label.config(text=f"{int(val)} ms")
        finally:
            self._suppress_decay_event = False
        try:
            self.light_controller.set_decay_ms(val)
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")

    def _extract_rgb(self, value: object) -> tuple[int, int, int]:
        if isinstance(value, (list, tuple)) and len(value) == 3:
            try:
                r, g, b = (int(value[0]), int(value[1]), int(value[2]))
                return (
                    max(0, min(r, 255)),
                    max(0, min(g, 255)),
                    max(0, min(b, 255)),
                )
            except (TypeError, ValueError):
                pass
        return DEFAULT_RGB

    def _extract_decay_ms(self, value: object) -> float:
        try:
            return max(0.0, min(float(value), 5000.0))
        except (TypeError, ValueError):
            return DEFAULT_DECAY_MS

    def _on_slider_change(self, color: str, value: int) -> None:
        if self._suppress_slider_events:
            return
        # Keep labels in sync for both side columns.
        clamped = max(0, min(int(value), 255))
        self.rgb_vars[color].set(clamped)
        if color in self.rgb_value_labels:
            self.rgb_value_labels[color].config(text=str(clamped))
        preview_key = f"preview_{color}"
        if preview_key in self.rgb_value_labels:
            self.rgb_value_labels[preview_key].config(text=str(clamped))
        self._update_controller_color()
        self._persist_rgb_if_needed()

    def _get_rgb_tuple(self) -> tuple[int, int, int]:
        return (
            self.rgb_vars["R"].get(),
            self.rgb_vars["G"].get(),
            self.rgb_vars["B"].get(),
        )

    def _get_selected_com_port(self) -> Optional[str]:
        port = self.com_combo.get()
        if not port or port.startswith("<none"):
            return None
        return port

    def _sync_com_port(self) -> bool:
        port = self._get_selected_com_port()
        if not port:
            self.set_status("Select a COM port first.")
            return False
        try:
            self.light_controller.set_com_port(port)
            return True
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")
            return False

    def _update_controller_color(self) -> None:
        try:
            self.light_controller.set_color(self._get_rgb_tuple())
        except Exception as exc:
            if self.mode_var.get() == light_controller.LightMode.ON.value:
                self.set_status(f"Serial error: {exc}")

    def _on_mode_change(self) -> None:
        try:
            mode = light_controller.LightMode(self.mode_var.get())
        except ValueError:
            return

        if mode != light_controller.LightMode.OFF and not self._sync_com_port():
            # Revert to off if we cannot talk to the lamp.
            self.mode_var.set(light_controller.LightMode.OFF.value)
            self._update_slider_states_for_mode(light_controller.LightMode.OFF)
            return

        self._update_slider_states_for_mode(mode)
        self._apply_saved_settings_for_mode(mode)

        try:
            self.light_controller.set_mode(mode)
        except Exception as exc:
            self.mode_var.set(light_controller.LightMode.OFF.value)
            self._update_slider_states_for_mode(light_controller.LightMode.OFF)
            self.set_status(f"Serial error: {exc}")
            return

        if mode == light_controller.LightMode.OFF:
            self.set_status("Light off")
        elif mode == light_controller.LightMode.ON:
            self.set_status("Light on (static)")
        elif mode == light_controller.LightMode.FADE_SYNC:
            self.set_status("Light fade sync on beat")
        elif mode == light_controller.LightMode.AUTO_RGB_FADE:
            self.set_status("Auto RGB fade cycling (no beat)")
        elif mode == light_controller.LightMode.BEAT_RGB_STEP:
            self.set_status("Beat-driven RGB sequence (R → G → B)")
        elif mode == light_controller.LightMode.SLIDER_SLOW_FADE:
            self.set_status("Slider color slow fade (no beat)")
        elif mode == light_controller.LightMode.FADE_SYNC_EVERY_4:
            self.set_status("Beat fade every 4th beat")
        elif mode == light_controller.LightMode.STROBE:
            self.set_status("Strobe: rapid flashes (0ms in / 20ms out)")

    def _on_decay_change(self, value: float) -> None:
        if self._suppress_decay_event:
            return
        val = max(0.0, min(float(value), 5000.0))
        self.decay_var.set(val)
        self.decay_label.config(text=f"{int(val)} ms")
        try:
            self.light_controller.set_decay_ms(val)
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")
            return
        self._persist_decay_if_needed(val)

    def _on_com_port_change(self, _event=None) -> None:
        port = self._get_selected_com_port()
        if not port:
            self.set_status("Select a COM port to control the light.")
            return
        try:
            self.light_controller.set_com_port(port)
            self.set_status(f"Using COM port {port}")
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")

    def send_rgb_to_serial(self) -> None:
        if not self._sync_com_port():
            return

        r, g, b = self._get_rgb_tuple()
        try:
            self.light_controller.set_color((r, g, b))
            self.light_controller.send_static_color()
            self.set_status(f"Sent RGB {r},{g},{b}")
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")

    def update_start_button(self) -> None:
        if self.running:
            self.start_btn.config(text="Stop listening")
        else:
            self.start_btn.config(text="Start listening")

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def on_close(self) -> None:
        self.running = False
        if self.listen_thread and self.listen_thread.is_alive():
            self.listen_thread.join(timeout=1.0)
        try:
            self.light_controller.close()
        except Exception:
            pass
        self.root.destroy()

    def _update_visualizer(self) -> None:
        rgb, last_cmd = self.lamp_mirror.tick()
        if rgb != self._visual_last_rgb:
            fill = "#%02x%02x%02x" % rgb
            self.visual_canvas.itemconfig(self.visual_rect, fill=fill)
            self.visual_rgb_label.config(text=f"Lamp RGB: {rgb[0]}, {rgb[1]}, {rgb[2]}")
            self._visual_last_rgb = rgb

        fade_in = last_cmd.get("fade_in_ms", "--")
        fade_out = last_cmd.get("fade_out_ms", "--")
        state = last_cmd.get("state", "rgb")
        self.visual_fade_label.config(text=f"Fade in/out: {fade_in} / {fade_out} ms (last: {state})")


def main() -> None:
    root = tk.Tk()
    MixxxGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
