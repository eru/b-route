import argparse
import json
import logging
import os
import signal
import tempfile
import time
from configparser import ConfigParser, NoOptionError, NoSectionError
from pathlib import Path
from threading import Event
from types import FrameType

from serial import Serial, SerialException

logger = logging.getLogger("b_route")

DEFAULT_CONFIG_PATH = Path("./config.ini")
DEFAULT_OUTPUT_PATH = Path("/run/b-route/wattage.json")
DEFAULT_INTERVAL = 60.0

SERIAL_BAUDRATE = 115200
SERIAL_TIMEOUT = 2.0
COMMAND_TIMEOUT = 5.0
SCAN_TIMEOUT_PER_DURATION = 15.0
JOIN_TIMEOUT = 30.0
FETCH_TIMEOUT = 10.0
FETCH_MAX_ATTEMPTS = 3

ECHONET_WATTAGE_FRAME = b"\x10\x81\x00\x01\x05\xff\x01\x02\x88\x01\x62\x01\xe7\x00"


class BRouteError(Exception):
    """Base exception for B-route communication errors."""


class BRouteTimeoutError(BRouteError):
    """Raised when a Wi-SUN command or response times out."""


def _read_line(ser: Serial) -> str | None:
    """Read a single line from serial, returning None on read timeout."""
    raw = ser.readline()
    if not raw:
        return None
    return raw.decode(encoding="utf-8", errors="replace").rstrip("\r\n")


def _send_command(ser: Serial, command: bytes, timeout: float = COMMAND_TIMEOUT) -> list[str]:
    """Send an AT command and wait for OK (or raise on FAIL/timeout)."""
    ser.reset_input_buffer()
    ser.write(command)
    deadline = time.monotonic() + timeout
    lines: list[str] = []

    while time.monotonic() < deadline:
        line = _read_line(ser)
        if line is None:
            continue
        if line == "OK":
            return lines
        if line.startswith("FAIL"):
            raise BRouteError(f"Command failed: {line}")
        lines.append(line)

    raise BRouteTimeoutError(f"Timed out waiting for OK after {timeout}s")


def scan(ser: Serial, timeout_per_duration: float = SCAN_TIMEOUT_PER_DURATION) -> tuple[str, str, str]:
    """Scan for the smart meter and resolve its IPv6 link-local address."""
    res: dict[str, str] = {}
    required_keys = ("Channel", "Pan ID", "Addr")

    for duration in range(4, 8):
        res.clear()
        command = f"SKSCAN 2 FFFFFFFF {duration}\r\n".encode("ascii")
        ser.reset_input_buffer()
        ser.write(command)
        deadline = time.monotonic() + timeout_per_duration

        while time.monotonic() < deadline:
            line = _read_line(ser)
            if line is None:
                continue
            if line.startswith("FAIL"):
                raise BRouteError(f"SKSCAN failed: {line}")
            if line.startswith("EVENT 22"):
                break
            if line.startswith("  ") and ":" in line:
                key, value = line.strip().split(":", 1)
                res[key] = value

        if all(key in res for key in required_keys):
            break
    else:
        raise BRouteTimeoutError("SKSCAN did not find smart meter within max duration")

    ser.reset_input_buffer()
    ser.write(f"SKLL64 {res['Addr']}\r\n".encode("ascii"))
    deadline = time.monotonic() + COMMAND_TIMEOUT
    address = ""
    while time.monotonic() < deadline:
        line = _read_line(ser)
        if line is None or not line:
            continue
        if line.startswith("SKLL64") or line == "OK":
            continue
        if line.startswith("FAIL"):
            raise BRouteError(f"SKLL64 failed: {line}")
        address = line.strip()
        break

    if not address:
        raise BRouteTimeoutError("Timed out waiting for SKLL64 address")

    return (res["Channel"], res["Pan ID"], address)


def save_config(inifile: ConfigParser, config_path: Path) -> None:
    """Persist configuration changes to disk."""
    with config_path.open("w", encoding="utf-8") as configfile:
        inifile.write(configfile)


def save_connection(inifile: ConfigParser, config_path: Path, channel: str, pan_id: str, address: str) -> None:
    """Save discovered Wi-SUN connection parameters."""
    if not inifile.has_section("connection"):
        inifile.add_section("connection")
    inifile.set("connection", "channel", channel)
    inifile.set("connection", "pan_id", pan_id)
    inifile.set("connection", "address", address)
    save_config(inifile, config_path)


def reset_connection(inifile: ConfigParser, config_path: Path) -> None:
    """Clear cached Wi-SUN connection parameters so the next cycle re-scans."""
    if not inifile.has_section("connection"):
        return
    removed = False
    for option in ("channel", "pan_id", "address"):
        if inifile.remove_option("connection", option):
            removed = True
    if removed:
        save_config(inifile, config_path)


def initialize_auth(ser: Serial, broute_id: str, broute_pw: str) -> None:
    """Configure B-route password and ID on the Wi-SUN module."""
    _send_command(ser, f"SKSETPWD C {broute_pw}\r\n".encode("ascii"))
    _send_command(ser, f"SKSETRBID {broute_id}\r\n".encode("ascii"))


def connect_pana(ser: Serial, channel: str, pan_id: str, address: str, timeout: float = JOIN_TIMEOUT) -> None:
    """Configure channel/PAN ID and establish a PANA session."""
    _send_command(ser, f"SKSREG S2 {channel}\r\n".encode("ascii"))
    _send_command(ser, f"SKSREG S3 {pan_id}\r\n".encode("ascii"))
    _send_command(ser, f"SKJOIN {address}\r\n".encode("ascii"))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = _read_line(ser)
        if line is None:
            continue
        if line.startswith("EVENT 24"):
            raise BRouteError("PANA connection failed (EVENT 24)")
        if line.startswith("EVENT 25"):
            return

    raise BRouteTimeoutError(f"Timed out waiting for PANA connection (EVENT 25) after {timeout}s")


def _parse_wattage_erxudp(line: str) -> int | None:
    """Parse instantaneous wattage from an ERXUDP line, or return None if unrelated."""
    parts = line.split(" ")
    if len(parts) < 9:
        return None
    payload = parts[8]
    if len(payload) < 36:
        return None
    seoj = payload[8:14]
    esv = payload[20:22]
    epc = payload[24:26]
    if seoj != "028801" or esv != "72" or epc != "E7":
        return None
    wattage_hex = payload[-8:]
    try:
        return int.from_bytes(bytes.fromhex(wattage_hex), byteorder="big", signed=True)
    except ValueError:
        return None


def fetch_wattage(
    ser: Serial,
    address: str,
    timeout: float = FETCH_TIMEOUT,
    max_attempts: int = FETCH_MAX_ATTEMPTS,
) -> int:
    """Query instantaneous wattage (EPC 0xE7) with bounded retries and deadlines."""
    command = f"SKSENDTO 1 {address} 0E1A 1 {len(ECHONET_WATTAGE_FRAME):04X} ".encode("ascii") + ECHONET_WATTAGE_FRAME

    for attempt in range(1, max_attempts + 1):
        ser.reset_input_buffer()
        ser.write(command)
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            line = _read_line(ser)
            if line is None:
                continue
            if line.startswith("FAIL"):
                raise BRouteError(f"SKSENDTO failed: {line}")
            if line.startswith("EVENT 29"):
                raise BRouteError("PANA session terminated (EVENT 29)")
            if line.startswith("EVENT 21"):
                parts = line.split(" ")
                if len(parts) >= 5 and parts[4] != "00":
                    logger.warning("UDP send failed (%s), attempt %d/%d", line, attempt, max_attempts)
                    break
                continue
            if line.startswith("ERXUDP"):
                wattage = _parse_wattage_erxudp(line)
                if wattage is not None:
                    return wattage

        logger.warning("Timed out waiting for wattage response, attempt %d/%d", attempt, max_attempts)

    raise BRouteTimeoutError(f"Failed to fetch wattage after {max_attempts} attempts")


def write_status(output_path: Path, wattage: int, updated_at: float | None = None) -> None:
    """Atomically write the latest wattage reading and timestamp as JSON."""
    timestamp = int(time.time() if updated_at is None else updated_at)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"wattage": wattage, "updated_at": timestamp}) + "\n"

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=output_path.parent,
            prefix=".wattage.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp_file:
            tmp_path = Path(tmp_file.name)
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, output_path)
    except BaseException:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise


def load_config(config_path: Path) -> ConfigParser:
    """Load and validate config.ini."""
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    inifile = ConfigParser()
    inifile.read(config_path, encoding="utf-8")
    return inifile


def _get_connection_params(inifile: ConfigParser, config_path: Path, ser: Serial) -> tuple[str, str, str]:
    """Read cached connection params from config or scan if absent."""
    try:
        channel = inifile.get("connection", "channel")
        pan_id = inifile.get("connection", "pan_id")
        address = inifile.get("connection", "address")
        if channel and pan_id and address:
            return (channel, pan_id, address)
    except (NoSectionError, NoOptionError):
        pass

    logger.info("Scanning for smart meter...")
    channel, pan_id, address = scan(ser)
    save_connection(inifile, config_path, channel, pan_id, address)
    logger.info("Smart meter found: channel=%s pan_id=%s address=%s", channel, pan_id, address)
    return (channel, pan_id, address)


def run_daemon(
    config_path: Path,
    output_path: Path | None = None,
    interval: float | None = None,
    stop_event: Event | None = None,
) -> None:
    """Run the B-route polling daemon with automatic recovery."""
    if stop_event is None:
        stop_event = Event()

    inifile = load_config(config_path)
    port = inifile.get("device", "port")
    broute_id = inifile.get("auth", "broute_id")
    broute_pw = inifile.get("auth", "broute_pw")

    resolved_output = output_path or Path(
        inifile.get("daemon", "output_file", fallback=str(DEFAULT_OUTPUT_PATH)),
    )
    resolved_interval = (
        interval if interval is not None else inifile.getfloat("daemon", "interval", fallback=DEFAULT_INTERVAL)
    )

    consecutive_failures = 0

    while not stop_event.is_set():
        ser: Serial | None = None
        try:
            logger.info("Opening serial port %s", port)
            ser = Serial(port, SERIAL_BAUDRATE, timeout=SERIAL_TIMEOUT, write_timeout=SERIAL_TIMEOUT)
            initialize_auth(ser, broute_id, broute_pw)
            channel, pan_id, address = _get_connection_params(inifile, config_path, ser)
            connect_pana(ser, channel, pan_id, address)
            logger.info("PANA session established; polling every %.1fs -> %s", resolved_interval, resolved_output)

            while not stop_event.is_set():
                cycle_start = time.monotonic()
                wattage = fetch_wattage(ser, address)
                write_status(resolved_output, wattage)
                consecutive_failures = 0
                logger.info("Wattage: %d W", wattage)

                elapsed = time.monotonic() - cycle_start
                sleep_seconds = max(0.0, resolved_interval - elapsed)
                if stop_event.wait(timeout=sleep_seconds):
                    break

        except (BRouteError, SerialException, OSError) as exc:
            consecutive_failures += 1
            logger.error("Communication error (failure #%d): %s", consecutive_failures, exc)
            if consecutive_failures >= 2:
                logger.warning("Resetting cached connection parameters after repeated failures")
                reset_connection(inifile, config_path)
        finally:
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    logger.exception("Error while closing serial port")

        if not stop_event.is_set():
            backoff = min(60.0, 5.0 * (2 ** max(0, consecutive_failures - 1)))
            logger.info("Reconnecting in %.1fs...", backoff)
            stop_event.wait(timeout=backoff)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description="B-route instantaneous wattage daemon")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config.ini (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Path to output JSON status file (default: {DEFAULT_OUTPUT_PATH})",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help=f"Polling interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entrypoint for the B-route daemon."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    args = parse_args(argv)
    stop_event = Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received signal %d, shutting down...", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    run_daemon(
        config_path=args.config,
        output_path=args.output,
        interval=args.interval,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    main()
