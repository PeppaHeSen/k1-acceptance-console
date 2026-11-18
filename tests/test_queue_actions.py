from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from server import ApiError, Console
from worker import ExecutionStopped, Node, UNITY_RUNTIME_CAPABILITY, UNITY_RUNTIME_CHECK


class QueueActionsTest(unittest.TestCase):
    def test_runtime_job_waits_for_a_node_with_the_requested_test_server(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)
            enrolled = console.create_node({"name": "QA", "os": "Windows"})
            node = console.agent(enrolled["id"], enrolled["token"])
            console.configure_node(node["id"], {"root": "C:/QA"})
            beat = {"root": "C:/QA", "os": "Windows", "capabilities": ["Unity"], "checks": []}
            console.heartbeat(node, beat)
            request = {"mode": "branch", "source": "NK-9705", "branch": "dev_NK-9705",
                       "method": "Unity", "server": "2903", "request": "verify-runtime-routing"}
            with self.assertRaisesRegex(ApiError, "自动实测"):
                console.submit({**request, "node": node["id"]})
            job_id = console.submit(request)["id"]
            self.assertIsNone(console.claim(node)["job"])
            self.assertIn("自动实测", console.state()["jobs"][0]["waiting"])
            beat["capabilities"].append(UNITY_RUNTIME_CAPABILITY)
            beat["checks"] = [{"label": UNITY_RUNTIME_CHECK, "ok": True, "servers": ["2904"]}]
            console.heartbeat(node, beat)
            self.assertIsNone(console.claim(node)["job"])
            beat["checks"][0]["servers"].append("2903")
            console.heartbeat(node, beat)
            self.assertIsNone(console.claim(node)["job"])
            beat["checks"][0]["batch_preparation"] = True
            console.heartbeat(node, beat)
            self.assertEqual(console.claim(node)["job"]["id"], job_id)

    def test_version_day_uses_entire_sheet_and_keeps_preparation_flags_independent(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)
            rows = [{"key": "NK-1", "title": "事项一"}, {"key": "NK-2", "title": "事项二"}]
            console.workbook = Mock(document="https://example.test/pack")
            console.workbook.sheets.return_value = [{"id": "sheet", "name": "602"}]
            console.workbook.entries.return_value = rows
            request = {"request": "verify-preparation-0001", "mode": "version", "method": "Unity",
                       "sheet": "sheet", "branch": "dev"}
            job = console.detail(console.submit(request)["id"])
            self.assertEqual(job["items"], rows)
            self.assertEqual(job["config"]["source"], "")
            self.assertTrue(job["config"]["auto_refresh_config"])
            self.assertTrue(job["config"]["auto_deploy"])
            branch = {"request": "verify-preparation-0002", "mode": "branch", "method": "Unity",
                      "source": "NK-1", "branch": "dev_NK-1", "auto_refresh_config": False}
            config = console.detail(console.submit(branch)["id"])["config"]
            self.assertFalse(config["auto_refresh_config"])
            self.assertTrue(config["auto_deploy"])

    def test_linked_merge_request_can_resolve_a_differently_named_branch(self):
        key = "NK-9705"
        sha = "a" * 40
        links = [{"object": {"url": "https://git.example.test/k1/client/-/merge_requests/123"}}]
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {"branch_lookup_repo": tmp}, None)
            with patch.object(console, "jira_links", return_value=links), patch("server.subprocess.run") as git:
                git.side_effect = [
                    subprocess.CompletedProcess([], 0, "git@git.example.test:k1/client.git\n"),
                    subprocess.CompletedProcess([], 0, f"{sha}\trefs/heads/feature-combat\n"
                                                f"{sha}\trefs/merge-requests/123/head\n"
                                                f"{'b'*40}\trefs/heads/dev_{key}\n"),
                ]
                job = console.submit({"mode": "branch", "source": key, "method": "Unity",
                                      "request": "verify-linked-branch-0001"})
            self.assertEqual(console.detail(job["id"])["config"]["branch"], "feature-combat")

    def test_queued_delete_and_running_stop_have_distinct_transitions(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)
            enrolled = console.create_node({"name": "QA", "os": "Windows"})
            node = console.agent(enrolled["id"], enrolled["token"])
            console.configure_node(node["id"], {"root": "C:/QA"})
            beat = {"root": "C:/QA", "os": "Windows", "capabilities": ["Unity"], "checks": [],
                    "stop_supported": True}
            console.heartbeat(node, beat)
            request = {"mode": "branch", "source": "NK-9705", "branch": "feature-combat", "method": "Unity",
                       "request": "verify-queue-actions-0001"}
            deleted = console.submit(request)["id"]
            self.assertEqual(console.change_queue(deleted, "delete"), {"deleted": True})
            with self.assertRaises(ApiError):
                console.submit(request)
            running = console.submit({**request, "request": "verify-queue-actions-0002"})["id"]
            self.assertGreater(running, deleted)
            claim = console.claim(node)["job"]
            self.assertEqual(claim["id"], running)
            with self.assertRaises(ApiError):
                console.change_queue(running, "delete")
            event = {"attempt": claim["attempt"], "seq": 1, "kind": "execution_stopped",
                     "payload": {"process_exited": True}}
            with self.assertRaises(ApiError):
                console.event(node, event)
            console.change_queue(running, "stop")
            self.assertEqual(console.detail(running)["state"], "running")
            self.assertEqual(console.heartbeat(node, beat)["stop_attempt"], claim["attempt"])
            console.event(node, event)
            self.assertEqual(console.detail(running)["stage"], "已停止")
            self.assertIsNone(console.heartbeat(node, beat)["stop_attempt"])

    def test_worker_interrupts_its_child_for_the_matching_attempt(self):
        worker = Node.__new__(Node)
        worker.attempt = {"id": "active"}
        worker.cancel_attempt = "different"
        with subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"]) as process:
            with worker.watch_stop(process):
                self.assertIsNone(process.poll())
                worker.cancel_attempt = "active"
                self.assertNotEqual(process.wait(timeout=5), 0)
            with self.assertRaises(ExecutionStopped):
                worker.check_stopped()
