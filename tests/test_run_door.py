import tempfile
import unittest
from datetime import time as dt_time
from pathlib import Path

import coop_door


class RunDoorConfigTests(unittest.TestCase):
    def test_config_without_run_door_section_disables_feature(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[location]\nlatitude = 40.0\n", encoding="utf-8")

            config = coop_door.load_config(path)

            self.assertIsNone(config.run_door)

    def test_run_door_section_uses_documented_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\n", encoding="utf-8")

            config = coop_door.load_config(path)

            self.assertIsNotNone(config.run_door)
            self.assertEqual(5, config.run_door.in1_pin)
            self.assertEqual(6, config.run_door.in2_pin)
            self.assertEqual(24, config.run_door.reed_closed_pin)
            self.assertEqual(75.0, config.run_door.travel_seconds)
            self.assertEqual(dt_time(7, 0), config.run_door.open_time)
            self.assertEqual(35, config.run_door.close_offset_minutes)

    def test_run_door_state_file_defaults_next_to_runtime_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "\n".join(
                    [
                        "[runtime]",
                        "state_file = /var/lib/coop-door/state.json",
                        "[run_door]",
                    ]
                ),
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual(
                Path("/var/lib/coop-door/run_door_state.json"),
                config.run_door.state_file,
            )

    def test_run_door_section_accepts_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text(
                "\n".join(
                    [
                        "[run_door]",
                        "in1_pin = 12",
                        "in2_pin = 13",
                        "reed_closed_pin = 25",
                        "travel_seconds = 60",
                        "open_time = 08:30",
                        "close_offset_minutes = 40",
                        "state_file = /tmp/run.json",
                    ]
                ),
                encoding="utf-8",
            )

            config = coop_door.load_config(path)

            self.assertEqual(12, config.run_door.in1_pin)
            self.assertEqual(13, config.run_door.in2_pin)
            self.assertEqual(25, config.run_door.reed_closed_pin)
            self.assertEqual(60.0, config.run_door.travel_seconds)
            self.assertEqual(dt_time(8, 30), config.run_door.open_time)
            self.assertEqual(40, config.run_door.close_offset_minutes)
            self.assertEqual(Path("/tmp/run.json"), config.run_door.state_file)

    def test_invalid_open_time_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coop_door.ini"
            path.write_text("[run_door]\nopen_time = seven\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                coop_door.load_config(path)


if __name__ == "__main__":
    unittest.main()
