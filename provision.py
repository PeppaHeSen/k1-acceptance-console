"""Create private runtime files for one operator and optionally a co-located Mac node."""

import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from k1_sheet import Workbook
from server import Console, DOCUMENT


def private_file(path, content):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                   "w", encoding="utf-8") as file:
        json.dump(content, file, ensure_ascii=False, indent=2)
        file.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--operator", default="operator")
    parser.add_argument("--dws", default="dws")
    parser.add_argument("--port", type=int, default=8082)
    parser.add_argument("--node-root", type=Path)
    parser.add_argument("--unity-editor", type=Path)
    parser.add_argument("--unity-runtime", action="store_true",
                        help="Enable isolated Unity branch runs on a Mac with a desktop session")
    parser.add_argument("--claude")
    parser.add_argument("--join-url", help="HTTPS address of the main server for a node-only install")
    parser.add_argument("--node-id", help="Use an existing manually issued node identifier")
    parser.add_argument("--ca-cert", type=Path, help="Public trust certificate for the main server")
    parser.add_argument("--method", choices=["Unity", "真机"], action="append")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.join_url:
        url = urlsplit(args.join_url)
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            parser.error("--join-url must be the main server HTTPS origin")
        if args.ca_cert and not args.ca_cert.is_file():
            parser.error("--ca-cert must point to a readable certificate file")
        token = (getpass.getpass("Node token issued by the main server: ").strip()
                 if args.node_id else secrets.token_urlsafe(32))
        if not token:
            parser.error("The node token is required")
        private_file(root / "node.json", {
            "id": args.node_id, "token": token, "url": args.join_url.rstrip("/"),
            "state": str(root / "node-state"), "skill": str(root / "K1FunctionReview"),
            "skill_remote": "https://github.com/RehearsalTap4Fun/K1FunctionReview.git",
            "unity_editor": str(args.unity_editor.resolve()) if args.unity_editor else None,
            "claude": args.claude, "methods": args.method or ["Unity"], "unity_runtime": args.unity_runtime,
            "ca_cert": str(args.ca_cert.resolve()) if args.ca_cert else None,
        })
        print(f"Node configuration saved to {root / 'node.json'}")
        return
    config = {"operator": args.operator, "password": secrets.token_urlsafe(32),
              "database": str(root / "console.sqlite3"), "port": args.port, "dws": args.dws}
    private_file(root / "server.json", config)
    if args.node_root:
        app = Console(config["database"], config, Workbook(args.dws, DOCUMENT))
        node = app.create_node({"name": "Mac M4", "os": "macOS"})
        app.configure_node(node["id"], {"root": str(args.node_root.absolute())})
        node.update(url=f"http://127.0.0.1:{args.port}", state=str(root / "node-state"),
                    skill=str(root / "K1FunctionReview"),
                    skill_remote="https://github.com/RehearsalTap4Fun/K1FunctionReview.git",
                    unity_editor=str(args.unity_editor) if args.unity_editor else None,
                    claude=args.claude, methods=["Unity"], unity_runtime=args.unity_runtime)
        private_file(root / "node.json", node)
    print(f"Runtime configuration saved to {root}")


if __name__ == "__main__":
    main()
