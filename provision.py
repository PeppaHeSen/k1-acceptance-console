"""Create private runtime files for one operator and optionally a co-located Mac node."""

import argparse
import json
import os
from pathlib import Path
import secrets

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
    parser.add_argument("--claude")
    args = parser.parse_args()
    root = args.directory.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
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
                    claude=args.claude, methods=["Unity"])
        private_file(root / "node.json", node)
    print(f"Runtime configuration saved to {root}")


if __name__ == "__main__":
    main()
