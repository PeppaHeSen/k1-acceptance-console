from contextlib import nullcontext
import io
import json
from pathlib import Path
from subprocess import Popen
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from server import Console

from worker import (FUNCTION_REVIEW_CASES, FUNCTION_REVIEW_RUNTIME, UNITY_RUNTIME_CAPABILITY, FunctionReview, Node,
                    UnityBridge, UnityTools, acceptance_checkout, command, resolve_repository, save_json,
                    serve_unity_tools)


class AcceptanceCheckoutTest(unittest.TestCase):
    def test_login_view_encodes_screenshot_as_mcp_image(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {})
            request = {"id": 1, "method": "tools/call", "params": {"name": "login_view"}}
            reply = io.StringIO()
            with patch("worker.sys.stdin", io.StringIO(json.dumps(request))), patch("worker.sys.stdout", reply), \
                    patch("worker.UnityTools") as tools:
                tools.return_value.handle.return_value = ({"language": "login"}, b"png")
                serve_unity_tools(manifest)
            content = json.loads(reply.getvalue())["result"]["content"]
            self.assertEqual(content[1], {"type": "image", "data": "cG5n", "mimeType": "image/png"})

    def test_codex_exec_shares_the_unity_tools_and_reports_live_progress(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            node = Node.__new__(Node)
            node.config = {"url": "https://example.test", "id": "node", "token": "test-token"}
            node.codex = "codex"
            node.attempt = {"runner": "codex", "pack_writeback": True}
            node.event = Mock()
            node.persist = Mock()
            node.check_stopped = Mock()
            node.watch_stop = Mock(return_value=nullcontext())
            callback = Mock()
            task = root / "runtime.json"
            events = [
                {"type": "thread.started", "thread_id": "019b2acb-764a-7c54-8f32-fbaedd6ba073"},
                {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "k1_unity",
                                                "tool": "select_test_role"}},
                {"type": "item.completed", "item": {"type": "mcp_tool_call"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "角色已登录"}},
                {"type": "turn.completed"},
            ]
            process = Mock(pid=7, stdout=map(json.dumps, events))
            process.wait.return_value = 0
            with patch("worker.subprocess.Popen", return_value=nullcontext(process)) as popen:
                node.run_ai("开始验收", root, root / "claude-runtime.stderr", manifest=task,
                            progress_callback=callback)
            args = popen.call_args.args[0]
            config = next(arg for arg in args if arg.startswith("mcp_servers="))
            self.assertIn(config, (root / ".codex" / "config.toml").read_text(encoding="utf-8"))
            self.assertIn("pack_item", config)
            self.assertIn("--unity-tools", config)
            self.assertNotIn("test-token", config)
            self.assertIn("K1_CONSOLE_PACK_SESSION", popen.call_args.kwargs["env"])
            self.assertIn("--ignore-user-config", args)
            node.event.assert_any_call("ai_output", text="角色已登录", continuation=False)
            self.assertEqual(callback.call_count, 3)
            self.assertTrue((root / "codex-runtime.stderr").exists())
            self.assertIn("--ephemeral", args)
            node.event.reset_mock()
            node.attempt["runner"] = "codex_app"
            process.stdout = map(json.dumps, events)
            with patch("worker.subprocess.Popen", return_value=nullcontext(process)) as popen:
                node.run_ai("开始验收", root, root / "claude-runtime.stderr", manifest=task,
                            progress_callback=callback)
            self.assertNotIn("--ephemeral", popen.call_args.args[0])
            node.event.assert_any_call("codex_session", thread_id=events[0]["thread_id"])
            node.event.assert_any_call("ai_output", text="角色已登录", continuation=False)
            node.event.reset_mock()
            node.attempt.update(final_output_only=True, pack_writeback=False)
            last_message = {"type": "item.completed", "item": {"type": "agent_message", "text": "本轮结论"}}
            process.stdout = map(json.dumps, [*events[:-1], last_message, events[-1]])
            with patch("worker.subprocess.Popen", return_value=nullcontext(process)) as popen:
                node.run_ai("开始验收", root, root / "claude-runtime.stderr", manifest=task)
            self.assertNotIn("register_cases", repr(popen.call_args.args[0]))
            self.assertEqual([call.kwargs["text"] for call in node.event.call_args_list if call.args == ("ai_output",)],
                             ["本轮结论"])
            node.event.reset_mock()
            node.attempt["reporting"] = "output"
            process.stdout = map(json.dumps, [*events[:-1], last_message, events[-1]])
            with patch("worker.subprocess.Popen", return_value=nullcontext(process)):
                node.run_ai("开始验收", root, root / "claude-runtime.stderr", manifest=task)
            self.assertEqual([call.kwargs for call in node.event.call_args_list if call.args == ("ai_output",)], [
                {"text": "角色已登录", "continuation": False},
                {"text": "本轮结论", "continuation": False},
                {"text": "本轮结论", "continuation": False, "final": True},
            ])

    def test_claude_emits_complete_text_blocks_and_marks_long_continuations(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            node = Node.__new__(Node)
            node.config = {}
            node.attempt = {}
            node.claude = "claude"
            node.event = Mock()
            node.persist = Mock()
            node.check_stopped = Mock()
            node.watch_stop = Mock(return_value=nullcontext())

            def stream(kind, **data):
                return {"type": "stream_event", "event": {"type": kind, **data}}

            def delta(text):
                return stream("content_block_delta", delta={"type": "text_delta", "text": text})

            long_text = "检查记录\n" * 3201
            events = [delta("雪夜铃"), {"type": "user"}, delta("屋预览正常。"), stream("content_block_stop"),
                      delta(long_text), stream("content_block_stop"),
                      {"type": "assistant", "message": {"content": [{"type": "text", "text": long_text}]}},
                      {"type": "result", "result": "雪夜铃屋预览正常。"}]
            process = Mock(pid=7, stdout=map(json.dumps, events))
            process.wait.return_value = 0
            with patch("worker.subprocess.Popen", return_value=nullcontext(process)), \
                    patch("worker.threading.Timer") as timer:
                node.run_claude("开始验收", root, root / "claude.stderr", manifest=root / "runtime.json")
            timer.assert_not_called()
            node.watch_stop.assert_called_once_with(process)

            output = [call.kwargs for call in node.event.call_args_list]
            self.assertEqual(output, [
                {"text": "雪夜铃屋预览正常。", "continuation": False},
                {"text": long_text[:16000], "continuation": False},
                {"text": long_text[16000:], "continuation": True},
            ])

    def test_worker_attaches_to_open_editor_without_switching_commits_or_closing_it(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / "k1_client" / "client"
            lock = project / "Temp" / "UnityLockfile"
            lock.parent.mkdir(parents=True)
            lock.touch()
            settings = project / "ProjectSettings"
            settings.mkdir()
            (settings / "ProjectVersion.txt").touch()
            (root / "runtime").mkdir()
            node = Node.__new__(Node)
            node.state = root
            node.config = {"unity_test_servers": ["2903"]}
            node.attempt = {"id": "test"}
            node.cancel_attempt = None
            node.event = Mock()
            node.persist = Mock()
            node.prepare_function_review = Mock(return_value={"revision": "skill"})
            node.unity_prompt = Mock(return_value="run acceptance")
            def use_existing(prompt, cwd, error_path, *, manifest, progress_callback):
                self.assertEqual(UnityTools(manifest).handle("start_unity", {}), {"status": "attached"})
                progress_callback()

            node.run_claude = Mock(side_effect=use_existing)
            node.close_unity = Mock()
            actual, target = "a" * 40, "b" * 40
            job = {"id": 8, "attempt": "test", "items": [{"key": "NK-9705", "title": "布阵页"}],
                   "config": {"mode": "version", "refs": {"client": "dev"},
                              "source": "", "server": "2903", "notes": ""}}
            with patch("worker.resolve_repository", return_value=project.parent), \
                    patch("worker.command", side_effect=[Mock(stdout=target), Mock(stdout=actual),
                                                         Mock(stdout=" M client/file.cs")]) as git, \
                    patch("worker.acceptance_checkout") as checkout, patch("worker.subprocess.Popen") as popen:
                node.execute_unity(job, root)
            self.assertEqual([call.args[0][1] for call in git.call_args_list],
                             ["ls-remote", "rev-parse", "--no-optional-locks"])
            checkout.assert_not_called()
            popen.assert_not_called()
            node.close_unity.assert_not_called()
            node.run_claude.assert_called_once()
            self.assertTrue(lock.exists())
            manifest = json.loads((root / "runtime/test/runtime.json").read_bytes())
            self.assertEqual(manifest["client"], actual)
            self.assertEqual(manifest["acceptance_scope"]["existingEditor"], {"target": target, "modified": True})
            result = json.loads((root / "runtime/test/result.json").read_bytes())
            self.assertEqual(result["client"], actual)
            self.assertIn("含本地跟踪文件改动", result["items"]["NK-9705"]["reason"])

    def test_unity_connection_reports_identification_separately_from_tool_timeout(self):
        bridge = UnityBridge({"project": "/acceptance/client", "ports": [21891, 21892, 21893]})
        with patch.object(bridge, "request", side_effect=[
            "/another/client/Assets", ConnectionRefusedError(), bridge.assets, TimeoutError()
        ]) as request:
            with self.assertRaisesRegex(ConnectionError, "21893 已识别本轮工程，调试操作请求失败（等待响应超时）"):
                bridge.invoke({"member": "HandleCommand"})
        self.assertEqual([call.args[0] for call in request.call_args_list], [21891, 21892, 21893, 21893])

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
            tools.bridge.code.side_effect = [{"in_game": False, "current_server": 0, "current_player": 0,
                                             "roles_ready": True, "players": [2]}, "logged_in", True]
            options = tools.handle("environment_options", {})
            self.assertEqual(options["actions"]["scenario"]["player"], 2)
            self.assertNotIn("command", options["actions"]["scenario"])
            first = {"item": "NK-1", "case": "TC-1"}
            second = {"item": "NK-1", "case": "TC-2"}
            action = {"action": "scenario", "reason": "缺前置", "parameters": {"stage": "active"}}
            tools.record({**first, "state": "running"})
            with self.assertRaisesRegex(ValueError, "测试服和角色"):
                tools.handle("prepare_environment", action)
            with self.assertRaisesRegex(ValueError, "已授权环境动作"):
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

    def test_gm_uses_current_login_and_does_not_require_configured_actions(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"project": temp, "server": "2903"})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            tools.bridge.code.side_effect = ["logged_in", {"status": "loading"},
                {"status": "ready", "commands": [{"command": "addasset"}]}, "sent", "sent", "sent"]
            gm = {"command": "addasset", "scope": "player", "arguments": ["item", "42", "1"],
                  "operation": "seed-box", "reason": "验证实际兑换"}
            with self.assertRaisesRegex(ValueError, "先开始"):
                tools.handle("run_test_gm", gm)
            tools.active = ("NK-10944", "ITEM-01")
            with self.assertRaisesRegex(ValueError, "先登录"):
                tools.handle("run_test_gm", gm)
            with patch.object(tools, "available_test_roles", return_value={"players": [266341]}):
                self.assertEqual(tools.select_test_role({"server": 2903, "player": 266341})["status"], "logged_in")
            self.assertEqual(tools.environment_options()["actions"], {})
            self.assertEqual(tools.environment_options()["unity_gm"]["tool"], "run_test_gm")
            self.assertEqual(tools.handle("test_gm_help", {"query": "addasset"})["status"], "loading")
            self.assertEqual(tools.handle("test_gm_help", {"query": "addasset"})["commands"][0]["command"], "addasset")
            sent = tools.handle("run_test_gm", gm)
            self.assertEqual(sent, {"status": "sent", "server": 2903, "player": 266341, "operation": "seed-box"})
            code = tools.bridge.code.call_args.args[0]
            self.assertIn("ServerId != 2903", code)
            self.assertIn("PlayerID != 266341", code)
            self.assertIn("Logic.LoginMgrNew.I.IsLogin", code)
            self.assertIn("tokens.Insert(0, Logic.LPlayer.I.PlayerID.ToString())", code)
            self.assertIn('UI.UIGM.SendGMReq("addasset", false, tokens.ToArray());', code)
            self.assertEqual(tools.handle("run_test_gm", gm), sent)
            self.assertEqual(tools.bridge.code.call_count, 4)
            with self.assertRaisesRegex(ValueError, "不同的 GM"):
                tools.handle("run_test_gm", {**gm, "arguments": ["item", "42", "2"]})
            tools.handle("run_test_gm", {**gm, "command": "actv", "subcommand": "newActv", "scope": "server",
                                         "arguments": ["10"], "operation": "open-activity"})
            code = tools.bridge.code.call_args.args[0]
            self.assertIn('{ "newActv", "10" }', code)
            self.assertNotIn("tokens.Insert", code)
            tools.handle("run_test_gm", {**gm, "command": "actv", "subcommand": "rtAddCard",
                                         "arguments": ["10", "1"], "operation": "seed-card"})
            self.assertIn("tokens.Insert(1, Logic.LPlayer.I.PlayerID.ToString())", tools.bridge.code.call_args.args[0])
            self.assertIn("GM 已发送", (Path(temp) / "environment.json").read_text(encoding="utf-8"))

    def test_gm_does_not_retry_an_uncertain_dispatch(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"project": temp, "server": "2903"})
            tools = UnityTools(manifest)
            tools.player = 266341
            tools.active = ("NK-10944", "ITEM-02")
            tools.bridge = Mock()
            tools.bridge.code.side_effect = ["help_pending", None, ConnectionError("reply lost")]
            gm = {"command": "addasset", "scope": "player", "arguments": ["item", "42", "2"],
                  "operation": "seed-box", "reason": "构造持有道具的状态"}
            self.assertEqual(tools.handle("run_test_gm", gm)["status"], "help_pending")
            with self.assertRaisesRegex(RuntimeError, "登录状态已变"):
                tools.handle("run_test_gm", gm)
            self.assertEqual(tools.handle("run_test_gm", gm)["status"], "unconfirmed")
            self.assertEqual(tools.handle("run_test_gm", gm)["status"], "unconfirmed")
            tools.server = 2904
            with self.assertRaisesRegex(ValueError, "批次测试服"):
                tools.handle("run_test_gm", gm)
            self.assertEqual(tools.bridge.code.call_count, 3)

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

    def test_final_only_game_click_uses_current_screenshot_and_target_guard(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_player": 1, "final_output_only": True})
            tools = UnityTools(manifest)
            tools.bridge.code = Mock(return_value={"status": "dispatched", "route": "unity_pointer"})
            with self.assertRaisesRegex(ValueError, "capture_view"):
                tools.handle("click_game", {"x": 0, "y": 0})
            tools.game_view_size = (1080, 2160)
            with self.assertRaisesRegex(ValueError, "1080×2160"):
                tools.handle("click_game", {"x": 1080, "y": 0})
            self.assertEqual(tools.handle("click_game", {"x": 1079, "y": 2159}),
                             {"status": "dispatched", "route": "unity_pointer", "x": 1079, "y": 2159})
            code = tools.bridge.code.call_args.args[0]
            self.assertIn("ServerId != 2903", code)
            self.assertIn("Vector2(1079 + 0.5f, 2160 - (2159 + 0.5f))", code)
            self.assertIn('GetProperty("targetRenderSize", flags)', code)
            self.assertIn("system.RaycastAll(pointer, hits)", code)
            self.assertIn("map.UpdateMapTapFilter(pointer, entity)", code)
            with self.assertRaisesRegex(ValueError, "capture_view"):
                tools.handle("click_game", {"x": 0, "y": 0})
            tools.bridge.code.assert_called_once()

    def test_continuous_game_recording_reports_observed_intervals_and_samples(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_player": 1, "final_output_only": True})
            tools = UnityTools(manifest)
            tools.bridge.code = Mock(return_value=True)
            clip = tools.handle("record_view", {"action": "start", "seconds": 30, "fps": 24})["clip"]
            folder = tools.recording_dir(clip)
            self.assertIn("WaitForEndOfFrame", tools.bridge.code.call_args.args[0])
            for index, ms in enumerate((40, 80, 130), 1):
                (folder / f"frame-{index:05d}-{ms:06d}.jpg").write_bytes(bytes([index]))
            status = tools.handle("record_view", {"action": "stop", "clip": clip})
            self.assertEqual((status["frames"], status["max_gap_ms"]), (3, 50))
            info, images = tools.handle("sample_recording", {"clip": clip, "start": 2})
            self.assertEqual(info["frames"], [{"frame": 2, "ms": 80}, {"frame": 3, "ms": 130}])
            self.assertEqual(images, [b"\x02", b"\x03"])

    def test_auto_server_uses_online_candidate_and_preserves_unavailable_reason(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "", "project": temp, "server_candidates": ["2903", "3020"],
                                 "acceptance_scope": {"server": ""}})
            tools = UnityTools(manifest)
            self.assertEqual(tools.environment_options()["test_server_candidates"], [2903, 3020])
            with self.assertRaisesRegex(ValueError, "选定"):
                tools.select_test_role({"server": 2903, "player": 1})
            with self.assertRaisesRegex(ValueError, "候选"):
                tools.choose_test_server({"server": 9999, "reason": "未登记"})
            tools.available_test_roles = Mock(return_value={"roles_ready": True, "server_listed": True, "server_online": False})
            with self.assertRaisesRegex(ValueError, "2903 服.*离线"):
                tools.choose_test_server({"server": 2903, "reason": "尝试"})
            tools.choose_test_server({"reason": "2903 离线，3020 尚未上线"})
            self.assertEqual(UnityTools(manifest).selection["reason"], "2903 离线，3020 尚未上线")
            tools.available_test_roles.return_value["server_online"] = True
            tools.choose_test_server({"server": 3020, "reason": "在线且可用"})
            tools.lock_test_server()
            resumed = UnityTools(manifest)
            self.assertEqual(resumed.target_server(), 3020)
            with self.assertRaisesRegex(ValueError, "不能切换"):
                resumed.choose_test_server({"server": 2903, "reason": "切换"})

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

    def test_agent_creates_a_role_through_the_target_server_confirmation(self):
        with TemporaryDirectory() as temp:
            manifest = Path(temp) / "runtime.json"
            save_json(manifest, {"server": "2903", "project": temp, "test_roles": {"2903": [2]}})
            tools = UnityTools(manifest)
            tools.bridge = Mock()
            roles = {"in_game": False, "current_server": 0, "current_player": 0, "login_error": False,
                     "roles_ready": True, "players": [], "server_listed": True, "server_online": True}
            identity = {"status": "logged_in", "server": 2903, "player": 3}
            tools.bridge.code.side_effect = [roles, {"status": "account_loading"},
                                            {"status": "confirmation_pending"}, {"status": "submitted"},
                                            {"status": "loading"}, identity]
            options = tools.handle("available_test_roles", {})
            self.assertEqual(options["players"], [])
            self.assertNotIn("fast_login_players", options)
            for status in ("account_loading", "confirmation_pending", "submitted", "loading"):
                self.assertEqual(tools.handle("create_test_role", {}), {"status": status})
                self.assertIsNone(tools.player)
            self.assertEqual(tools.handle("create_test_role", {}), identity)
            self.assertIn("PlayerID != 3", tools.guard)
            scripts = [call.args[0] for call in tools.bridge.code.call_args_list]
            self.assertIn("if (true) {", scripts[3])
            self.assertIn('if (true) return new {status = "loading"}', scripts[4])
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

    def test_one_agent_registers_and_reports_cases_during_unity_run(self):
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
            node.config["unity_environment_actions"] = {
                name: {"kind": kind, "server": server, "command": [sys.executable, name], "description": name}
                for name, kind, server in (("config", "refresh_config", 2903), ("deploy", "deploy_server", 2903),
                                           ("other-server", "deploy_server", 2904))
            }
            node.attempt = {"id": "test-attempt"}
            node.cancel_attempt = None
            node.persist = Mock()
            node.watch_stop = lambda _: nullcontext()
            node.check_skill = lambda: skill
            node.sync_skill = Mock(return_value=(skill, revision, True))
            node.event = Mock()
            node.close_unity = Mock()
            process = Mock(pid=314, returncode=0)
            process.poll.return_value = 0
            job = {"id": 8, "attempt": "test-attempt", "items": [{"key": "NK-10786", "title": "皮肤界面"},
                                                               {"key": "NK-9705", "title": "布阵页"}],
                   "config": {"mode": "branch", "refs": {"client": "dev_NK-9705", "game": "dev", "data": "qa"},
                              "source": "NK-9705", "server": "2903", "notes": "",
                              "auto_refresh_config": True, "auto_deploy": True}}
            editor_started = False
            bridges = []

            def launch(*args, **kwargs):
                nonlocal editor_started
                if args[0][0] == "git":
                    return Popen(*args, **kwargs)
                editor_started = True
                return process

            def execute(prompt, cwd, error_path, *, manifest, progress_callback):
                self.assertFalse(editor_started)
                bridges[0].code.assert_not_called()
                bridges[0].play.assert_not_called()
                self.assertFalse((run / "cases.json").exists())
                tools = UnityTools(manifest)
                self.assertEqual(set(tools.handle("environment_options", {})["actions"]), {"config", "deploy"})
                with patch("worker.subprocess.run") as action:
                    action.side_effect = [Mock(stdout=json.dumps({"status": status, "evidence": status}))
                                          for status in ("pending", "ready", "ready")]
                    refresh = {"action": "config", "reason": "构建本轮配置"}
                    self.assertEqual(tools.handle("prepare_environment", refresh)["status"], "pending")
                    self.assertEqual(tools.handle("prepare_environment", refresh)["status"], "ready")
                    self.assertEqual(tools.handle("prepare_environment", {
                        "action": "deploy", "reason": "启动批次测试服"})["status"], "ready")
                    first, poll, deploy = [json.loads(call.kwargs["input"]) for call in action.call_args_list]
                    self.assertEqual(first["operation"], poll["operation"])
                    self.assertIsNone(first["case"])
                    self.assertEqual(deploy["server"], 2903)
                    self.assertEqual(deploy["refs"], job["config"]["refs"])
                    tools.manifest["acceptance_scope"]["batch_preparation"]["auto_refresh_config"] = False
                    with self.assertRaisesRegex(ValueError, "未开启"):
                        tools.handle("prepare_environment", refresh)
                    self.assertEqual(action.call_count, 3)
                self.assertEqual(tools.handle("start_unity", {}), {"status": "requested"})
                progress_callback()
                self.assertTrue(editor_started)
                self.assertEqual(tools.handle("start_unity", {}), {"status": "started"})
                tools.bridge.code.side_effect = [ConnectionError("等待 Editor Debug MCP"),
                                                 {"compiling": True, "playing": False},
                                                 "compiling", "dirty", "ready", {"playing": True}]
                self.assertEqual(tools.handle("editor_status", {}),
                                 {"status": "unavailable", "detail": "等待 Editor Debug MCP"})
                with self.assertRaisesRegex(ValueError, "编号无效"):
                    tools.handle("register_cases", {"item": "NK-10786",
                                                   "cases": [{"id": "无效编号", "title": "检查预览"}]})
                tools.handle("report_item_blocked", {"item": "NK-10786", "reason": "缺预览规则"})
                item = "NK-9705"
                first = {"item": item, "case": "TC-01"}
                tools.handle("register_cases", {"item": item, "cases": [{"id": "TC-01", "title": "检查半身像"}]})
                progress_callback()
                self.assertTrue(any(call.args == ("case_snapshot",) for call in node.event.call_args_list))
                with patch("worker.time.sleep") as wait:
                    state = tools.handle("editor_status", {"wait_seconds": 10})
                self.assertTrue(state["editor"]["compiling"])
                wait.assert_called_once_with(10)
                self.assertEqual(tools.handle("enter_test_play", {}), {"status": "compiling"})
                self.assertEqual(tools.handle("enter_test_play", {}), {"status": "dirty"})
                tools.bridge.play.assert_not_called()
                self.assertEqual(tools.handle("enter_test_play", {}), {"status": "starting_play"})
                tools.bridge.play.assert_called_once_with(True)
                self.assertTrue(tools.handle("editor_status", {})["editor"]["playing"])
                tools.record({**first, "state": "running"})
                tools.record({**first, "state": "confirm", "observation": "待核对预期"})
                tools.handle("register_cases", {"item": item, "cases": [
                    {"id": "TC-01", "title": "检查半身像"}, {"id": "TC-02", "title": "检查候选列表"}]})
                progress_callback()

            node.run_claude = Mock(side_effect=execute)

            def git(args, cwd=None, timeout=30):
                if cwd == skill:
                    return command(args, cwd=cwd, timeout=timeout)
                return Mock(stdout="new portrait")

            def unity(manifest):
                bridge = Mock(manifest=manifest)
                bridges.append(bridge)
                return bridge

            # Local changes to the skill checkout must never drift into a batch.
            (skill / FUNCTION_REVIEW_RUNTIME[1]).write_text("a different local workflow", encoding="utf-8")
            with patch("worker.command", side_effect=git), patch("worker.UnityBridge", side_effect=unity), \
                    patch("worker.subprocess.Popen", launch):
                snapshot = node.prepare_function_review()
                self.assertEqual(node.launch_unity(job, "b" * 40, project, run, snapshot), 0)
            node.sync_skill.assert_called_once_with(skill)
            node.run_claude.assert_called_once()
            execution = node.run_claude.call_args.args[0]
            self.assertIn("review-k1-function/SKILL.md", execution)
            self.assertIn("batch_context", execution)
            self.assertIn(str(project), execution)
            self.assertLess(len(execution), 600)
            for path in (*FUNCTION_REVIEW_RUNTIME, *FUNCTION_REVIEW_CASES):
                self.assertNotIn(rules[path], execution)
                self.assertEqual(FunctionReview(snapshot).read(path), rules[path])
            self.assertNotIn("testPlayer", json.loads((run / "runtime.json").read_text(encoding="utf-8"))["acceptance_scope"])
            self.assertNotIn("a different local workflow", execution)
            manifest = json.loads((run / "runtime.json").read_text(encoding="utf-8"))
            tools = UnityTools(run / "runtime.json")
            self.assertEqual(tools.handle("batch_context", {})["items"], manifest["items"])
            with self.assertRaisesRegex(ValueError, "不在本批次"):
                tools.handle("batch_context", {"item": "NK-0"})
            with patch("worker.command", return_value=Mock(stdout="关联提交")) as git:
                context = tools.handle("batch_context", {"item": "NK-9705"})
            self.assertEqual(context["changes"], "关联提交")
            self.assertIn(manifest["client"], git.call_args.args[0])
            self.assertEqual(manifest["function_review"]["revision"], revision)
            self.assertEqual(manifest["acceptance_scope"]["acceptanceMode"], "development")
            with self.assertRaises(ValueError):
                FunctionReview(manifest["function_review"]).read("review-k1-function/references/../../secret.md")
            snapshots = [call.kwargs["cases"] for call in node.event.call_args_list if call.args == ("case_snapshot",)]
            self.assertEqual(snapshots[0], [{"id": "TC-01", "title": "检查半身像", "state": "pending", "evidence": ""}])
            self.assertEqual([c["state"] for c in snapshots[1]], ["confirm", "pending"])
            self.assertEqual([c["state"] for c in snapshots[-1]], ["confirm", "blocked"])
            reports = json.loads((run / "result.json").read_text(encoding="utf-8"))["items"]
            self.assertEqual(reports["NK-10786"]["result"], "blocked")
            self.assertEqual(reports["NK-10786"]["reason"], "缺预览规则")
            self.assertIn("待核对预期", reports["NK-9705"]["reason"])
