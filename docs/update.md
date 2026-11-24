# 升级已有安装

在宿主机终端使用原安装路径执行：

```sh
goofish update
```

命令从 `gdolobg/goofish-cli` 的最新 GitHub Release 下载 Maxuria wheel，
校验发布清单中的 SHA256，并使用当前 Goofish 所属 Python 环境的 pip 升级。
需要该环境可写且已安装 pip；不会切换到系统 Python 或申请 sudo。

如果 Docker 可用，会检查已有沙盒。已有沙盒会停止并使用新版重建，保留原观察端口
和 `goofish-sandbox-state` 登录态数据卷。未创建过沙盒时只升级 CLI。
安装失败不会停止沙盒；若安装成功但重建失败，按错误提示手动重试重建，数据卷仍保留。
升级前请结束正在执行的沙盒任务。

完成后重新连接 Codex 中的 Goofish MCP。已有登录通常可继续使用；平台登录过期时仍需扫码。
此命令只提供 CLI 入口，不暴露为 MCP 工具。

旧版本尚无 `update` 命令时，需要先手动安装一次包含此命令的新版 wheel。
