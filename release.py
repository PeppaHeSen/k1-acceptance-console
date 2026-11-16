"""Activate a previously reviewed GitHub release from the main server."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


MAX_WORKER_BYTES = 512 * 1024


def atomic_write(path, data, mode=0o600):
    with NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            file.close()
            temporary.unlink(missing_ok=True)


def publish(config, source, commit, notes):
    config = config.resolve()
    source = source.resolve()
    version = (source / "VERSION").read_text(encoding="ascii").strip()
    deployed = Path(__file__).resolve().parent
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("VERSION 无效")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("需要 GitHub Release 对应的完整提交 SHA")
    if not 0 < len(notes.strip()) <= 2000:
        raise ValueError("请输入 1 至 2000 字的更新说明")
    for name in ("VERSION", "server.py", "lan.py", "release.py", "web/index.html", "web/login.html",
                 "web/app.js", "web/appearance.js", "web/styles.css"):
        if (source / name).read_bytes() != (deployed / name).read_bytes():
            raise ValueError(f"主节点尚未部署此次发布的 {name}")
    worker = (source / "worker.py").read_bytes()
    if not 0 < len(worker) <= MAX_WORKER_BYTES:
        raise ValueError("执行节点更新包大小无效")
    compile(worker, "worker.py", "exec")
    sha = hashlib.sha256(worker).hexdigest()
    database = Path(json.loads(config.read_text(encoding="utf-8"))["database"]).resolve()
    directory = database.parent / "releases"
    directory.mkdir(mode=0o700, exist_ok=True)
    manifest = {"version":version, "commit":commit, "notes":notes.strip(), "worker_sha256":sha}
    pointer = directory / "current.json"
    if pointer.is_file():
        previous = json.loads(pointer.read_text(encoding="utf-8"))
        if previous["version"] == version and previous["commit"] != commit:
            raise ValueError("该版本号已对应另一提交，请增加版本号")
    atomic_write(directory / f"{sha}.py", worker)
    atomic_write(pointer, json.dumps(manifest, ensure_ascii=False).encode("utf-8"))
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-config", required=True, type=Path)
    parser.add_argument("--source", required=True, type=Path, help="Verified release checkout")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--notes", required=True, type=Path)
    args = parser.parse_args()
    if not args.server_config.is_file():
        parser.error("请指定主节点现有的 server.json")
    try:
        print(json.dumps(publish(args.server_config,args.source,args.commit,args.notes.read_text(encoding="utf-8")),
                         ensure_ascii=False))
    except (OSError, ValueError, SyntaxError) as error:
        parser.exit(1, str(error)+"\n")
