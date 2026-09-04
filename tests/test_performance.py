import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_collector import load_collector, write_record


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "hermes_bot_status.py"


def process_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    return int(fields[13]) + int(fields[14])


def process_status(pid: int) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            values["rss_kib"] = int(line.split()[1])
        elif line.startswith("voluntary_ctxt_switches:"):
            values["voluntary"] = int(line.split()[1])
        elif line.startswith("nonvoluntary_ctxt_switches:"):
            values["involuntary"] = int(line.split()[1])
    return values


class CollectorPerformanceTests(unittest.TestCase):
    def test_representative_snapshot_workloads_meet_latency_and_payload_budgets(self):
        collector = load_collector()
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": str(Path(tmp) / "state"),
                "HERMES_ROOT": str(Path(tmp) / "hermes"),
            },
        ):
            state_root = Path(tmp) / "state/omarchy/hermes-bots"
            hermes_root = Path(tmp) / "hermes"
            (hermes_root / "profiles").mkdir(parents=True)
            cache = collector.SnapshotCache()

            def measure(name: str, *, online_sessions=None):
                started = time.perf_counter()
                snapshot = collector.build_snapshot(
                    state_root,
                    now=200,
                    online_sessions=online_sessions or [],
                    process_alive=lambda _pid, _start: True,
                    cache=cache,
                )
                elapsed = time.perf_counter() - started
                payload = collector.serialize_snapshot(snapshot).encode("utf-8")
                with self.subTest(name=name):
                    self.assertLessEqual(elapsed, 0.25)
                    self.assertLessEqual(len(payload), collector.MAX_SNAPSHOT_BYTES)

            measure("no-sessions")
            idle = [{"profile": "default", "pid": 10, "processStart": "20", "runningForSec": 10}]
            measure("one-idle-session", online_sessions=idle)
            write_record(state_root, "default", "active")
            measure("one-active-session", online_sessions=idle)
            for index in range(1, 6):
                write_record(state_root, "default", f"active-{index}", writerPid=10 + index)
            measure(
                "multiple-sessions-one-profile",
                online_sessions=[
                    {"profile": "default", "pid": 10 + index, "processStart": "20", "runningForSec": 10}
                    for index in range(6)
                ],
            )
            for index in range(20):
                (hermes_root / "profiles" / f"profile_{index}").mkdir()
            measure("many-profiles")
            for index in range(100):
                write_record(
                    state_root,
                    "default",
                    f"terminal-{index}",
                    state="succeeded",
                    finishedAt=150 + index / 1000,
                    durationSec=50,
                )
            measure("one-hundred-retained-events")
            measure("notification-backlog")

    def test_idle_persistent_collector_meets_resource_and_wakeup_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_root = Path(tmp) / "state/omarchy/hermes-bots"
            state_root.mkdir(parents=True)
            hermes_root = Path(tmp) / "hermes"
            hermes_root.mkdir()
            env = dict(
                os.environ,
                XDG_STATE_HOME=str(Path(tmp) / "state"),
                HERMES_ROOT=str(hermes_root),
                PYTHONDONTWRITEBYTECODE="1",
            )
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    str(SCRIPT),
                    "watch",
                    "--health-scan-sec",
                    "30",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self.assertIsNotNone(process.stdout)
            stdout = process.stdout
            assert stdout is not None
            try:
                ready, _, _ = select.select([stdout], [], [], 3)
                self.assertTrue(ready, "collector did not emit its initial snapshot")
                self.assertEqual(json.loads(stdout.readline())["schemaVersion"], 1)
                ticks_per_second = os.sysconf("SC_CLK_TCK")
                start_ticks = process_ticks(process.pid)
                start_status = process_status(process.pid)
                started = time.monotonic()
                time.sleep(4)
                elapsed = time.monotonic() - started
                end_ticks = process_ticks(process.pid)
                end_status = process_status(process.pid)
                cpu_percent = ((end_ticks - start_ticks) / ticks_per_second) / elapsed * 100
                context_switches = (
                    end_status.get("voluntary", 0)
                    + end_status.get("involuntary", 0)
                    - start_status.get("voluntary", 0)
                    - start_status.get("involuntary", 0)
                )
                children_path = Path(f"/proc/{process.pid}/task/{process.pid}/children")
                children = children_path.read_text().strip() if children_path.exists() else ""

                self.assertLessEqual(cpu_percent, 0.25)
                self.assertLessEqual(context_switches, 4)
                self.assertLessEqual(end_status.get("rss_kib", 0), 64 * 1024)
                self.assertEqual(children, "")
            finally:
                process.terminate()
                process.communicate(timeout=2)


if __name__ == "__main__":
    unittest.main()
