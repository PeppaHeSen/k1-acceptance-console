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
4. 节点上线后，在配置向导中保存节点本机三端工程根目录。三端 Git 仓库可分别放在 `K1Client/k1_client`、`K1Game/game` 和 `K1Dataconfig/dataconfig`，也可直接放在根目录下的 `k1_client`、`game` 和 `dataconfig`。节点会检查工程和技能仓库，满足对应能力时显示「可用」。使用真机时传 `--method 真机`，并确认 ADB 能检测到已授权设备。

执行程序会自动识别 macOS / Windows，把配对结果写入本机 `node.json`。将该文件保留在原节点的私有运行目录中，供重启后继续使用。网页中的操作员名称用于记录操作，按 IP 登记节点适用于可信内网。

## Unity 运行验收

在有桌面登录会话的 Mac 或 Windows 节点上启用。首次部署加入 `--unity-runtime`。已有节点在本机 `node.json` 中设置 `"unity_runtime": true`，配置测试服后，在用户的桌面会话中重启 Worker。网页配置向导中的「Unity」表示 Editor 和工程可读，「Unity 自动实测」显示已配置的测试服。提交分支或版本日验收时，填写测试服 ID，队列只会分配给支持该测试服自动实测的节点。

Worker 使用节点已配置的客户端工程。若该工程正被 Unity 占用，批次会等待编辑器关闭，再继续准备工程。Worker 记录原分支和提交，还原暂存区和工作树中的 Git 跟踪文件，再切到验收提交并启动 Unity。未跟踪文件保留。节点运行目录的 `node-state/runtime/<执行尝试>` 保存日志、`runtime.json` 和用于恢复的 `workspace.json`。服务端与配置通过实测环境核对。

Worker 接单后冻结一个 K1FunctionReview 提交，本轮只发起一次验收 AI 会话。Agent 在这次会话中使用同一提交的 `review-k1-function`、`slg-testcase-generator`、运行验收和屏幕适配规则。它可以按事项通过 `register_cases` 登记或追加用例，并用 `case_progress` 上报状态。新用例初始为「待执行」，清单、执行结果和 Claude 输出实时回传网页。Agent 可用 `read_function_review` 补查同一提交的引用和已有功能经验。

网页选择的功能验收、版本日验收分别对应 FunctionReview 的 `development`、`mainline_regression`。自动验收在 Unity 中执行。Agent 在遇到缺少前置的用例时按技能查找现有条件，按需重登、切换测试身份，或使用节点上已配置的环境准备动作。准备过程、回执和用例结果进入批次执行记录，截图保存在执行节点的运行目录。

在节点 `node.json` 中用 `"unity_test_servers": ["2903"]` 配置可验收的测试服，也兼容旧版 `unity_test_roles` 中的服号。验收 Agent 读取本机 Unity 当前账号下目标服可用角色，按用例选择并验证实际身份。普通列表为空时，Agent 可以通过 Unity 的 Dev 快速登录面板使用已登记在 `unity_test_roles` 的目标服测试角色。切换到目标服以外做专门的环境准备时，也使用该配置中对应服的角色。Unity 工程需启用本机 Editor Debug MCP，默认检查 21891–21893。若端口不同，可用 `unity_debug_ports` 指定本机端口列表。

节点管理员可在 `node.json` 的 `unity_environment_actions` 中登记已有的环境准备脚本。每个动作填写 `kind`（`gm`、`simulation` 或 `jenkins`）、`server`、`player`、`description`、`command`（固定参数数组，首项为本机可执行文件的绝对路径）和可选 `timeout`（单次调用 1 到 180 秒，默认 90）。服和角色需出现在 `unity_test_roles`。GM 与故障模拟还需要 Unity 已登录该服和角色。可在 `parameters` 定义可选值和整数范围，例如 `{"stage":["prepare","active"],"count":{"min":1,"max":3}}`。Agent 执行时按用例选择具体参数。

动作脚本从标准输入读取一条 JSON，包含 `batch`、`attempt`、`item`、`case`、`action`、`server`、`player`、`client`、`refs`、`function_review`、`parameters` 和 `operation`。脚本可复用该提交里的 FunctionReview GM 或 Jenkins 流程。同一次执行中，同动作、同参数会得到同一个 `operation`，脚本按它保存回执，重复调用时查询或复用已提交的动作。标准输出只返回 `{"status":"ready|pending|blocked","evidence":"可展示的回执摘要"}`。长时间的部署可先返回 `pending`，Agent 可以暂放该用例，跑其他独立用例后再查询。回执摘要会显示在网页，凭据、原始部署参数和敏感日志留在节点本地。Agent 收到 `ready` 后还要验证用例需要的业务前置。

Worker 启动 Unity 后立即启动本轮验收 Agent。Agent 查看工程连接、导入及编译状态，期间可按 FunctionReview 登记用例，再按实际状态进入 Play、选择测试角色及执行清单。网页逐条显示进度，结论和截图保存在该批次运行目录。无法完成的用例保留原因。验收结束后 Worker 关闭 Unity、还原工程并回传结果。省略测试服 ID 可提交基础链路联调。版本日可填写一个 Jira 单号，仅排队该项。

屏幕适配用例在 Unity Game 窗口逐个应用目标分辨率并核对实际画面，Worker 在退出前恢复原尺寸。语言用例通过游戏内设置切换，客户端重启后接续同一测试角色，结束时恢复原语言。需要英雄或战力时，Agent 可以在指定的测试角色上通过正常玩法获得英雄、领取免费奖励和使用已有普通资源升级。英雄获得和升级可能保留在测试角色上。正式业务源码、数据表和线上热更属于独立流程。

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
