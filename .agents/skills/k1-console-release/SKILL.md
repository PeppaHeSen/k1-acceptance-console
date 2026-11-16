---
name: k1-console-release
description: 发布 K1 验收台新版本，创建 GitHub Release，部署主节点并通知内网网页与 Mac 或 Windows 执行节点更新。
---

# 发布 K1 验收台

在 `k1-acceptance-console` 仓库中执行。先读根目录的 `K1验收台-Agent部署说明.md`，确认目标环境、运行目录和现有进程管理方式。版本号取自仓库根目录的 `VERSION`。普通提交和推送只更新源码，发布通知由主节点的 `release.py` 启用。

1. 确认本轮要发布的提交和版本。检查工作区和目标远端，核对版本号对应唯一的提交。准备本次更新说明，运行 `python -m unittest discover -s tests -v`、`python -m py_compile server.py lan.py worker.py provision.py release.py` 和 `node --check web/app.js`。已经发布的版本应核对标签和发布对象后继续完成剩余步骤。
2. 将该提交推送到用户指定的仓库和分支。在该提交建立 `v<版本号>` 标签，用 GitHub Release 发布该标签和说明。回读标签实际指向的完整 SHA。发布凭据使用当前授权的 GitHub 会话。
3. 按部署说明将同一提交部署到主节点。在维护窗口内处理主节点服务重启，保留既有数据库、DWS 会话、节点配置和 HTTPS 证书。Mac 的 DWS 依赖登录会话时，沿用已验证能读表的启动方式。回读 HTTPS 首页，验证验收队列、真实页签与节点心跳。老节点首次使用更新功能需要先部署支持更新的 Worker。
4. 在主节点运行本次部署目录中的 `release.py`，激活内网通知。

   ```sh
   python3 release.py --server-config <运行目录>/server.json \
     --source <已验证的发布源目录> --commit <完整SHA> --notes <更新说明文件>
   ```

   发布源可以是该标签的检出目录或对应源码归档。命令将更新包和版本清单写到验收数据库旁的 `releases/`，运行目录内的配置不随发布包传输。
5. 通过正式 HTTPS 入口回读 `/api/state` 的 `release`，核对版本、提交和更新包摘要。浏览器的「刷新到新版」保留当前批次位置。执行节点的更新按钮登记已发布版本，节点会在当前任务结束后替换 Worker 并重启；节点重新签到并显示「已更新」才算安装完成。报告 GitHub Release、内网入口和节点更新状态。

GitHub 或部署验证失败时停在该步骤并留下可续跑的信息。需要重试时复用同一个标签和确认过的提交。
