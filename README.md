# Cute Cat

Cute Cat 是一个面向家庭 NAS 和自托管环境的 Web 视频转码工作台。它将 FFmpeg 放进容器，媒体目录由宿主机挂载后以只读/读写边界暴露给容器。

## 当前能力

- Rust + Axum 单体服务，SQLite 保存任务状态。
- 浏览已配置的媒体工作目录，创建 MP4 转码任务。
- H.264/H.265 软件预设、队列、取消、失败状态和重启恢复标记。
- 启动时报告 FFmpeg / ffprobe、编码器、硬件加速路径和存储根目录状态。
- 深色高信息密度 Web 工作台，支持窄屏和键盘焦点。
- Docker Compose 默认非 root、只读根文件系统、丢弃 capability、SQLite 使用本地 volume。

## 快速开始

> 构建和运行命令按项目约定在 WSL Ubuntu 中执行。

```bash
cp config.example.toml config.toml
mkdir -p media/incoming media/converted
touch media/.cute-cat-mounted
docker compose up --build
```

打开 http://127.0.0.1:8080。默认 Compose 只绑定本机回环地址；跨主机访问请放在 TLS 反向代理后，并设置 `security.api_token`。

## 配置存储目录

`config.toml` 中的 `storage_roots` 是应用唯一允许访问的目录。客户端只提交根 ID 和相对路径，不接受容器绝对路径、`..` 或符号链接。

SMB/NFS 应由宿主机挂载并确认权限，再将挂载点 bind mount 到容器：

```yaml
volumes:
  - /srv/nas/video:/media
```

建议为每个共享目录放置 `mount_marker` 指定的标记文件。标记缺失时界面会显示不可用，避免 NAS 未挂载时把本地空目录误当成共享目录。NAS 凭据只保留在宿主机挂载配置或凭据存储中，不写入镜像和普通环境变量。SQLite 数据库不要放在网络文件系统。

## GPU 与 FFmpeg

FFmpeg 的 codec 能力主要由构建时决定；本项目不会在 Web 请求或启动时执行任意 `apt install`，也不会修改宿主机驱动。容器启动时只检测当前容器真正能看到的 FFmpeg 构建、编码器和设备。

- CPU：使用默认镜像即可。
- NVIDIA：宿主机先安装匹配的 NVIDIA 驱动与 Container Toolkit，并显式透传 GPU；镜像需包含对应 NVENC 用户态库。
- VAAPI：宿主机透传 `/dev/dri`，使用包含目标用户态驱动的镜像/Compose 配置。
- 选择性环境准备只允许使用经过固定版本和许可审查的镜像变体。`DRIVER_SETUP=auto` 目前只是显式诊断钩子，不执行包管理命令；这样不会给 Web 服务 root 权限或 Docker socket。

## 本地开发

需要在 WSL 中安装 Rust stable、FFmpeg 和 ffprobe。当前开发环境检测到 Node/npm，但未检测到 rustc、cargo、docker、ffmpeg 或 ffprobe；因此未能在本机完成编译或真实 Docker 启动验证。

```bash
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test
cargo run
```

服务端默认读取 `config.toml`，也可以设置 `CUTE_CAT_CONFIG` 指向其他配置文件。数据库迁移由 SQLx 在启动时执行。

## API

- `GET /health/live`
- `GET /health/ready`
- `GET /api/v1/capabilities`
- `GET /api/v1/presets`
- `GET /api/v1/storage-roots`
- `GET /api/v1/storage-roots/{id}/entries?path=...`
- `POST /api/v1/probe`
- `GET|POST /api/v1/jobs`
- `GET /api/v1/jobs/{id}`
- `POST /api/v1/jobs/{id}/cancel`

生产环境应设置 API token，并通过反向代理提供 TLS。该 MVP 面向可信管理员的单机部署，不是多租户公网服务。

## 许可证与范围

FFmpeg 的许可证和编解码器专利义务取决于构建配置与部署地区，发布镜像时请分别核对。当前版本不包含字幕/章节编辑、光盘菜单、多节点调度、任意 FFmpeg 参数、宿主机驱动安装或容器内挂载 SMB/NFS。
