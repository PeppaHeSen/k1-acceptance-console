from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from server import ApiError, Console
from worker import ExecutionStopped, Node, UNITY_RUNTIME_CAPABILITY, UNITY_RUNTIME_CHECK


class QueueActionsTest(unittest.TestCase):
    def test_automatic_executor_prefers_idle_codex_and_respects_worker_selection(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)

            def enroll(name, checks):
                created = console.create_node({"name": name, "os": "macOS"})
                node = console.agent(created["id"], created["token"])
                console.configure_node(node["id"], {"root": "/tmp/qa"})
                console.heartbeat(node, {"root": "/tmp/qa", "os": "macOS",
                                         "capabilities": ["Unity", UNITY_RUNTIME_CAPABILITY], "checks": [
                                             {"label": UNITY_RUNTIME_CHECK, "ok": True, "servers": ["2903"],
                                              "acceptance_reporting": True}, *checks]})
                return node

            claude = enroll("Claude", [{"label": "Claude Code", "ok": True}, {"label": "Codex CLI", "ok": False}])
            codex = enroll("Codex", [{"label": "Codex App", "ok": True}, {"label": "Codex CLI", "ok": True}])
            console.submit({"request": "verify-auto-executor-01", "mode": "branch", "source": "NK-1",
                            "branch": "dev_NK-1", "method": "Unity", "server": "2903", "runner": "auto",
                            "auto_refresh_config": False, "auto_deploy": False})
            self.assertIsNone(console.claim(claude)["job"])
            job = console.claim(codex)["job"]
            self.assertEqual(job["config"]["runner"], "codex_app")
            self.assertEqual(job["config"]["reporting"], "cases")
            self.assertIn("case_progress", Node.unity_prompt({"source": "NK-1", "server": "2903"}, job["config"]))
            console.submit({"request": "verify-auto-executor-02", "mode": "branch", "source": "NK-1",
                            "branch": "dev_NK-1", "method": "Unity", "runner": "auto"})
            self.assertEqual(console.claim(claude)["job"]["config"]["runner"], "claude")
            self.assertIsNone(console.claim(codex)["job"])

    def test_only_app_batches_expose_a_valid_saved_codex_session(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)
            enrolled = console.create_node({"name": "Mac", "os": "macOS"})
            node = console.agent(enrolled["id"], enrolled["token"])
            console.configure_node(node["id"], {"root": "/tmp/qa"})
            checks = [{"label": "Codex CLI", "ok": True}]
            beat = {"root": "/tmp/qa", "os": "macOS", "capabilities": ["Unity"], "checks": checks}
            console.heartbeat(node, beat)
            beat["capabilities"].append(UNITY_RUNTIME_CAPABILITY)
            checks.append({"label": UNITY_RUNTIME_CHECK, "ok": True, "servers": ["2903"]})
            console.heartbeat(node, beat)
            job_id = console.submit({"mode": "branch", "source": "NK-1", "branch": "dev_NK-1",
                                     "method": "Unity", "runner": "codex_app", "request": "verify-app-session",
                                     "server": "2903", "final_output_only": True})["id"]
            self.assertIsNone(console.claim(node)["job"])
            checks.append({"label": "Codex App", "ok": True})
            console.heartbeat(node, beat)
            job = console.claim(node)["job"]
            self.assertEqual(job["id"], job_id)
            self.assertNotIn("codex_thread_id", console.detail(job_id))
            event = {"attempt": job["attempt"], "seq": 1, "kind": "codex_session"}
            with self.assertRaisesRegex(ApiError, "会话 ID"):
                console.event(node, {**event, "payload": {"thread_id": "new"}})
            thread_id = "019b2acb-764a-7c54-8f32-fbaedd6ba073"
            payload = {**event, "payload": {"thread_id": thread_id}}
            self.assertEqual(console.event(node, payload), {"ack": 1})
            self.assertEqual(console.event(node, payload), {"ack": 1})
            self.assertEqual(console.detail(job_id)["codex_thread_id"], thread_id)
            config = console.detail(job_id)["config"]
            self.assertTrue(config["final_output_only"])
            self.assertFalse(config["auto_deploy"])
            final = Node.unity_prompt({"source": "NK-1", "server": "2903"}, config)
            self.assertIn("功能验收", final)
            self.assertIn("dev_NK-1", final)

    def test_jobs_only_route_to_the_requested_ai_runner(self):
        with TemporaryDirectory() as tmp:
            console = Console(Path(tmp) / "console.sqlite3", {}, None)
            nodes = []
            for name, checks in (
                ("Claude", [{"label": "Claude Code", "ok": True}]),
                ("Codex", [{"label": "Codex CLI", "ok": True}, {"label": "Claude Code", "ok": False}]),
            ):
                enrolled = console.create_node({"name": name, "os": "Windows"})
                node = console.agent(enrolled["id"], enrolled["token"])
                console.configure_node(node["id"], {"root": "C:/QA"})
                console.heartbeat(node, {"root": "C:/QA", "os": "Windows", "capabilities": ["Unity"], "checks": checks})
                nodes.append(node)
            claude, codex = nodes
            request = {"mode": "branch", "source": "NK-9705", "branch": "dev_NK-9705",
                       "method": "Unity", "runner": "codex", "request": "verify-codex-routing"}
            with self.assertRaisesRegex(ApiError, "Codex CLI"):
                console.submit({**request, "node": claude["id"]})
            with self.assertRaisesRegex(ApiError, "AI 执行器"):
                console.submit({**request, "runner": ["bogus"]})
            job_id = console.submit(request)["id"]
            self.assertIsNone(console.claim(claude)["job"])
            job = console.claim(codex)["job"]
            self.assertEqual((job["id"], job["config"]["runner"]), (job_id, "codex"))
            del request["runner"]
            old_id = console.submit({**request, "request": "verify-old-api-routing"})["id"]
            old = console.claim(claude)["job"]
            self.assertEqual((old["id"], old["config"]["runner"]), (old_id, "claude"))

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
