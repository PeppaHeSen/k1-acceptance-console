from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from server import ApiError, Console, PAIRING_TIMEOUT


class PairingTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.console = Console(Path(directory.name) / "console.sqlite3", {}, None)
        self.registration = {"name": "Windows QA", "ip": "192.168.1.20"}
        self.worker = {"os": "Windows", "token": secrets.token_urlsafe(32)}

    def test_pairing_is_bound_to_ip_and_can_retry_after_lost_reply(self):
        ip = self.registration["ip"]
        self.assertIsNone(self.console.accept_pairing(ip, self.worker)["id"])
        self.console.request_pairing(self.registration)
        self.assertIsNone(self.console.accept_pairing("192.168.1.21", self.worker)["id"])
        node_id = self.console.accept_pairing(ip, self.worker)["id"]
        self.assertTrue(node_id)
        self.assertEqual(self.console.accept_pairing(ip, self.worker)["id"], node_id)
        other = {**self.worker, "token": secrets.token_urlsafe(32)}
        self.assertIsNone(self.console.accept_pairing(ip, other)["id"])
        node = self.console.agent(node_id, self.worker["token"])
        self.console.heartbeat(node, {"os": "Windows", "capabilities": [], "checks": []})
        self.assertEqual(len(self.console.state()["nodes"]), 1)
        self.assertTrue(self.console.state()["nodes"][0]["online"])
        self.assertEqual(self.console.pairing_status()["items"][0]["node"], node_id)
        with self.assertRaises(ApiError) as error:
            self.console.request_pairing(self.registration)
        self.assertEqual(error.exception.status, 409)

    def test_expired_pairing_can_be_requested_again(self):
        ip = self.registration["ip"]
        with patch("server.time.time", return_value=10):
            first = self.console.request_pairing(self.registration)
            self.assertEqual(self.console.request_pairing(self.registration), first)
        with patch("server.time.time", return_value=10 + PAIRING_TIMEOUT):
            self.assertEqual(self.console.pairing_status()["items"], [])
            self.assertIsNone(self.console.accept_pairing(ip, self.worker)["id"])
            self.console.request_pairing({**self.registration, "name": "Replacement name"})
            self.assertTrue(self.console.accept_pairing(ip, self.worker)["id"])
        self.assertEqual(self.console.state()["nodes"][0]["name"], "Replacement name")
