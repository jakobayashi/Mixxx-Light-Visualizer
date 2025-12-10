"""
Basic CLI listener for Mixxx's "MIDI for light" mapping.

It opens a MIDI input port, decodes the key messages documented at:
mixxx.wiki/Midi-Clock-Output (cloned locally) and prints a concise log
of deck changes, beats, BPM updates, and optional VU meters.
"""
import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import mido

# Note numbers from the Mixxx "MIDI for light" mapping
NOTE_DECK_CHANGE = 0x30  # 48
NOTE_BEAT = 0x32         # 50
NOTE_BPM = 0x34          # 52

# A subset of the VU meter notes (others can be added if needed)
VU_NOTES = {
    0x40: "vu_mono_current",
    0x41: "vu_mono_average_min",
    0x42: "vu_mono_average_mid",
    0x43: "vu_mono_average_max",
    0x44: "vu_mono_average_fit",
    0x45: "vu_mono_current_meter1",
    0x46: "vu_mono_current_meter2",
    0x47: "vu_mono_current_meter3",
    0x48: "vu_mono_current_meter4",
    0x49: "vu_mono_average_meter1",
    0x4A: "vu_mono_average_meter2",
    0x4B: "vu_mono_average_meter3",
    0x4C: "vu_mono_average_meter4",
}


@dataclass
class MixxxState:
    """Tracks the latest decoded state."""

    current_deck: Optional[int] = None
    reported_bpm: Optional[float] = None
    last_beat_ts: Optional[float] = None
    calculated_bpm: Optional[float] = None
    vu_cache: dict = field(default_factory=dict)


class MixxxLightDecoder:
    """Decode incoming Mixxx MIDI-for-light messages into higher-level events."""

    def __init__(self) -> None:
        self.state = MixxxState()

    def handle(self, msg: mido.Message, show_vu: bool = False) -> None:
        # The mapping uses note_on events for everything except MTC (not handled here).
        if msg.type != "note_on":
            return

        note = msg.note
        velocity = msg.velocity
        channel = msg.channel + 1  # mido is 0-based; mapping docs are 1-based

        if note == NOTE_DECK_CHANGE:
            self._handle_deck_change(velocity, channel)
        elif note == NOTE_BEAT:
            self._handle_beat(channel)
        elif note == NOTE_BPM:
            self._handle_bpm(velocity, channel)
        elif show_vu and note in VU_NOTES:
            self._handle_vu(note, velocity, channel)

    def _handle_deck_change(self, velocity: int, channel: int) -> None:
        # Velocity is 100 + deck number in the official script.
        deck = max(velocity - 100, 0) or None
        if deck:
            self.state.current_deck = deck
            logging.info("Deck change -> deck %s (channel %s)", deck, channel)

    def _handle_beat(self, channel: int) -> None:
        now = time.time()
        if self.state.last_beat_ts:
            interval = now - self.state.last_beat_ts
            # drop implausibly short intervals (< 0.2s => >300 BPM)
            if interval < 0.2:
                return
            self.state.calculated_bpm = 60.0 / interval
        self.state.last_beat_ts = now
        logging.info("Beat (deck=%s, calc_bpm=%s, reported_bpm=%s)",
                    self.state.current_deck or "?",
                    f"{self.state.calculated_bpm:.1f}" if self.state.calculated_bpm else "n/a",
                    f"{self.state.reported_bpm:.1f}" if self.state.reported_bpm else "n/a")


    def _handle_bpm(self, velocity: int, channel: int) -> None:
        # The script sends BPM every beat; value is (BPM - 50) clamped to 0..127.
        self.state.reported_bpm = clamp_bpm_from_velocity(velocity)
        # No logging here; BPM is reported alongside the beat log to avoid double lines.

    def _handle_vu(self, note: int, velocity: int, channel: int) -> None:
        label = VU_NOTES[note]
        self.state.vu_cache[label] = velocity
        logging.debug("VU %s = %d (channel %s)", label, velocity, channel)


def clamp_bpm_from_velocity(velocity: int) -> float:
    """Convert velocity back to BPM according to the mapping rules."""
    # In the script: send_value = clamp(BPM - 50, 0, 127)
    # This reverse assumes the original BPM was within the supported range.
    return float(max(0, min(velocity, 127)) + 50)


def list_input_ports() -> None:
    ports = mido.get_input_names()
    if not ports:
        print("No MIDI input ports found.")
        return
    print("Available MIDI input ports:")
    for idx, name in enumerate(ports):
        print(f"[{idx}] {name}")


def guess_mixxx_port() -> Optional[str]:
    """Try to find a Mixxx MIDI-for-light port heuristically."""
    for name in mido.get_input_names():
        name_lower = name.lower()
        if "mixxx" in name_lower or "light" in name_lower:
            return name
    return None


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Decode Mixxx 'MIDI for light' output (beat/BPM/deck/VU)."
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List available MIDI input ports")

    listen = sub.add_parser("listen", help="Listen to a Mixxx MIDI for light port")
    listen.add_argument(
        "--port",
        help="Exact MIDI input port name (otherwise tries to guess Mixxx/light port)",
    )
    listen.add_argument(
        "--show-vu",
        action="store_true",
        help="Also log VU meter messages (more verbose, debug level recommended)",
    )
    listen.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging (for VU meter spam)",
    )

    parser.set_defaults(command="listen")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)

    if args.command == "list":
        list_input_ports()
        return 0

    level = logging.DEBUG if getattr(args, "debug", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    port_name = getattr(args, "port", None) or guess_mixxx_port()
    if not port_name:
        logging.error("No port specified and no Mixxx/light port found. Use --port.")
        return 1

    logging.info("Opening MIDI input: %s", port_name)
    decoder = MixxxLightDecoder()

    try:
        with mido.open_input(port_name) as port:
            logging.info("Listening... Ctrl+C to stop.")
            for message in port:
                decoder.handle(message, show_vu=getattr(args, "show_vu", False))
    except KeyboardInterrupt:
        logging.info("Stopped.")
    except Exception as exc:  # pragma: no cover - defensive print for CLI
        logging.error("Error while listening: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
