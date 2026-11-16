from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from worker import Node, acceptance_checkout, command


class AcceptanceCheckoutTest(unittest.TestCase):
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
            (skill / "review-k1-function").mkdir(parents=True)
            (skill / "review-k1-function" / "SKILL.md").write_text("read this verified skill")
            run = base / "run"
            run.mkdir()
            node = Node.__new__(Node)
            node.config = {"unity_editor": str(base / "2022.3.1f1" / "Unity")}
            node.attempt = {"id": "test-attempt"}
            node.cancel_attempt = None
            node.persist = Mock()
            node.watch_stop = lambda _: nullcontext()
            node.check_skill = lambda: skill
            node.sync_skill = lambda _: (skill, "a" * 40, True)
            node.event = Mock()
            node.run_claude = Mock(return_value={"summary": "已准备验收清单", "items": [
                {"key": "NK-9705", "cases": [{"id": "TC-01", "title": "检查半身像"}]},
            ]})
            process = Mock(pid=314, returncode=0)
            process.poll.return_value = 0
            job = {"id": 8, "attempt": "test-attempt", "items": [{"key": "NK-9705", "title": "布阵页"}],
                   "config": {"mode": "branch", "refs": {"client": "dev_NK-9705"},
                              "source": "NK-9705", "server": "2903", "notes": ""}}

            def launch(*args, **kwargs):
                self.assertTrue((run / "cases.json").is_file())
                self.assertTrue(any(call.args == ("case_snapshot",) for call in node.event.call_args_list))
                return process

            with patch("worker.command", return_value=Mock(stdout="new portrait")), patch("worker.subprocess.Popen", launch):
                self.assertEqual(node.launch_unity(job, "b" * 40, project, run, base), 0)
            self.assertIn("read this verified skill", node.run_claude.call_args.args[0])
            snapshots = [call.kwargs["cases"] for call in node.event.call_args_list if call.args == ("case_snapshot",)]
            self.assertEqual(snapshots, [[{"id": "TC-01", "title": "检查半身像", "state": "pending", "evidence": ""}]])
