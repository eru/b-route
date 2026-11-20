import json
import subprocess
import tempfile
import time
import unittest
from configparser import ConfigParser
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from b_route.sensor import (
    BRouteError,
    BRouteTimeoutError,
    connect_pana,
    fetch_wattage,
    run_daemon,
    scan,
    write_status,
)


def _make_fake_serial(lines: list[bytes]) -> MagicMock:
    ser = MagicMock()
    iterator = iter(lines)

    def readline() -> bytes:
        try:
            return next(iterator)
        except StopIteration:
            return b""

    ser.readline.side_effect = readline
    return ser


class SensorTestCase(unittest.TestCase):
    def test_scan_times_out_when_no_response(self) -> None:
        ser = _make_fake_serial([])
        with self.assertRaises(BRouteTimeoutError):
            scan(ser, timeout_per_duration=0.01)

    def test_connect_pana_times_out_without_event_25(self) -> None:
        ser = _make_fake_serial([b"OK\r\n", b"OK\r\n", b"OK\r\n"])
        with self.assertRaises(BRouteTimeoutError):
            connect_pana(ser, "21", "1234", "FE80:0000:0000:0000:0000:0000:0000:0001", timeout=0.02)

    def test_connect_pana_raises_on_event_24(self) -> None:
        ser = _make_fake_serial([b"OK\r\n", b"OK\r\n", b"OK\r\n", b"EVENT 24 FE80::1 0\r\n"])
        with self.assertRaises(BRouteError):
            connect_pana(ser, "21", "1234", "FE80:0000:0000:0000:0000:0000:0000:0001", timeout=1.0)

    def test_fetch_wattage_parses_erxudp(self) -> None:
        # 450 W = 0x000001C2 (SEOJ=028801, DEOJ=05FF01, ESV=72, OPC=01, EPC=E7, PDC=04)
        payload = "1081000102880105FF017201E704000001C2"
        erxudp = f"ERXUDP FE80::1 FE80::2 0E1A 0E1A 001C640000000000 0 {len(payload) // 2:04X} {payload}\r\n"
        ser = _make_fake_serial([b"OK\r\n", erxudp.encode("ascii")])
        wattage = fetch_wattage(ser, "FE80::1", timeout=1.0, max_attempts=1)
        self.assertEqual(wattage, 450)

    def test_fetch_wattage_raises_on_event_29(self) -> None:
        ser = _make_fake_serial([b"OK\r\n", b"EVENT 29 FE80::1 0\r\n"])
        with self.assertRaises(BRouteError):
            fetch_wattage(ser, "FE80::1", timeout=1.0, max_attempts=2)

    def test_fetch_wattage_times_out_with_bounded_attempts(self) -> None:
        ser = _make_fake_serial([])
        with self.assertRaises(BRouteTimeoutError):
            fetch_wattage(ser, "FE80::1", timeout=0.01, max_attempts=2)

    def test_write_status_atomic_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "sub" / "wattage.json"
            write_status(output_path, 512, updated_at=1700000000)
            data = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(data, {"wattage": 512, "updated_at": 1700000000})

    def test_run_daemon_closes_serial_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.ini"
            output_path = Path(tmpdir) / "wattage.json"

            cfg = ConfigParser()
            cfg["device"] = {"port": "/dev/ttyUSB0"}
            cfg["auth"] = {"broute_id": "ID", "broute_pw": "PW"}
            cfg["connection"] = {"channel": "21", "pan_id": "1234", "address": "FE80::1"}
            with config_path.open("w", encoding="utf-8") as f:
                cfg.write(f)

            stop_event = Event()
            fake_ser = MagicMock()

            def fake_fetch(_ser: MagicMock, _addr: str) -> int:
                stop_event.set()
                return 321

            with (
                patch("b_route.sensor.Serial", return_value=fake_ser),
                patch("b_route.sensor.initialize_auth"),
                patch("b_route.sensor.connect_pana"),
                patch("b_route.sensor.fetch_wattage", side_effect=fake_fetch),
            ):
                run_daemon(config_path=config_path, output_path=output_path, interval=60.0, stop_event=stop_event)

            fake_ser.close.assert_called_once()
            data = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(data["wattage"], 321)

    def test_munin_plugin_reads_fresh_and_stale_status(self) -> None:
        plugin_path = Path(__file__).resolve().parent.parent / "munin" / "b_route_wattage"
        with tempfile.TemporaryDirectory() as tmpdir:
            status_path = Path(tmpdir) / "wattage.json"

            # Fresh
            write_status(status_path, 777, updated_at=time.time())
            res_fresh = subprocess.run(
                [str(plugin_path)],
                env={"PATH": "/usr/bin:/bin", "MUNIN_LIBDIR": "/usr/share/munin", "status_file": str(status_path)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_fresh.returncode, 0)
            self.assertEqual(res_fresh.stdout.strip(), "b_route.value 777")

            # Stale
            write_status(status_path, 777, updated_at=time.time() - 600)
            res_stale = subprocess.run(
                [str(plugin_path)],
                env={"PATH": "/usr/bin:/bin", "MUNIN_LIBDIR": "/usr/share/munin", "status_file": str(status_path)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res_stale.returncode, 0)
            self.assertEqual(res_stale.stdout.strip(), "b_route.value U")
