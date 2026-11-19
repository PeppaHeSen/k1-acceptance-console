"""Run a node's acceptance profile from a local Codex App Server session."""

import argparse
import json
from pathlib import Path
from queue import Empty, Queue
import subprocess
import threading
from uuid import uuid4

from worker import CODEX_THREAD_ID, FunctionReview, Node, codex_executable, save_json, test_servers


class DirectAcceptance(Node):
    def __init__(self, config, snapshot, model, open_app):
        super().__init__(config)
        self.codex = codex_executable(config.get("codex"))
        if not self.codex:
            raise RuntimeError("当前节点未找到 Codex。")
        self.snapshot = snapshot
        self.model = model
        self.open_app = open_app

    def prepare_function_review(self):
        if not self.snapshot:
            return super().prepare_function_review()
        FunctionReview(self.snapshot).read("review-k1-function/SKILL.md")
        self.event("stage_changed", text=f"FunctionReview {self.snapshot['revision']}")
        return self.snapshot

    def event(self, kind, **payload):
        if kind in {"stage_changed", "codex_session", "ai_output", "execution_finished"}:
            print(json.dumps({"kind": kind, **payload}, ensure_ascii=False), flush=True)

    def run_codex(self, prompt, cwd, error_path, manifest=None, progress_callback=None, *, save_session=False):
        settings, env = self.codex_acceptance_config(cwd, manifest)
        trust = f'projects.{json.dumps(str(cwd), ensure_ascii=False)}.trust_level="trusted"'
        args = [self.codex, "app-server", "-c", trust]
        for setting in settings:
            args.extend(["-c", setting])
        configured = subprocess.run([self.codex, "mcp", "list", "--json"], cwd=cwd, check=True,
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
        for server in json.loads(configured.stdout):
            if server["name"] != "k1_unity":
                name = json.dumps(server["name"], ensure_ascii=False)
                args.extend(["-c", f'mcp_servers.{name}={{command="/usr/bin/false",enabled=false}}'])
        with error_path.open("wb") as errors, subprocess.Popen(
                args, cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                text=True, encoding="utf-8", errors="replace") as process:
            try:
                self.app_turn(process, cwd, prompt, progress_callback)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)

    def app_turn(self, process, cwd, prompt, progress_callback):
        def send(method, params=None, identifier=None):
            message = {"method": method, "params": params or {}}
            if identifier is not None:
                message["id"] = identifier
            print(json.dumps(message, ensure_ascii=False), file=process.stdin, flush=True)

        messages = Queue()

        def receive():
            for line in process.stdout:
                messages.put(line)
            messages.put(None)

        threading.Thread(target=receive, daemon=True).start()
        send("initialize", {"clientInfo": {"name": "k1_direct_acceptance", "title": "K1 Direct Acceptance",
                                          "version": "1.0"}}, 1)
        thread = None
        final_text = None
        while True:
            if progress_callback:
                progress_callback()
            try:
                line = messages.get(timeout=.25)
            except Empty:
                continue
            if line is None:
                raise RuntimeError("Codex App Server 未完成即退出。")
            message = json.loads(line)
            identifier, method = message.get("id"), message.get("method")
            if "error" in message and identifier is not None:
                raise RuntimeError("Codex App Server: " + message["error"].get("message", "unknown error"))
            if method and identifier is not None:
                response = {"id": identifier, "error": {"code": -32601, "message": "Approval unavailable"}}
                print(json.dumps(response), file=process.stdin, flush=True)
            elif identifier == 1:
                send("initialized")
                params = {"cwd": str(cwd), "approvalPolicy": "never", "sandbox": "read-only"}
                if self.model:
                    params["model"] = self.model
                send("thread/start", params, 2)
            elif identifier == 2:
                thread = message["result"]["thread"]["id"]
                if not CODEX_THREAD_ID.fullmatch(thread):
                    raise RuntimeError("Codex App Server 未返回有效会话 ID。")
                self.event("codex_session", thread_id=thread, workspace=str(cwd))
                if self.open_app:
                    subprocess.run(["/usr/bin/open", f"codex://threads/{thread}"], check=False, timeout=10)
                send("mcpServer/tool/call", {"threadId": thread, "server": "k1_unity",
                                             "tool": "acceptance_profile", "arguments": {}}, 4)
            elif identifier == 4:
                profile = json.loads(message["result"]["content"][0]["text"])
                save_json(cwd / "verified-profile.json", profile)
                self.event("stage_changed", text=(f"FunctionReview {profile['function_review_revision']} · "
                                                 f"Unity MCP {profile['unity_mcp_schema']}"))
                send("turn/start", {"threadId": thread, "input": [{"type": "text", "text": prompt}]}, 3)
            elif method == "item/completed":
                item = message["params"]["item"]
                if item.get("type") == "agentMessage":
                    final_text = item.get("text")
                if item.get("type") == "mcpToolCall" and item.get("tool") in {"acceptance_profile", "record_view"}:
                    self.event("stage_changed", text=json.dumps(item.get("result"), ensure_ascii=False)[:1800])
            elif method == "turn/completed":
                turn = message["params"]["turn"]
                if turn.get("status") != "completed" or not final_text:
                    error = turn.get("error") or {}
                    raise RuntimeError(f"Codex App Server 会话结束状态为 {turn.get('status')}。{error.get('message', '')}")
                save_json(cwd / "final.json", {"thread_id": thread, "output": final_text})
                self.event("ai_output", text=final_text)
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--open-app", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.node.read_text(encoding="utf-8"))
    task = json.loads(args.task.read_text(encoding="utf-8"))
    job = task["job"]
    job["attempt"] = uuid4().hex
    if (job["config"]["server"] not in test_servers(config) or job["config"].get("writeback_enabled") or
            not job["config"].get("final_output_only")):
        raise ValueError("直接验收需要本节点已启用的测试服，进包表回写由队列批次承接。")
    node = DirectAcceptance(config, task.get("function_review"), task.get("model"), args.open_app)
    node.ledger = node.state / "direct" / job["attempt"] / "attempt.json"
    node.ledger.parent.mkdir(mode=0o700, parents=True)
    node.attempt = {"id": job["attempt"], "job": job["id"], "runner": "codex_app",
                    "final_output_only": True, "pending": [], "seq": 0}
    node.persist()
    node.execute_unity(job, Path(task["root"]))


if __name__ == "__main__":
    main()
