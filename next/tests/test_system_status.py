"""Read-only resource parsing, failure isolation and caching."""
import subprocess
import unittest
from unittest.mock import patch
from helpers import TempEnv
from cutecat.system_status import SystemStatus, parse_cpu_stat, parse_memory, parse_nvidia
from cutecat.decoders import parse_decoders, decoder_inventory


class SystemTests(unittest.TestCase):
    def setUp(self):
        self.env = TempEnv()
        self.collector = SystemStatus(self.env.config())

    def tearDown(self):
        self.env.cleanup()

    def test_cpu_guest_not_double_counted(self):
        self.assertEqual(parse_cpu_stat("cpu 10 2 3 20 5 0 0 0 9 2\n"), (40, 25))

    def test_cpu_delta_and_reset(self):
        with patch("cutecat.system_status._text", side_effect=lambda p: "cpu 10 0 0 10\n" if p == "/proc/stat" else "model name: CPU" ):
            self.assertIsNone(self.collector._cpu()["utilization_percent"])
        with patch("cutecat.system_status._text", side_effect=lambda p: "cpu 20 0 0 20\n" if p == "/proc/stat" else "model name: CPU"):
            self.assertEqual(self.collector._cpu()["utilization_percent"], 50)
        with patch("cutecat.system_status._text", side_effect=lambda p: "cpu 1 0 0 1\n" if p == "/proc/stat" else "model name: CPU"):
            self.assertIsNone(self.collector._cpu()["utilization_percent"])

    def test_memory_available_not_free(self):
        result = parse_memory("MemTotal: 100 kB\nMemAvailable: 60 kB\nMemFree: 10 kB")
        self.assertEqual(result["used_bytes"], 40 * 1024)
        with self.assertRaises(ValueError):
            parse_memory("MemTotal: 100 kB\nMemFree: 10 kB")

    def test_nvidia_multi_card_and_na(self):
        rows = parse_nvidia("GPU A, 1, 2048, 123, [N/A]\nGPU B, 2, 4096, 0, 0\n")
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["utilization_percent"])
        self.assertEqual(rows[1]["memory_used_bytes"], 0)
        self.assertEqual(rows[1]["utilization_percent"], 0)
        with self.assertRaises(ValueError):
            parse_nvidia("bad")

    def test_snapshot_partial_failure_and_cache(self):
        with patch.object(self.collector, "_cpu", side_effect=PermissionError), patch.object(self.collector, "_gpu", return_value={"devices": []}) as spy:
            first = self.collector.snapshot()
            second = self.collector.snapshot()
        self.assertIs(first, second)
        self.assertEqual(first["cpu"]["status"], "unknown")
        self.assertEqual(first["memory"]["status"], "ok")
        self.assertEqual(spy.call_count, 1)

    def test_missing_mount_does_not_read_underlying_disk(self):
        from dataclasses import replace
        cfg = self.env.config()
        root = replace(cfg.storage_roots[0], mount_marker="missing")
        collector = SystemStatus(replace(cfg, storage_roots=(root,)))
        calls = []
        real = __import__("shutil").disk_usage
        with patch("cutecat.system_status.shutil.disk_usage", side_effect=lambda path: (calls.append(path), real(path))[1]):
            rows = collector._disks()["items"]
        self.assertNotIn(root.path, calls)
        self.assertEqual(rows[-1]["status"], "unknown")

    def test_same_disk_paths_report_same_filesystem(self):
        rows = self.collector._disks()["items"]
        ready = [row for row in rows if row["status"] == "ok"]
        self.assertEqual(len(ready), 4)
        self.assertEqual(rows[-1]["filesystem_id"], rows[-2]["filesystem_id"])
        self.assertTrue(all(row["available_bytes"] >= 0 for row in ready))

    def test_gpu_query_timeout_is_unknown_not_zero(self):
        with patch("cutecat.system_status.shutil.which", return_value="nvidia-smi"), patch("cutecat.system_status._run", side_effect=subprocess.TimeoutExpired("query", 2)), patch.object(self.collector, "_sysfs_gpus", return_value=[]):
            result = self.collector._gpu()
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["devices"], [])

    def test_cgroup_memory_and_cpu_limits(self):
        values = {"/proc/stat": "cpu 1 2 3 4", "/proc/cpuinfo": "model name: CPU",
                  "/proc/meminfo": "MemTotal: 100 kB\nMemAvailable: 50 kB",
                  "/sys/fs/cgroup/cpu.max": "150000 100000", "/sys/fs/cgroup/memory.max": "1024",
                  "/sys/fs/cgroup/memory.current": "256"}
        with patch("cutecat.system_status._text", side_effect=lambda p: values[str(p)]):
            self.assertEqual(self.collector._cpu()["quota_cpus"], 1.5)
            self.assertEqual(self.collector._memory()["container_limit_bytes"], 1024)

    def test_wsl_gpu_path_and_cache(self):
        with patch("cutecat.system_status.shutil.which", return_value=None), patch("pathlib.Path.is_file", return_value=True), patch("cutecat.system_status._run", return_value="GPU, 1, 200, 20, 0") as run:
            first = self.collector._gpu()
            self.assertIs(self.collector._gpu(), first)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], "/usr/lib/wsl/lib/nvidia-smi")

    def test_sysfs_amd_metrics_and_driver(self):
        from pathlib import Path
        device = Path(self.env.tmp) / "gpu"
        device.mkdir()
        for name, text in {"class":"0x030000", "vendor":"0x1002", "device":"0x1234", "gpu_busy_percent":"30", "mem_info_vram_total":"2048", "mem_info_vram_used":"1024"}.items():
            (device / name).write_text(text)
        with patch("pathlib.Path.glob", side_effect=lambda pattern: [device] if pattern == "*" else []):
            rows = self.collector._sysfs_gpus()
        self.assertEqual(rows[0]["utilization_percent"], 30)
        self.assertEqual(rows[0]["memory_total_bytes"], 2048)

    def test_concurrent_snapshot_only_collects_once(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(self.collector, "_gpu", return_value={"devices": []}) as spy:
            with ThreadPoolExecutor(max_workers=4) as pool:
                values = list(pool.map(lambda _: self.collector.snapshot(), range(4)))
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(all(value is values[0] for value in values))

    def test_decoder_tool_failure_and_success_cache(self):
        with patch("cutecat.decoders.shutil.which", return_value="ffmpeg"), patch("cutecat.decoders.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 3)):
            self.assertEqual(decoder_inventory(True)["status"], "unknown")
        proc = subprocess.CompletedProcess([], 0, " V....D h264 H.264\n", "")
        with patch("cutecat.decoders.shutil.which", return_value="ffmpeg"), patch("cutecat.decoders.subprocess.run", return_value=proc) as run:
            first = decoder_inventory(True)
            self.assertIs(decoder_inventory(), first)
        self.assertEqual(first["status"], "reported")
        self.assertEqual(run.call_count, 1)

    def test_decoder_list_and_missing_tool(self):
        rows = parse_decoders(" VFS..D h264 H.264 / AVC\n A....D aac AAC\n V..... = Video\n bogus\n")
        self.assertEqual([r["name"] for r in rows], ["h264", "aac"])
        with patch("cutecat.decoders.shutil.which", return_value=None):
            result = decoder_inventory(refresh=True)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["items"], [])


if __name__ == "__main__":
    unittest.main()
