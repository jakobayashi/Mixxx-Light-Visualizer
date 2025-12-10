"""Simple Tkinter GUI for the Mixxx light listener.

It lets you pick a MIDI input port and a serial COM port, then shows the
incoming beat/BPM information decoded by mixxx_listener.MixxxLightDecoder.
"""
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

import mido
import serial
import serial.tools.list_ports

import mixxx_listener


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
        self.serial_conn: Optional[serial.Serial] = None

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
            )
            scale.grid(row=idx, column=1, sticky="ew", padx=(8, 0))
            val_label = ttk.Label(led_frame, text="0")
            val_label.grid(row=idx, column=2, sticky="e", padx=(8, 0))
            self.rgb_value_labels[color] = val_label

        self.send_btn = ttk.Button(led_frame, text="Send to COM port", command=self.send_rgb_to_serial)
        self.send_btn.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(6, 0))

        preview_frame = ttk.LabelFrame(self.root, text="RGB preview")
        preview_frame.grid(row=3, column=0, sticky="ew", **padding)
        for idx, color in enumerate(("R", "G", "B")):
            ttk.Label(preview_frame, text=f"{color}:").grid(row=0, column=idx * 2, sticky="e", padx=(0, 4))
            label = ttk.Label(preview_frame, text="0")
            label.grid(row=0, column=idx * 2 + 1, sticky="w")
            self.rgb_value_labels[f"preview_{color}"] = label

        self.status_var = tk.StringVar(value="Idle")
        self.status_label = ttk.Label(self.root, textvariable=self.status_var, anchor="w")
        self.status_label.grid(row=4, column=0, sticky="ew", **padding)

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

        self.root.after(120, self._poll_ui)

    def _fmt_value(self, value) -> dict:
        if value is None:
            return {"text": "--"}
        if isinstance(value, float):
            return {"text": f"{value:.1f}"}
        return {"text": str(value)}

    def _on_slider_change(self, color: str, value: int) -> None:
        # Keep labels in sync for both side columns.
        clamped = max(0, min(int(value), 255))
        self.rgb_vars[color].set(clamped)
        self.rgb_value_labels[color].config(text=str(clamped))
        self.rgb_value_labels[f"preview_{color}"].config(text=str(clamped))

    def _get_rgb_tuple(self) -> tuple[int, int, int]:
        return (
            self.rgb_vars["R"].get(),
            self.rgb_vars["G"].get(),
            self.rgb_vars["B"].get(),
        )

    def _ensure_serial(self) -> bool:
        """Open (and keep) a serial connection for RGB sends."""
        if self.serial_conn and self.serial_conn.is_open:
            return True

        com_port = self.com_combo.get()
        if not com_port or com_port.startswith("<none>"):
            self.set_status("Select a COM port first.")
            return False

        try:
            ser = serial.Serial(com_port, 115200, timeout=1)
            # Avoid toggling DTR after open; some boards reset on DTR changes.
            try:
                ser.dtr = False
            except Exception:
                pass
            self.serial_conn = ser
            self.set_status(f"Connected to {com_port}")
            return True
        except Exception as exc:
            self.set_status(f"Serial error: {exc}")
            self.serial_conn = None
            return False

    def send_rgb_to_serial(self) -> None:
        if not self._ensure_serial():
            return

        r, g, b = self._get_rgb_tuple()
        cmd = f"RGB {r} {g} {b}\n".encode("ascii")
        try:
            assert self.serial_conn is not None
            self.serial_conn.write(cmd)
            self.serial_conn.flush()
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
        if self.serial_conn and self.serial_conn.is_open:
            try:
                self.serial_conn.close()
            except Exception:
                pass
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    MixxxGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
