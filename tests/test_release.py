import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from release import publish
from server import ApiError, Console, PROJECT


class ReleaseFlowTest(unittest.TestCase):
    def test_in_flight_batch_finishes_before_its_worker_can_update(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = {"database":str(root/"console.db")}
            config_file = root / "server.json"
            config_file.write_text(json.dumps(config))
            console = Console(config["database"],config,None)
            enrollment = console.create_node({"name":"QA","os":"Windows"})
            node_id = enrollment["id"]
            node = console.agent(node_id,enrollment["token"])
            console.configure_node(node_id,{"root":"C:/QA"})
            heartbeat = {"root":"C:/QA","os":"Windows","capabilities":["真机"],"checks":[],"revision":"0"*64}
            self.assertIsNone(console.heartbeat(node,heartbeat)["update"])
            request = {"mode":"branch","branch":"dev_NK-9705","source":"NK-9705","method":"真机",
                       "node":node_id,"request":"sample-branch-job-0001"}
            job_id = console.submit(request)["id"]
            response = console.claim(node)
            self.assertIn("ai_prompt", response["features"])
            claim = response["job"]
            self.assertEqual(claim["id"],job_id)
            self.assertIsNotNone(claim["started"])
            release = publish(config_file,PROJECT,"a"*40,"改善运行队列")
            target = release["worker_sha256"]
            console.request_update(node_id,{"target":target})
            self.assertIsNone(console.heartbeat(node,heartbeat)["update"])
            with self.assertRaises(ApiError) as error:
                console.update_package(node,{"target":target})
            self.assertEqual(error.exception.status,409)

            def event(seq,kind,**payload):
                return console.event(node,{"attempt":claim["attempt"],"kind":kind,"seq":seq,"payload":payload})

            snapshot = [{"id":"TC-1","title":"布阵显示半身像","state":"pass","evidence":"截图"}]
            prompt = ["原始验收要求\n\n", "  <资料>保留格式</资料>\n"]
            for seq, part in enumerate(prompt, 1):
                event(seq, "ai_prompt", text=part)
            self.assertEqual([json.loads(entry["payload"])["text"] for entry in console.events(job_id, 0)], prompt)
            event(3,"case_snapshot",key="NK-9705",cases=snapshot)
            self.assertEqual(console.detail(job_id)["items"][0]["cases"], snapshot)
            event(4,"item_finished",key="NK-9705",result="pass",evidence={"reason":"通过"})
            event(5,"execution_finished",process_exited=True)
            finished = console.detail(job_id)
            self.assertGreaterEqual(finished["ended"], finished["started"])
            console.submit({**request,"request":"sample-branch-job-0002"})
            self.assertIsNone(console.claim(node)["job"])
            self.assertEqual(console.heartbeat(node,heartbeat)["update"],release)
            package = console.update_package(node,{"target":target})
            self.assertEqual(hashlib.sha256(base64.b64decode(package["worker"])).hexdigest(),target)
            self.assertIsNone(console.heartbeat(node,{**heartbeat,"revision":target})["update"])
            self.assertEqual(console.state()["nodes"][0]["update"],"installed")
            self.assertIsNotNone(console.claim(node)["job"])
