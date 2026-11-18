from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from unittest import TestCase

from k1_sheet import SheetError, Workbook
from server import ApiError, Console, Handler
import worker
from worker import UNITY_RUNTIME_CAPABILITY, UNITY_RUNTIME_CHECK


class MemoryWorkbook(Workbook):
    def __init__(self):
        super().__init__("dws", "fixture")
        self.rows = [["内容", "验收人", "备注", "状态", "合并状态", "JIRA单"],
                     ["两个关联单号", "原有记录", "备注保留", "待验收", "已合进dev", "NK-11 NK-12"],
                     ["其他人接手", "", "", "待验收", "已合进dev", "NK-13"]]
        self.updates = []
        self.interrupt_write = False

    def call(self, *args):
        if args == ("list",):
            return {"sheets": [{"sheetId": "release", "name": "602"}]}
        if args[:2] == ("range", "read"):
            return {"returnedRange": f"A1:F{len(self.rows)}", "hasMore": False, "values": self.rows}
        assert args[:4] == ("range", "update", "--sheet-id", "release")
        address = args[5]
        row = int(address.split(":")[0][1:])
        values = json.loads(args[7])[0]
        assert address == f"B{row}:D{row}" and values[1] == {}
        self.rows[row - 1][1] = values[0]["text"]
        self.rows[row - 1][3] = values[2]["text"]
        self.updates.append((row, values[2]["text"]))
        if self.interrupt_write:
            self.interrupt_write = False
            raise SheetError("模拟写入已送达但响应丢失")
        return {"success": True}


class PackProgressTest(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workbook = MemoryWorkbook()
        self.console = Console(Path(temp.name) / "console.sqlite3", {"operator": "console"}, self.workbook)
        self.jobs = []
        self.node_tokens = {}
        for index in (1, 2):
            created = self.console.create_node({"name": f"Node {index}", "os": "Windows"})
            self.node_tokens[created["id"]] = created["token"]
            node = self.console.agent(created["id"], created["token"])
            self.console.configure_node(node["id"], {"root": "C:/QA"})
            self.console.heartbeat(node, {"root": "C:/QA", "os": "Windows",
                "capabilities": ["Unity", UNITY_RUNTIME_CAPABILITY], "checks": [{"label": UNITY_RUNTIME_CHECK,
                    "ok": True, "servers": ["2903"], "batch_preparation": True, "pack_writeback": True}]})
            job_id = self.console.submit({"request": f"pack-progress-test-{index}", "mode": "version", "sheet": "release",
                "branch": "dev", "method": "Unity", "server": "2903", "node": node["id"]}, f"操作人{index}")["id"]
            job = self.console.claim(node)["job"]
            self.assertEqual(job["id"], job_id)
            self.jobs.append((node, job))

    def action(self, index, action, item="NK-11", **data):
        node, job = self.jobs[index]
        return self.console.pack_action(node, {"attempt": job["attempt"], "action": action, "item": item, **data})

    def test_competing_nodes_only_one_claims_and_shared_row_aggregates(self):
        ready = Barrier(2)
        def claim(index):
            ready.wait(timeout=5)
            return self.action(index, "claim")["status"]
        with ThreadPoolExecutor(max_workers=2) as workers:
            states = list(workers.map(claim, range(2)))
        self.assertCountEqual(states, ["claimed", "skip"])
        winner = states.index("claimed")
        self.assertEqual(len(self.workbook.updates), 1)
        self.assertEqual(self.action(winner, "finish", result="pass", reason="已验收")["status"], "waiting")
        self.assertEqual(self.workbook.rows[1][3], "验收中")
        self.assertEqual(self.action(1 - winner, "claim", "NK-12")["status"], "skip")
        self.assertEqual(self.action(winner, "claim", "NK-12")["status"], "claimed")
        self.assertEqual(self.action(winner, "finish", "NK-12", result="fail", reason="实测失败")["status"], "recorded")
        self.assertEqual(self.workbook.rows[1][2:4], ["备注保留", "未通过"])
        self.assertIn("原有记录", self.workbook.rows[1][1])
        self.assertEqual(self.action(winner, "claim", "NK-13")["status"], "claimed")
        self.workbook.interrupt_write = True
        with self.assertRaises(SheetError):
            self.action(winner, "finish", "NK-13", result="pass", reason="实测通过")
        self.assertEqual(self.workbook.rows[2][3], "已完成")
        updates = self.workbook.updates[:]
        self.assertEqual(self.action(winner, "finish", "NK-13", result="pass", reason="实测通过")["status"], "recorded")
        self.assertEqual(self.workbook.updates, updates)

    def test_recheck_online_owner_and_recover_unacknowledged_writes(self):
        self.workbook.rows[2][1:4] = ["外部协作者", "保留备注", "验收中"]
        self.assertEqual(self.action(0, "claim", "NK-13")["status"], "skip")
        self.assertEqual(self.workbook.updates, [])
        self.workbook.interrupt_write = True
        with self.assertRaises(SheetError):
            self.action(0, "claim")
        self.assertEqual(self.action(1, "claim")["status"], "skip")
        self.assertEqual(self.action(0, "claim")["status"], "claimed")
        self.assertEqual(self.workbook.updates, [(2, "验收中")])
        self.workbook.rows.insert(1, ["新行", "", "", "已完成", "已合进dev", "NK-14"])
        self.assertEqual(self.action(0, "inspect")["rows"][0]["row"], 3)
        self.assertEqual(self.action(0, "cleanup")["status"], "cleaned")
        self.assertEqual(self.workbook.rows[2][1:4], ["原有记录", "备注保留", "待验收"])
        self.assertEqual(self.workbook.rows[3][1:4], ["外部协作者", "保留备注", "验收中"])

    def test_unclaimed_item_and_changed_owner_cannot_write_a_result(self):
        self.assertEqual(self.action(0, "finish", result="pass", reason="通过")["status"], "conflict")
        self.assertEqual(self.action(0, "claim", "NK-13")["status"], "claimed")
        self.workbook.rows[2][1] = "外部协作者"
        self.assertEqual(self.action(0, "finish", "NK-13", result="pass", reason="通过")["status"], "conflict")
        self.action(0, "cleanup")
        self.assertEqual(self.workbook.rows[2][1:4], ["外部协作者", "", "验收中"])
        node, _ = self.jobs[1]
        with self.assertRaises(ApiError):
            self.console.pack_action(node, {"attempt": self.jobs[0][1]["attempt"], "action": "claim", "item": "NK-13"})

    def test_same_jira_on_new_row_does_not_bypass_active_claim(self):
        self.assertEqual(self.action(0, "claim", "NK-13")["status"], "claimed")
        self.workbook.rows.append(["新增源行", "", "", "待验收", "已合进dev", "NK-13"])
        _, second = self.jobs[1]
        duplicate = {"key": "NK-13", "source_rows": [{"title": "新增源行", "keys": ["NK-13"]}]}
        self.assertEqual(self.console.pack_progress.item(second, duplicate, "claim")["status"], "skip")
        self.assertEqual(self.workbook.rows[-1][3], "待验收")
        self.action(0, "cleanup")
        self.assertEqual(self.console.pack_progress.item(second, duplicate, "claim")["status"], "claimed")

    def test_mcp_tool_uses_worker_identity_and_requires_observed_result(self):
        node, job = self.jobs[0]
        self.workbook.rows[1][1:4] = ["Other tester", "备注保留", "验收中"]
        with TemporaryDirectory() as directory, ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
            http.app = self.console
            serving = Thread(target=http.serve_forever, daemon=True)
            serving.start()
            try:
                run = Path(directory)
                manifest = run / "manifest.json"
                manifest.write_text(json.dumps({"project": directory, "server": "2903",
                    "attempt": job["attempt"], "acceptance_scope": {"writeback": True}}), encoding="utf-8")
                (run / "cases.json").write_text(json.dumps({key: [{"id": "TC-1", "state": "pending"}]
                    for key in ("NK-11", "NK-13")}), encoding="utf-8")
                session = {"url": f"http://127.0.0.1:{http.server_port}", "id": node["id"],
                           "token": self.node_tokens[node["id"]]}

                def call(request_id, name, **args):
                    return {"id": request_id, "method": "tools/call", "params": {"name": name, "arguments": args}}
                requests = [
                    {"id": 1, "method": "tools/list"},
                    call(2, "case_progress", item="NK-13", case="TC-1", state="running"),
                    call(3, "pack_item", action="claim", item="NK-13"),
                    call(4, "pack_item", action="finish", item="NK-13", result="pass", reason="Unobserved"),
                    call(5, "case_progress", item="NK-13", case="TC-1", state="running"),
                    call(6, "case_progress", item="NK-13", case="TC-1", state="blocked", observation="Blocked"),
                    call(7, "pack_item", action="release", item="NK-13", reason="Blocked"),
                    call(8, "pack_item", action="claim", item="NK-11"),
                ]
                result = subprocess.run([sys.executable, worker.__file__, "--unity-tools", str(manifest)],
                    input="\n".join(map(json.dumps, requests)) + "\n", capture_output=True, encoding="utf-8",
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "K1_CONSOLE_PACK_SESSION": json.dumps(session)},
                    timeout=15, check=True)
                replies = {reply["id"]: reply["result"] for reply in map(json.loads, result.stdout.splitlines())}
                self.assertIn("pack_item", {tool["name"] for tool in replies[1]["tools"]})
                self.assertTrue(replies[2]["isError"])
                self.assertEqual(json.loads(replies[3]["content"][0]["text"])["status"], "claimed")
                self.assertTrue(replies[4]["isError"])
                self.assertEqual(json.loads(replies[5]["content"][0]["text"])["state"], "running")
                self.assertEqual(json.loads(replies[6]["content"][0]["text"])["state"], "blocked")
                self.assertEqual(json.loads(replies[7]["content"][0]["text"])["status"], "recorded")
                self.assertEqual(json.loads(replies[8]["content"][0]["text"])["status"], "skip")
                self.assertEqual(self.workbook.updates, [(3, "验收中"), (3, "待验收")])
                worker.Node.finish_auto_cases({"items": job["items"], "server": "2903", "client": "fixture"}, run, "Ended")
                report = worker.Node.unity_result(run, {"server": "2903"}, "fixture", 0, [item["key"] for item in job["items"]])
                self.assertEqual(report["NK-11"]["result"], "skipped")
                self.assertEqual(report["NK-13"]["result"], "blocked")
            finally:
                http.shutdown()
                serving.join(timeout=5)
