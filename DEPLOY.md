# K1 验收台 Agent 部署说明

本仓库可部署主服务器和执行节点。主服务器提供网页、钉钉进包表和队列，macOS 或 Windows 节点连接该服务器执行批次。当前版本支持只读链路联调，包括真实选单、接单和 Claude Code 输出。

把本文件和目标机器信息交给 Agent。说明要部署「主服务器」还是「执行节点」，以及是否允许它通过 SSH 操作目标机。主服务器需要内网地址和部署目录。执行节点需要主服务器的 HTTPS 地址、节点名称、操作系统和本机三端工程根目录。复用主机上已有的登录方式。

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
2. 打开主服务器网页，进入「节点配置 → 登记执行节点」。填写该机器的名称和操作系统，保存页面返回的节点 ID 和令牌。网页登记的操作系统应和实际机器一致。
3. 在新节点上生成配置。交互提示时输入节点令牌。主服务器使用自签名证书时，通过可信渠道取得公用 PEM 文件并传给 `--ca-cert`。使用已受信任的证书时可以省略该参数。

```bash
python3 provision.py --directory <节点运行目录> --join-url https://<主机IP>:8443 \
  --node-id <网页返回的节点ID> --ca-cert <主服务器公用证书.pem> \
  --method Unity --unity-editor <节点本机的Unity可执行文件>
python3 worker.py --config <节点运行目录>/node.json
```

使用真机路径时传 `--method 真机`，并确认节点的 ADB 能检测到已授权设备。连接成功后，在主服务器的配置向导中选该节点，填写节点本机三端工程根目录。该目录下使用 `K1Client/k1_client`、`K1Game/game` 和 `K1Dataconfig/dataconfig`。节点会检查工程和技能仓库，满足对应能力时显示「可用」。

## 交付前核对

- 从另一台内网机器直接打开主服务器 HTTPS 地址。输入操作员名称后进入验收台并刷新页签，再使用同名进入，确认沿用原登记。
- 在主服务器确认新节点在线、平台匹配和依赖检查结果。提交一条授权范围内的联调批次，检查节点接单、详情进度和 AI 输出。
- 核对重启方式、配置与证书文件位置。macOS 上如需在重启后自动启动，应按实际用户会话配置进程管理，并检查外接盘和钥匙串可用后才启动服务。
