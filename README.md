# SpringHub · HandBrake 转码工作台

面向家庭 NAS 和自托管环境的原生 Web 视频转码工作台。当前仅保留 **Python 标准库 + 真实 HandBrakeCLI** 实现，源码与页面位于 `next/`；旧 Rust / FFmpeg 服务和页面已移除。

## 启动预览

所有命令在 WSL Ubuntu 中执行。需要 Python 3.11+ 和 HandBrakeCLI；前端无需安装 npm 依赖或构建。

Claude Code 的默认预览配置为 **`springhub-preview`**，启动新版工作台，地址为 <http://localhost:18087>。

也可以在 WSL 终端启动：

```bash
SPRINGHUB_HB_PORT=18087 SPRINGHUB_HB_STATE="$HOME/.cache/cute-cat-handbrake-preview-18087" bash .claude/preview.sh
```

预览脚本优先使用真实 HandBrakeCLI；未安装时会明确提示使用接口 mock，mock 不执行真实转码。预览目录中的占位文件不是可转码的媒体，请添加真实源文件再创建任务。

## 当前功能

- 系统概览、编解码器状态、任务队列与任务详情。
- 七标签编码设置、预设与任务模板、参数预校验。
- 启动 / 继续、暂停、取消、重试、删除记录与批量操作。
- 路径 containment 校验、源文件保护、临时产物校验与原子发布。
- 结构化参数白名单；不允许客户端传入任意编码命令。
- SMB/NFS 由宿主机或 Docker 挂载，应用不执行挂载、系统安装或驱动安装。

## 部署与配置

- [完整文档](README.handbrake.md)
- [配置示例](config.example.handbrake.toml)
- [Docker 镜像](Dockerfile.handbrake)
- [Compose 部署](compose.handbrake.yaml)
- [环境变量示例](.env.handbrake.example)
- [安全边界与验证记录](CONVERGENCE.md)

```bash
docker compose -f compose.handbrake.yaml up --build
```

Docker 构建和运行仍需在目标环境验证。远程访问需配置 API token 和 TLS 反向代理，SQLite 放在本地盘，网络共享凭据不进入源码、日志或前端。

## 测试

```bash
PYTHONPATH=next:next/tests python3 -m unittest discover -s next/tests -t next/tests
```

```bash
node --test next/tests/test_choices.cjs
```

## SpringHub 改名兼容

页面、通知、命令入口和镜像品牌已更名 SpringHub。推荐使用 `python -m springhub` 和 `SPRINGHUB_*` 环境变量；`python -m cutecat` 及 `CUTE_CAT_*` 仍兼容，显式新变量优先。数据库文件 `cute-cat.db`、Docker 数据卷 `cute-cat-data`、挂载标记 `.cute-cat-mounted`、浏览器偏好键和模板 JSON 格式保留历史标识，避免改名导致数据丢失。

Compose 服务现在名为 `springhub`。已有容器部署应先停止旧服务并复用原 Compose 项目名和数据卷，不使用 `down -v`，不要让两实例同时管理同一数据库；本机尚未实测 Docker 升级。宿主项目目录不会自动移动。

## 旧版数据

删除旧版源码不会迁移或删除旧数据库、Docker volume 或媒体。新版与旧版的任务协议和数据库结构不兼容，请勿让新版直接使用旧版数据库。历史实现仍可在 Git 历史中查看。

HandBrake 的 GPL 及编解码器许可、专利义务需按实际构建与部署地区核对。
