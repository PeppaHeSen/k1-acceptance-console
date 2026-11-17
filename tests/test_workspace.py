from contextlib import nullcontext
import json
from pathlib import Path
from subprocess import Popen
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from server import Console

from worker import (FUNCTION_REVIEW_CASES, FUNCTION_REVIEW_RUNTIME, UNITY_RUNTIME_CAPABILITY, FunctionReview, Node,
                    UnityTools, acceptance_checkout, command, resolve_repository, save_json)


class AcceptanceCheckoutTest(unittest.TestCase):
    def test_windows_runtime_claim_does_not_fall_back_to_integration(self):
        node = Node.__new__(Node)
        node.os_name = "Windows"
        node.caps = ["Unity", UNITY_RUNTIME_CAPABILITY]
        node.config = {"unity_runtime": True, "unity_test_roles": {"2903": 1}}
        node.persist = Mock()
        node.check_stopped = Mock()
        node.execute_unity = Mock()
        job = {"id": 29, "attempt": "fixture", "config": {"method": "Unity", "server": "2903"}}
        node.execute({"job": job, "root": "D:/K1Project"})
        node.execute_unity.assert_called_once_with(job, Path("D:/K1Project"))

    def test_saved_case_images_share_the_original_case_gallery(self):
        with TemporaryDirectory() as temp:
            attempt = "a" * 32
            run = Path(temp) / attempt
            run.mkdir()
            items = [{"key": "NK-1", "cases": []}, {"key": "NK-2", "cases": [{"id": "TC-2"}]}]
            path = run / "runtime.json"
            save_json(path, {"batch": 22, "attempt": attempt, "server": "2903", "project": temp,
                             "test_player": 1, "items": items})
            tools = UnityTools(path)
            tools.active = ("NK-2", "TC-2")
            tools.capture_view = Mock(side_effect=[({}, b"first screenshot"), ({}, b"second screenshot")])
            for name in ("item-2-TC-2.png", "item-2-TC-2.2.png"):
                info, _ = tools.handle("capture_view", {"save": True})
                self.assertEqual(info["evidence"], name)
            (run / "item-2-unreported.2.png").write_bytes(b"another screenshot")
            console = Console.__new__(Console)
            console.local_evidence = ("mac", Path(temp))
            job = {"id": 22, "attempt": attempt, "node": "mac", "items": items}
            self.assertEqual(list(console.screenshots(job)), ["item-2-TC-2.png", "item-2-TC-2.2.png"])

    def test_project_checks_accept_flat_and_nested_repositories(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            paths = {"client": root / "k1_client", "game": root / "K1Game" / "game",
                     "data": root / "dataconfig"}
            for path in paths.values():
                path.mkdir(parents=True)
                command(["git", "init", "-q"], cwd=path)
            node = Node.__new__(Node)
            node.config = {"methods": []}
            node.check_skill = Mock(return_value=root)
            node.claude = None
            node.setup(str(root))
            checks = {check["label"]: check for check in node.checks}
            for role, path in paths.items():
                self.assertEqual(checks[role], {"label": role, "ok": True, "detail": str(path.resolve())})
            (paths["client"] / "client").mkdir()
            with self.assertRaisesRegex(ValueError, "Git 工作树"):
                resolve_repository(paths["client"], "K1Client/client")

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

    def test_case_can_prepare_environment_and_resume_after_other_cases(self):
        with TemporaryDirectory() as temp:
            run = Path(temp)
            manifest = run / "runtime.json"
            save_json(manifest, {"batch": 28, "attempt": "test", "server": "2903", "project": temp,
                                 "client": "a" * 40, "function_review": {"revision": "b" * 40},
                                 "reference_branches": {"game": "dev"}, "test_player": 1,
                                 "test_roles": {"2903": [1], "2904": [2]},
                                 "environment_actions": {"scenario": {
                                     "kind": "gm", "server": 2904, "player": 2, "description": "准备场景", "timeout": 10,
                                     "parameters": {"stage": ["active"]},
                                     "command": [sys.executable, "-c",
                                                 "import json,sys; request=json.load(sys.stdin);"
                                                 "assert request['server']==2904 and request['case']=='TC-1';"
                                                 "assert request['parameters']=={'stage':'active'} and request['operation'];"
                                                 "print(json.dumps({'status':'ready','evidence':'readback 123'}))"],
                                 }}})
            cases = run / "cases.json"
            save_json(cases, {"NK-1": [{"id": "TC-1", "state": "pending"}, {"id": "TC-2", "state": "pending"}]})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            tools.bridge.code.side_effect = ["logged_in", True]
            options = tools.handle("environment_options", {})
            self.assertEqual(options["roles"]["2904"], [2])
            self.assertNotIn("command", options["actions"]["scenario"])
            first = {"item": "NK-1", "case": "TC-1"}
            second = {"item": "NK-1", "case": "TC-2"}
            action = {"action": "scenario", "reason": "缺前置", "parameters": {"stage": "active"}}
            tools.record({**first, "state": "running"})
            with self.assertRaisesRegex(ValueError, "测试服和角色"):
                tools.handle("prepare_environment", action)
            with self.assertRaisesRegex(ValueError, "不在节点配置"):
                tools.handle("select_test_role", {"server": 2904, "player": 3})
            self.assertEqual(tools.handle("select_test_role", {"server": 2904, "player": 2})["status"], "logged_in")
            self.assertIn("ServerId != 2904", tools.guard)
            with self.assertRaisesRegex(ValueError, "超出配置范围"):
                tools.handle("prepare_environment", {**action, "parameters": {"stage": "closed"}})
            self.assertEqual(tools.handle("prepare_environment", action)["evidence"], "readback 123")
            self.assertFalse(tools.observed)
            tools.record({**first, "state": "defer", "observation": "等待状态同步"})
            tools.record({**second, "state": "running"})
            tools.record({**second, "state": "confirm", "observation": "需要确认"})
            tools.record({**first, "state": "running"})
            self.assertIn("readback 123", (run / "environment.json").read_text(encoding="utf-8"))

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

    def test_agent_can_switch_from_cached_role_to_discovered_role_on_target_server(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_roles": {"2903": [1]}})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            roles = {"in_game": True, "current_server": 2904, "current_player": 1,
                     "login_error": False, "roles_ready": True, "players": [2]}
            tools.bridge.code.side_effect = [roles, roles, roles,
                                            {"status": "role_picker_opened", "server": 2904, "player": 1},
                                            "selected", "confirmed", "logged_in"]
            self.assertEqual(tools.handle("available_test_roles", {})["players"], [2])
            with self.assertRaisesRegex(ValueError, "没有该角色"):
                tools.handle("select_test_role", {"player": 1})
            with patch("worker.time.sleep"):
                result = tools.handle("select_test_role", {"player": 2})
            self.assertEqual(result, {"status": "logged_in", "server": 2903, "player": 2})
            self.assertIn("PlayerID != 2", tools.guard)
            tools.bridge.play.assert_not_called()

    def test_agent_can_use_existing_dev_login_when_the_regular_role_list_is_empty(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_roles": {"2903": [2]}})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            roles = {"in_game": False, "current_server": 0, "current_player": 0, "login_error": False,
                     "roles_ready": False, "players": [], "fast_login_panel": True, "screens": ["UILogin"]}
            tools.bridge.code.side_effect = [roles, "selected", "confirmed", "logged_in"]
            options = tools.handle("available_test_roles", {})
            self.assertEqual((options["players"], options["fast_login_players"]), ([], [2]))
            with self.assertRaisesRegex(ValueError, "已登记"):
                tools.handle("select_test_role", {"player": 3, "fast_login": True})
            with patch("worker.time.sleep"):
                result = tools.handle("select_test_role", {"player": 2, "fast_login": True})
            self.assertEqual(result, {"status": "logged_in", "server": 2903, "player": 2})
            report = (manifest.parent / "environment.json").read_text(encoding="utf-8")
            self.assertIn("身份已验证", report)

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
                {"key": "NK-10786", "cases": [{"id": "无效编号", "title": "检查预览"}]},
                {"key": "NK-9705", "cases": [{"id": "TC-01", "title": "检查半身像"}]},
            ]}, None])
            node.close_unity = Mock()
            process = Mock(pid=314, returncode=0)
            process.poll.side_effect = [None, None, 0]
            job = {"id": 8, "attempt": "test-attempt", "items": [{"key": "NK-10786", "title": "皮肤界面"},
                                                               {"key": "NK-9705", "title": "布阵页"}],
                   "config": {"mode": "branch", "refs": {"client": "dev_NK-9705"},
                              "source": "NK-9705", "server": "2903", "notes": ""}}
            report_plan_error = Mock()

            def launch(*args, **kwargs):
                if args[0][0] == "git":
                    return Popen(*args, **kwargs)
                self.assertTrue((run / "cases.json").is_file())
                self.assertTrue(any(call.args == ("case_snapshot",) for call in node.event.call_args_list))
                self.assertEqual(report_plan_error.call_args.args[0], "NK-10786")
                self.assertIn("第 1 条用例编号无效", report_plan_error.call_args.args[1])
                return process

            def git(args, cwd=None, timeout=30):
                if cwd == skill:
                    return command(args, cwd=cwd, timeout=timeout)
                return Mock(stdout="new portrait")

            def unity(manifest):
                bridge = Mock(manifest=manifest)
                bridge.code.side_effect = ["playing"]
                return bridge

            # Local changes to the skill checkout must never drift into a batch.
            (skill / FUNCTION_REVIEW_RUNTIME[1]).write_text("a different local workflow", encoding="utf-8")
            with patch("worker.command", side_effect=git), patch("worker.UnityBridge", side_effect=unity), \
                    patch("worker.subprocess.Popen", launch):
                snapshot, runtime_rules = node.prepare_function_review()
                self.assertEqual(node.launch_unity(job, "b" * 40, project, run, base, snapshot, runtime_rules,
                                                  report_plan_error), 0)
            node.sync_skill.assert_called_once_with(skill)
            planning, execution = (call.args[0] for call in node.run_claude.call_args_list)
            for path in FUNCTION_REVIEW_RUNTIME:
                self.assertIn(rules[path], planning)
                self.assertIn(rules[path], execution)
            for path in FUNCTION_REVIEW_CASES:
                self.assertIn(rules[path], planning)
            self.assertIn(revision, planning)
            self.assertIn(revision, execution)
            self.assertIn("available_test_roles", execution)
            self.assertNotIn("testPlayer", json.loads((run / "runtime.json").read_text(encoding="utf-8"))["acceptance_scope"])
            self.assertNotIn("a different local workflow", execution)
            manifest = json.loads((run / "runtime.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["function_review"]["revision"], revision)
            self.assertEqual(manifest["acceptance_scope"]["acceptanceMode"], "development")
            with self.assertRaises(ValueError):
                FunctionReview(manifest["function_review"]).read("review-k1-function/references/../../secret.md")
            snapshots = [call.kwargs["cases"] for call in node.event.call_args_list if call.args == ("case_snapshot",)]
            self.assertEqual(snapshots[0], [{"id": "TC-01", "title": "检查半身像", "state": "pending", "evidence": ""}])
            self.assertEqual(snapshots[1][0]["state"], "blocked")
            report_plan_error.assert_called_once()
            reports = json.loads((run / "result.json").read_text(encoding="utf-8"))["items"]
            self.assertEqual(reports["NK-10786"]["result"], "blocked")
            self.assertIn("第 1 条用例编号无效", reports["NK-10786"]["reason"])
            self.assertNotIn("第 1 条用例编号无效", reports["NK-9705"]["reason"])
