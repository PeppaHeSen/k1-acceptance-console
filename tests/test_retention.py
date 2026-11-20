from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from server import ApiError, Console


class RetentionTest(unittest.TestCase):
    def test_retention_prunes_history_on_save_and_completion(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "console.db"
            console = Console(db_path, {}, None)
            enrollment = console.create_node({"name": "QA", "os": "Windows"})
            node = console.agent(enrollment["id"], enrollment["token"])
            console.configure_node(node["id"], {"root": "C:/QA"})
            console.heartbeat(node, {"root": "C:/QA", "os": "Windows", "capabilities": ["真机"], "checks": []})

            def request(number):
                return {"request": f"retention-example-{number}", "mode": "branch",
                        "branch": "dev_NK-9705", "source": "NK-9705", "method": "真机"}

            def finish(claim):
                item = {"attempt": claim["attempt"], "seq": 1, "kind": "item_finished",
                        "payload": {"key": "NK-9705", "result": "pass", "evidence": {"reason": "验收通过"}}}
                console.event(node, item)
                event = {"attempt": claim["attempt"], "seq": 2, "kind": "execution_finished",
                         "payload": {"process_exited": True}}
                console.event(node, event)
                return event

            first = console.submit(request(1))["id"]
            first_event = finish(console.claim(node)["job"])
            second = console.submit(request(2))["id"]
            finish(console.claim(node)["job"])
            third = console.submit(request(3))["id"]
            third_claim = console.claim(node)["job"]
            fourth = console.submit(request(4))["id"]

            with self.assertRaises(ApiError) as error:
                console.set_retention({"limit": 1, "expected_deleted": 0})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(console.detail(first)["state"], "done")
            self.assertEqual(console.set_retention({"limit": 1, "expected_deleted": 1}),
                             {"limit": 1, "completed": 1, "removed": 1})
            self.assertEqual(console.events(first, 0), [])
            with self.assertRaises(ApiError) as error:
                console.detail(first)
            self.assertEqual(error.exception.status, 404)
            self.assertEqual(console.event(node, first_event), {"ack": 2})
            with self.assertRaises(ApiError) as error:
                console.submit(request(1))
            self.assertEqual(error.exception.status, 410)
            self.assertEqual(console.detail(third)["state"], "running")
            self.assertEqual(console.detail(fourth)["state"], "queued")

            finish(third_claim)
            self.assertEqual(console.events(second, 0), [])
            restored = Console(db_path, {}, None)
            self.assertEqual(restored.state()["retention"], {"limit": 1, "completed": 1})
            self.assertEqual([job["id"] for job in restored.state()["jobs"]], [fourth, third])
