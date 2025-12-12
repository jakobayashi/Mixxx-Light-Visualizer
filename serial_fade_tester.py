"""
Standalone serial fade tester for the new `RGB r g b fadeIn fadeOut` command.

Sends a small suite of fade commands (with different channels/timings) so you
can visually verify on-lamp fading without rapid-fire serial writes.
"""
from __future__ import annotations

import time
from typing import Iterable, Sequence, Tuple

import serial

# Configure your COM port here.
COM_PORT = "COM3"
BAUDRATE = 115200

# Send every command once per second, regardless of fade length.
SEND_INTERVAL_SEC = 1.0

# (label, (r, g, b, fadeInMs, fadeOutMs))
# Values are milliseconds for fade in/out.
TEST_COMMANDS: Sequence[Tuple[str, Tuple[int, int, int, int, int]]] = (
    ("White breath 1s/1s", (255, 255, 255, 1000, 1000)),
    ("Red pop then 2s fade", (255, 0, 0, 0, 2000)),
    ("Green long 3s/3s", (0, 255, 0, 3000, 3000)),
    ("Blue quick 0/500ms", (0, 0, 255, 0, 500)),
    # Overlap demo: send green then red one second later (both 2s/2s) so they mix to yellow.
    ("Overlap part 1: green 2s/2s", (0, 255, 0, 2000, 2000)),
    ("Overlap part 2: red 2s/2s", (255, 0, 0, 2000, 2000)),
)


def clamp_rgb(rgb: Iterable[int]) -> Tuple[int, int, int]:
    values = tuple(max(0, min(int(v), 255)) for v in rgb)
    return values  # type: ignore


def send_fade(ser: serial.Serial, r: int, g: int, b: int, fade_in: int, fade_out: int) -> None:
    r, g, b = clamp_rgb((r, g, b))
    fade_in = max(0, int(fade_in))
    fade_out = max(0, int(fade_out))
    cmd = f"RGB {r} {g} {b} {fade_in} {fade_out}\n".encode("ascii")
    ser.write(cmd)


def main() -> None:
    ser = serial.Serial(COM_PORT, BAUDRATE, timeout=1, write_timeout=0.05)
    try:
        try:
            ser.dtr = False
        except Exception:
            pass

        print(f"Starting fade suite on {COM_PORT} @ {BAUDRATE}")
        while True:
            for label, params in TEST_COMMANDS:
                print(f"-> {label}: RGB {params}")
                try:
                    send_fade(ser, *params)
                except serial.SerialTimeoutException:
                    ser.reset_output_buffer()
                    send_fade(ser, *params)

                # Read any immediate ACK/ERR without blocking the fade.
                try:
                    ser.timeout = 0.05
                    line = ser.readline().decode("ascii", errors="ignore").strip()
                    if line:
                        print(f"   {line}")
                except Exception:
                    pass

                time.sleep(SEND_INTERVAL_SEC)

            print("Cycle complete; restarting...\n")

    finally:
        try:
            ser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
