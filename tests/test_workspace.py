from contextlib import nullcontext
import json
from pathlib import Path
from subprocess import Popen
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from worker import (FUNCTION_REVIEW_CASES, FUNCTION_REVIEW_RUNTIME, FunctionReview, Node,
                    UnityTools, acceptance_checkout, command, save_json)


class AcceptanceCheckoutTest(unittest.TestCase):
    def test_language_restart_retries_play_once_after_login_error(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_player": 1})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            tools.bridge.code.side_effect = ["login_error", True, "login_error", "logged_in"]
            self.assertEqual(tools.resume_test_login(), "restarting_play")
            self.assertEqual(tools.resume_test_login(), "login_error")
            self.assertEqual(tools.resume_test_login(), "logged_in")
            self.assertEqual([c.args for c in tools.bridge.play.call_args_list], [(False,), (True,)])

    def test_game_view_resize_checks_screenshot_pixels_instead_of_screen_dimensions(self):
        with TemporaryDirectory() as temp:
            run = Path(temp)
            manifest = run / "runtime.json"
            save_json(manifest, {"server": "2903", "project": str(run), "test_player": 1})
            tools = UnityTools(manifest)
            tools.bridge.code = Mock(side_effect=[0, True, True])
            old = {"width": 1080, "height": 2160, "language": "cn"}
            requested = {"width": 768, "height": 1024, "language": "cn"}
            with patch.object(tools, "capture_view", side_effect=[(old, b"old"), (requested, b"new")]), \
                    patch("worker.time.sleep"):
                self.assertEqual(tools.set_game_view(768, 1024), (requested, b"new"))
            original = run / "game-view-original.json"
            self.assertEqual(json.loads(original.read_text(encoding="utf-8")), 0)
            tools.restore_game_view()
            self.assertFalse(original.exists())

    def test_auto_result_blocks_unexecuted_cases(self):
        with TemporaryDirectory() as temp:
            run = Path(temp)
            manifest = {"server": "2903", "client": "a" * 40, "items": [{"key": "NK-1"}],
                        "project": str(run), "test_player": 1}
            save_json(run / "runtime.json", manifest)
            save_json(run / "cases.json", {"NK-1": [{"id": "TC-1", "title": "观察画面", "state": "pending"}]})
            tools = UnityTools(run / "runtime.json")
            tools.record({"item": "NK-1", "case": "TC-1", "state": "running"})
            with self.assertRaisesRegex(ValueError, "observe"):
                tools.record({"item": "NK-1", "case": "TC-1", "state": "pass", "observation": "猜测"})
            Node.finish_auto_cases(manifest, run, "没有得到实测证据。")
            cases = json.loads((run / "cases.json").read_text(encoding="utf-8"))
            report = json.loads((run / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(cases["NK-1"][0]["state"], "blocked")
            self.assertEqual(report["items"]["NK-1"]["result"], "blocked")

    def test_runtime_error_restores_original_branch_and_discards_tracked_edits(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            repo = base / "repo"
            repo.mkdir()
            run = base / "run"
            run.mkdir()

            def git(*args):
                return command(["git", *args], cwd=repo).stdout.strip()

            git("init", "-b", "dev")
            git("config", "user.email", "fixture@example.test")
            git("config", "user.name", "Test")
            source = repo / "hero.txt"
            source.write_text("existing version\n")
            git("add", "hero.txt")
            git("commit", "-m", "existing version")
            original = git("rev-parse", "HEAD")
            git("switch", "-c", "feature")
            source.write_text("new portrait\n")
            git("commit", "-am", "new portrait")
            target = git("rev-parse", "HEAD")
            git("switch", "dev")
            source.write_text("generated staged edit\n")
            git("add", "hero.txt")
            source.write_text("generated unstaged edit\n")
            untracked = repo / "notes.txt"
            untracked.write_text("node notes\n")
            with self.assertRaisesRegex(RuntimeError, "editor failed"):
                with acceptance_checkout(repo, target, run) as project:
                    self.assertEqual(project, repo / "client")
                    self.assertEqual(git("rev-parse", "HEAD"), target)
                    self.assertEqual(source.read_text(), "new portrait\n")
                    self.assertTrue(untracked.exists())
                    source.write_text("Unity-generated update\n")
                    raise RuntimeError("editor failed")
            self.assertEqual(git("branch", "--show-current"), "dev")
            self.assertEqual(git("rev-parse", "HEAD"), original)
            self.assertEqual(git("status", "--porcelain", "--untracked-files=no"), "")
            self.assertEqual(git("show", ":hero.txt"), "existing version")
            self.assertEqual(source.read_text(), "existing version\n")
            self.assertEqual(untracked.read_text(), "node notes\n")

    def test_unity_publishes_ai_case_plan_before_starting_editor(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "client"
            (project / "ProjectSettings").mkdir(parents=True)
            (project / "ProjectSettings" / "ProjectVersion.txt").write_text("m_EditorVersion: 2022.3.1f1")
            skill = base / "skill"
            skill.mkdir()
            rules = {
                path: f"verified {Path(path).parent.name}/{Path(path).name}"
                for path in (*FUNCTION_REVIEW_RUNTIME, *FUNCTION_REVIEW_CASES)
            }
            for path, text in rules.items():
                fixture = skill / path
                fixture.parent.mkdir(parents=True, exist_ok=True)
                fixture.write_text(text, encoding="utf-8")
            for args in (("init", "-b", "main"), ("add", ".")):
                command(["git", *args], cwd=skill)
            command(["git", "-c", "user.name=Test", "-c", "user.email=fixture@example.test",
                     "-c", "commit.gpgSign=false", "commit", "-m", "Skill snapshot"], cwd=skill)
            revision = command(["git", "rev-parse", "HEAD"], cwd=skill).stdout.strip()
            run = base / "run"
            run.mkdir()
            node = Node.__new__(Node)
            node.config = {"unity_editor": str(base / "2022.3.1f1" / "Unity"), "unity_test_roles": {"2903": 1}}
            node.attempt = {"id": "test-attempt"}
            node.cancel_attempt = None
            node.persist = Mock()
            node.watch_stop = lambda _: nullcontext()
            node.check_skill = lambda: skill
            node.sync_skill = Mock(return_value=(skill, revision, True))
            node.event = Mock()
            node.run_claude = Mock(side_effect=[{"summary": "已准备验收清单", "items": [
                {"key": "NK-9705", "cases": [{"id": "TC-01", "title": "检查半身像"}]},
            ]}, None])
            node.close_unity = Mock()
            process = Mock(pid=314, returncode=0)
            process.poll.side_effect = [None, None, 0]
            job = {"id": 8, "attempt": "test-attempt", "items": [{"key": "NK-9705", "title": "布阵页"}],
                   "config": {"mode": "branch", "refs": {"client": "dev_NK-9705"},
                              "source": "NK-9705", "server": "2903", "notes": ""}}

            def launch(*args, **kwargs):
                if args[0][0] == "git":
                    return Popen(*args, **kwargs)
                self.assertTrue((run / "cases.json").is_file())
                self.assertTrue(any(call.args == ("case_snapshot",) for call in node.event.call_args_list))
                return process

            def git(args, cwd=None, timeout=30):
                if cwd == skill:
                    return command(args, cwd=cwd, timeout=timeout)
                return Mock(stdout="new portrait")

            def unity(manifest):
                bridge = Mock(manifest=manifest)
                bridge.code.side_effect = ["playing", "logged_in"]
                return bridge

            # Local changes to the skill checkout must never drift into a batch.
            (skill / FUNCTION_REVIEW_RUNTIME[1]).write_text("a different local workflow", encoding="utf-8")
            with patch("worker.command", side_effect=git), patch("worker.UnityBridge", side_effect=unity), \
                    patch("worker.subprocess.Popen", launch):
                snapshot, runtime_rules = node.prepare_function_review()
                self.assertEqual(node.launch_unity(job, "b" * 40, project, run, base, snapshot, runtime_rules), 0)
            node.sync_skill.assert_called_once_with(skill)
            planning, execution = (call.args[0] for call in node.run_claude.call_args_list)
            for path in FUNCTION_REVIEW_RUNTIME:
                self.assertIn(rules[path], planning)
                self.assertIn(rules[path], execution)
            for path in FUNCTION_REVIEW_CASES:
                self.assertIn(rules[path], planning)
            self.assertIn(revision, planning)
            self.assertIn(revision, execution)
            self.assertNotIn("a different local workflow", execution)
            manifest = json.loads((run / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["function_review"]["revision"], revision)
            self.assertEqual(manifest["acceptance_scope"]["acceptanceMode"], "development")
            with self.assertRaises(ValueError):
                FunctionReview(manifest["function_review"]).read("review-k1-function/references/../../secret.md")
            snapshots = [call.kwargs["cases"] for call in node.event.call_args_list if call.args == ("case_snapshot",)]
            self.assertEqual(snapshots[0], [{"id": "TC-01", "title": "检查半身像", "state": "pending", "evidence": ""}])
            self.assertEqual(snapshots[1][0]["state"], "blocked")
