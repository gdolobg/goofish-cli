# 其他机器安装（含独立桌面沙盒）

此包为 Maxuria 本地分发版本 `0.6.3+maxuria.20261124.1`，不是上游 PyPI 正式发布。
包含此前的沙盒与下单工具，以及 首次 MCP 工具调用时自动启动 Docker Desktop 和沙盒。Windows 启动会恢复完整用户环境，避免 ProgramData 缺失，并使 Docker 独立于 MCP 客户端运行。

## 环境要求

- Python 3.11+（推荐 3.12）。
- 可运行 Linux 容器的 Docker Desktop / Docker Engine。
- 首次安装、构建需要联网。包不包含 Python 依赖或 Docker 基础镜像，不是离线安装包。
- 默认沙盒资源：3 CPU、3 GB RAM、1 GB 共享内存。建议宿主机至少 8 GB RAM。
- 本机验证平台为 Windows x64；其他平台和 Apple Silicon 尚未实测。

先运行 `docker version`、`docker info` 和 `docker run --rm hello-world`，确认 Server 正常且 OSType 为 linux。
不要为安装沙盒直接套用旧版 Docker；按目标机器的系统版本选择 Docker 支持的版本。

## Windows 安装

解压分发 ZIP，在解压目录打开 PowerShell：

```powershell
py -3.12 -m venv C:\tools\goofish\.venv
$python = 'C:\tools\goofish\.venv\Scripts\python.exe'
& $python -m pip install .\goofish_cli-0.6.3+maxuria.20261124.1-py3-none-any.whl
$goofish = 'C:\tools\goofish\.venv\Scripts\goofish.exe'
& $goofish sandbox --help
& $goofish skills install --list
```

使用 CHECKSUMS.sha256 校验包文件。不要复制原机器的 .venv、Cookie、账号文件或 Docker 状态卷。

## Mac / Linux 安装

```sh
python3 -m venv "$HOME/.local/share/goofish-venv"
"$HOME/.local/share/goofish-venv/bin/python" -m pip install ./goofish_cli-0.6.3+maxuria.20261124.1-py3-none-any.whl
GOOFISH="$HOME/.local/share/goofish-venv/bin/goofish"
"$GOOFISH" sandbox --help
```

Linux 无桌面主机可使用 Docker Engine。Windows/Mac 安装 Docker Desktop 后，首次调用 MCP 工具时可自动启动；手动执行 sandbox start/login 前仍需先启动 Docker。

## 启动与独立扫码登录

Windows（其他系统使用对应 goofish 绝对路径）：

```powershell
& $goofish sandbox start
& $goofish sandbox login
& $goofish sandbox run auth status --format json
```

打开启动命令返回的本机观察链接，通常为 http://127.0.0.1:16080/vnc.html?autoconnect=1&resize=scale 。
在手机闲鱼 App 扫码并确认。账号有效后才继续。可关闭观察网页，但保留容器及内部 Chromium。
每台机器独立登录，不共享登录态。不要将观察端口开放到公网。

## MCP 配置

MCP 名称保留为 goofish，command 使用目标机器的绝对路径，args 为 ["sandbox", "mcp"]。
Docker 命令必须对 MCP 客户端进程可见；安装 Docker 后重启客户端以刷新 PATH。

Codex 的 config.toml 示例（修改已有条目，不要重复添加）：

```toml
[mcp_servers.goofish]
command = 'C:\tools\goofish\.venv\Scripts\goofish.exe'
args = ["sandbox", "mcp"]
startup_timeout_sec = 180
tool_timeout_sec = 180
```

支持 mcpServers JSON 的客户端示例：

```json
{"mcpServers":{"goofish":{"command":"C:\\tools\\goofish\\.venv\\Scripts\\goofish.exe","args":["sandbox","mcp"]}}}
```

Mac/Linux 替换为实际 goofish 可执行文件路径。不要将 noVNC 链接填入 MCP URL。
MCP 连接与列出工具不启动 Docker，首次实际调用工具时才检测 Docker 并自动启动沙盒，首次工具调用可能构建镜像。Windows/macOS 可自动启动 Docker Desktop（等待最多 120 秒）；Linux 需先启动 Docker Engine。首次构建可能超过客户端工具调用超时，建议预先运行 sandbox start。MCP 断开后 Docker 和沙盒继续运行。

## 配套技能

Claude 默认位置：`goofish skills install`。
其他客户端用 `goofish skills install --dest <该客户端的技能目录>`。
不要使用 --force 覆盖已有自定义技能，除非已备份。安装包中已包含沙盒登录和下单使用指引。

## 验收和维护

重连后检查 19 个工具，包含 order_preview 和 order_create_unpaid。
依次调用 auth_status、search_items、item_view、order_preview；先不调用任何写工具。
沙盒会执行真实闲鱼操作，不是模拟环境。下单只创建待付款订单，不付款。
结果不确定时先查订单，不换 request_id 自动重提。本机与沙盒防重复记录不共享。

升级安装包后：

```powershell
& $goofish sandbox stop
& $goofish sandbox start --rebuild
```

默认数据卷 goofish-sandbox-state 保存独立登录态及防重复记录，停止/重建保留该卷。
不要删除数据卷来解决登录或下单错误。默认仅支持一个沙盒实例，浏览器任务串行。
遇到平台验证，执行 `goofish sandbox browse --item-id <商品ID>` 后亲自处理。

本版还修复 Windows MCP 中文输出编码，并记录每次调用的分阶段耗时到 stderr 和 `~/.goofish-cli/logs/mcp-timing-<UTC日期>-<进程ID>.jsonl`。耗时记录不包含工具参数或结果；旧日志需按需清理。

更多细节见同包 sandbox.md、mcp-setup.md、unpaid-orders.md。
