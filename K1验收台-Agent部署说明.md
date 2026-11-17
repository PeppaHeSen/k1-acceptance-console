# K1 验收台 Agent 部署说明

本仓库可部署主服务器和执行节点。主服务器提供网页、钉钉进包表和队列，macOS 或 Windows 节点连接该服务器执行批次。当前版本支持只读链路联调，包括真实选单、接单和 Claude Code 输出。

把本文件和目标机器信息交给 Agent。说明要部署「主服务器」还是「执行节点」，以及是否允许它通过 SSH 操作目标机。主服务器需要内网地址和部署目录。执行节点需要主服务器的 HTTPS 地址、本机部署目录、三端工程根目录和要使用的验收方式。复用机器上已有的登录方式。

## 部署主服务器

1. 检出仓库，确认当前提交。自定义硬盘先核对挂载点和可写性。把运行目录放在代码目录之外，升级时复用已有数据库和配置。
2. 检查 Python 3.9+、Git 和 DWS。用运行网页服务的账号实测 `dws sheet list --node <进包文档地址> --format json`。如果 Mac 的 DWS 登录态保存在钥匙串，从能读表的 Mac 登录终端启动服务。
3. 首次运行 `python3 provision.py --directory <运行目录>`。配置文件会生成供 HTTPS 入口连接本机后端的凭据。主机也承担验收时，可以加 `--node-root <本机根目录> --unity-editor <Unity可执行文件>` 来登记同机 Mac 节点。Windows 上可使用 `py -3` 代替 `python3`。
4. 启动后端 `python3 server.py --config <运行目录>/server.json`。后端监听 `127.0.0.1:8082`。如已配置同机节点，再启动 `python3 worker.py --config <运行目录>/node.json`。
5. 使用已签发的内网证书时，将证书链和私钥路径传给 HTTPS 入口。主机 IP 需包含在证书的 SAN 中。没有现成证书时，可以加 `--generate-cert`，为指定 IP 生成单主机证书。这种情况下，访问者需要通过可信渠道确认并信任该证书。

```bash
python3 lan.py --bind <主机内网IP> --port 8443 --upstream-port 8082 \
  --server-config <运行目录>/server.json \
  --cert <运行目录>/https/console.pem --key <运行目录>/https/console.key
```

部署记录应包含实际 URL、运行目录、进程启动方式和日志位置。HTTPS 网页只填写操作员名称，首次使用时自动登记到运行目录的 `operators.sqlite3`，并建立浏览器会话。名称用于显示，供可信内网使用。浏览器和异机节点都访问同一个 HTTPS 地址。给访问者分发公用证书，私钥保存在主服务器。

## 部署并登记执行节点

1. 在目标 macOS 或 Windows 机器检出仓库，检查 Python、Git、Claude Code 的登录态，以及所选测试方式需要的 Unity 或 ADB。节点配置和运行记录放在本机独立目录。
2. 在新节点上生成配置并启动执行程序。主服务器使用自签名证书时，通过可信渠道取得公用 PEM 文件并传给 `--ca-cert`。使用已受信任的证书时可以省略该参数。复用已登记节点的 `node.json` 时直接启动执行程序。

```bash
python3 provision.py --directory <节点运行目录> --join-url https://<主机IP>:8443 \
  --ca-cert <主服务器公用证书.pem> \
  --method Unity --unity-editor <节点本机的Unity可执行文件>
python3 worker.py --config <节点运行目录>/node.json
```

3. 将节点用于直连主服务器的内网 IPv4 地址交给操作员。操作员在主服务器网页打开「节点配置 → 添加执行节点」，填写名称和该 IP，点击「连接并登记」。执行程序主动请求配对，主服务器按 HTTPS 连接的来源 IP 匹配请求。请求有效期为 5 分钟。两台机器之间需要直连，节点 IP 应当稳定且独立可见。
4. 节点上线后，在配置向导中保存节点本机三端工程根目录。目录结构为 `K1Client/k1_client`、`K1Game/game` 和 `K1Dataconfig/dataconfig`。节点会检查工程和技能仓库，满足对应能力时显示「可用」。使用真机时传 `--method 真机`，并确认 ADB 能检测到已授权设备。

执行程序会自动识别 macOS / Windows，把配对结果写入本机 `node.json`。将该文件保留在原节点的私有运行目录中，供重启后继续使用。网页中的操作员名称用于记录操作，按 IP 登记节点适用于可信内网。

## Mac Unity 运行验收

在有桌面登录会话的 Mac 节点上启用。首次部署加入 `--unity-runtime`，已有节点在本机 `node.json` 中设置 `"unity_runtime": true` 后重启 Worker。网页提交分支或版本日验收时，选择 Unity、该 Mac 节点和现有测试服 ID。

把节点的客户端目录指向专用验收工程，提交前关闭该工程的 Unity。Worker 记录原分支和提交，直接还原暂存区和工作树中的 Git 跟踪文件，再切到验收提交并启动 Unity。未跟踪文件保留。节点运行目录的 `node-state/runtime/<执行尝试>` 保存日志、`runtime.json` 和用于恢复的 `workspace.json`。在批次详情取得交接文件路径，由 Mac 上的验收人员或 Agent 登录目标服实测。服务端与配置通过实测环境核对。

Worker 接单后冻结一个 K1FunctionReview 提交。用例生成和实测共用该提交里的 `review-k1-function`、运行验收、客户端路径及多语言与屏幕适配规则。用例生成另外读取同一提交的 `slg-testcase-generator` 及 K1 用例规则。Agent 可按需用 `read_function_review` 从该提交读取引用和已有功能经验。批次执行记录和 `runtime.json` 记录技能提交及远端核对状态。用例初始状态为「待执行」，Claude 输出和用例进度会回传网页。

网页选择的功能验收、版本日验收分别对应 FunctionReview 的 `development`、`mainline_regression`。当前 Mac 自动验收路径使用 Unity 和已配置的测试服，按该范围核对现有产物。进度、截图和用例结果记录在网页，任务范围不包含 Jenkins 转换或部署、真机、在线回写和群播报。缺少技能引用或操作工具时，执行记录说明实际缺口，继续可独立验收的用例。

要启用全自动 Unity 验收，在 Mac 的 `node.json` 配置 `"unity_test_roles": {"测试服数字ID": 角色数字ID}`，使用该节点已登录账号下允许验收的测试角色。Unity 工程需启用本机 Editor Debug MCP，默认检查 21891–21893。若端口不同，可用 `unity_debug_ports` 指定本机端口列表。

Worker 会等待工程导入、进入 Play、登录并核对实际角色，再通过 Unity 工具让 Agent 执行清单。网页逐条显示进度，结论和截图保存在该批次运行目录。无法完成的用例保留原因。验收结束后 Worker 关闭 Unity、还原工程并回传结果。如果节点未配置 `unity_test_roles`，Unity 路径仍使用人工接手。版本日可填写一个 Jira 单号，仅排队该项。

屏幕适配用例在 Unity Game 窗口逐个应用目标分辨率并核对实际画面，Worker 在退出前恢复原尺寸。语言用例通过游戏内设置切换，客户端重启后接续同一测试角色，结束时恢复原语言。需要英雄或战力时，Agent 可以在指定的测试角色上通过正常玩法获得英雄、领取免费奖励和使用已有普通资源升级。英雄获得和升级可能保留在测试角色上。代码、数据表和服务端配置禁止修改。

用例按 `runtime.json` 的 `cases_file` 路径原子写入。单事项可写用例数组。多事项用对象保存，键取 `runtime.json` 中每个事项的 `key`，值为各自的用例数组。每个用例填写稳定的 `id`、`title`、`state` 和可选的 `evidence`，状态支持 `pending`、`running`、`pass`、`fail`、`blocked`、`confirm`。每完成一条用例就更新该文件，Worker 会把进度同步到网页。

实测结束后，在 `runtime.json` 同一目录原子写入 `result.json`，然后关闭本批次的 Unity Editor。回执须包含以下字段，实际回执填写完整提交和证据位置。

```json
{
  "server": "2903",
  "client": "<完整客户端提交 SHA>",
  "result": "pass",
  "reason": "在目标服完成验收，记录实测现象",
  "evidence": "节点验收报告或截图位置"
}
```

`result` 支持 `pass`、`fail`、`blocked`。请在回执中记录实际测试角色和各项观察，便于复核。编辑器退出后，Worker 还原验收产生的跟踪文件改动并切回原分支，再校验回执并更新批次。Worker 异常退出时，确认该工程的 Unity 已关闭，按 `workspace.json` 还原跟踪文件并切回原分支和提交，核对后结束待核对批次。

多事项批次在回执顶层保留 `server` 和 `client`，另设 `items` 对象。该对象以事项 `key` 为键，每项分别填 `result`、`reason` 和 `evidence`。

## 发布与更新

分支验收支持按 Jira 单号查询目标分支。主服务器的 `server.json` 可填写 `branch_lookup_repo`
为本机能读取客户端 Git 远端的检出目录。要匹配名称不含单号的分支，另配 `jira_url`（HTTPS）
和 `jira_token_file`（服务账号只读令牌文件的绝对路径）。查询优先核对 Jira 关联的客户端
GitLab 合并请求、提交或分支链接。令牌只存放于主服务器的私有运行目录。

等待中的批次可从队列删除。进行中的批次可向支持停止的 Worker 发送请求，节点退出本次
进程并完成工程恢复后，才会将批次标记为「已停止」。恢复失败的批次进入待核对。

使用仓库的 [k1-console-release 技能](.agents/skills/k1-console-release/SKILL.md) 发布。升级主节点时将后端、HTTPS 入口、`VERSION` 和网页资源部署为同一版本。如果当前 HTTPS 入口单独存放，重启时从这版源目录运行 `lan.py`，沿用既有证书与运行配置。执行节点首次升级到支持更新的 Worker 后，后续可点网页上的「更新并重启」。

## 交付前核对

- 从另一台内网机器直接打开主服务器 HTTPS 地址。输入操作员名称后进入验收台并刷新页签，再使用同名进入，确认沿用原登记。
- 在主服务器确认新节点在线、平台匹配和依赖检查结果。提交一条授权范围内的联调批次，检查节点接单、详情进度和 AI 输出。
- 核对重启方式、配置与证书文件位置。macOS 上如需在重启后自动启动，应按实际用户会话配置进程管理，并检查外接盘和钥匙串可用后才启动服务。
