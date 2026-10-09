# 播放模板、多引擎与外部调用

SpringHub 保留 HandBrake，同时支持本机 FFmpeg 和管理员配置的 rffmpeg 兼容 wrapper。它生成媒体文件，不提供播放器、HTTP Range 媒体服务、HLS 分片或直播推流。MP4 产物可交由支持 Range 的网站/CDN/媒体服务器播放；播放器、浏览器、素材和硬件的实际兼容性仍须验收。

## 内置模板

内置模板不写入或覆盖用户模板，UUID 稳定，支持选用、复制、导出、设默认。编辑后保存新用户模板；原模板只读。调用 `GET /api/v1/task-templates` 可取得 UUID、说明、参数和 `builtin` 标识。

| 模板 | 尺寸上限 | 视频质量 | 码率上限 | 音频 |
|---|---|---|---|---|
| 网站标准 | 1920×1080 | H.264 RF/CRF 22 | 5000 kbps | AAC 128 kbps |
| 网站轻量 | 1280×720 | H.264 23 | 2500 | AAC 128 |
| 食品 / 商品细节 | 1920×1080 | H.264 19 | 7000 | AAC 160 |
| 手机兼容 | 1280×720 | H.264 23 | 2200 | AAC 128 |
| 手机省流量 | 854×480 | H.264 Baseline 25 | 1000 | AAC 96 |
| 平板 | 1920×1080 | H.264 21 | 5000 | AAC 160 |
| 电视通用 | 1920×1080 | H.264 20 | 8000 | AAC 192 |
| 现代设备 HEVC | 1920×1080 | H.265 24 | 4000 | AAC 128 |
| 静音网页背景 | 1280×720 | H.264 25 | 1800 | 无音轨 |

| HEVC MP4 · 保持源参数 | 不缩放、不裁剪 | 源视频平均码率作为 HEVC 目标 | 不设置上限/缓冲 | 全部兼容音轨直接复制 |

新增源参数模板仅支持 FFmpeg 本机 / rffmpeg，保留源时间戳、尺寸、SAR、8/10-bit 4:2:0、章节与可直接复制的 mov_text 字幕/全局元信息；不保留全部容器专属信息。不自动切换引擎，不设置 30 fps 限制；读取视频流 `bit_rate` 或 `BPS`/`BPS-eng` 为目标 kbps（四舍五入），不用容器总码率、文件大小估算或默认 CRF 回退。没有可靠视频码率、HDR/旋转附属信息、非 4:2:0、附属流或无法复制到 MP4 的音轨/字幕时明确拒绝；音频直接复制限 AAC/MP3/AC3/EAC3/ALAC，不偷偷转码。**重编码不能精确保留实际码率、文件大小或画质**。

模板策略通过 `source_preserve:true` 表达，选择源后预校验展示解析目标，无源预校验仅结构通过并返回 `pending_source:true`、空 argv；创建时再次解析并冻结源参数和目标 spec，执行前及等待输出锁后复查源信息，有变化明确失败。视频页可关闭“保持源参数”再手动改设置；复制/导入导出保留策略。

原 9 类模板共同参数：MP4 faststart、8-bit 4:2:0、最高 30 fps、关键帧间隔 60 帧、缓冲为码率上限的两倍、保持比例、仅缩小、不裁边；音频为 48 kHz 立体声。食品模板不自动增艳或锐化。HEVC 并非所有浏览器都支持；静音不保证浏览器自动播放。质量值不是所有编码器通用尺度，也不保证固定文件大小。HDR→SDR 色调映射不在这些模板范围内。

## 引擎配置

见 `config.example.handbrake.toml`：

```toml
[engine]
handbrake_bin = "HandBrakeCLI"
ffmpeg_bin = "/usr/bin/ffmpeg"
ffprobe_bin = "/usr/bin/ffprobe"
rffmpeg_bin = ""
rffprobe_bin = ""
remote_probe_dir = ""
```

新环境变量 `SPRINGHUB_FFMPEG`、`SPRINGHUB_FFPROBE`、`SPRINGHUB_RFFMPEG`、`SPRINGHUB_RFFPROBE` 可覆盖启动配置程序路径；旧 `CUTE_CAT_*` 前缀仍接受。任务 API 请求不得指定程序路径、原始 argv、SSH 命令或凭据。

### 网页设置入口

“系统设置 → FFmpeg 与远程”统一管理本机 FFmpeg 与 rffmpeg。创建任务中的刷新入口为图标按钮；远程配置不再在摘要页重复提供入口。

**本机一键安装**：已有可执行 FFmpeg / ffprobe 时保留部署版本；缺失时，确认后下载固定月度 BtbN GPL 构建（FFmpeg 8.1 系列，2026-09-30），校验预置文件大小及 SHA-256，只将 FFmpeg、ffprobe 与许可/来源文件写到数据库同级的 `tools/ffmpeg/<sha256>/`。使用私有暂存目录、跨进程安装锁、有限解包和原子发布，版本检查通过后才启用；失败清理暂存文件并允许重试，不覆盖原安装、不调用 sudo/apt、不改宿主机或远端。支持 Linux x86_64 / ARM64（glibc ≥ 2.28、内核 ≥ 4.18；实际兼容性由版本调用验证），需要网络、可执行的数据卷及约 1.2 GiB 空间。固定下载地址不可由 API 请求指定，重定向限 GitHub 受信任 HTTPS 主机。来源与 GPL 条款随安装保留，ARM64 与容器实际运行验证范围以测试结果为准。

`GET /api/v1/ffmpeg/install` 只读取状态，不下载或调用程序；`POST` 仅接受 `{"confirm":true}`，使用现有 API token 鉴权并拒绝跨站浏览器安装请求。安装在服务后台进行，关闭抽屉只停止前端状态查询，再打开可继续查看；服务重启可清理中断暂存并重试。已有系统版本优先，程序对象不会原地修改，运行任务保持原引擎实例。应用安装不改变 rffmpeg、驱动或硬件能力；Docker 官方镜像已内置 FFmpeg，通常无需安装。

设置负责引擎部署配置，创建任务仍可逐任务选择引擎。未安装 HandBrake、缺少 FFmpeg/ffprobe、未配置 rffmpeg 或共享探测目录不可用时，对应选项显示原因并禁用；可点击引擎标题旁的刷新图标重查；安装与配置统一位于“系统设置 → FFmpeg 与远程”。轻量状态接口 `GET /api/v1/engines/availability` 只检查本机前置条件，不执行程序、不联系远端。被选引擎失效时不会静默切换，加入队列被禁用；安装可选不代表所有编解码组合可用。

点击右上角 **系统设置 → FFmpeg 与远程**，在 rffmpeg 远程区域填写 FFmpeg 兼容入口、ffprobe 兼容入口和共享探测目录，点击“保存设置”。程序路径是 Linux 服务/容器内路径，支持绝对路径或单个 PATH 程序名，不接受命令参数。共享目录必须位于已配置的可写存储根内。

网页配置存到 SQLite，覆盖 TOML/环境启动默认值，无需重启，重启后仍保留；不写回 TOML。“恢复本类默认”仅恢复启动默认草稿，保存后生效；三项同时清空保存可停用。保存及读取设置**不会执行 wrapper**，可以先保存尚未安装/挂载的部署配置。

“检查已保存配置”需要先保存草稿，检查本机程序可执行性、共享目录/挂载/权限与固定 `-version` 响应，可能通过 wrapper 联系远端；不创建任务、不编码、不安装或建目录。检查通过不证明远端身份、路径一致或真实编码。切换引擎的完整能力检测仍需主动进行。

新 rffmpeg 任务冻结创建时三项配置，不影响已排队或正在运行的任务；已有旧任务没有远程配置快照时使用当前配置。`GET /api/v1/config` 中 `engine` 是启动配置，`effective_rffmpeg` 是当前有效配置。`POST /api/v1/rffmpeg/check` 使用已保存配置（空对象请求），受 API token 鉴权，不接受草稿路径/argv。

已有配置、请求、任务和模板未指定引擎时继续使用 HandBrake。新任务把实际引擎、有效 spec、模板身份与运行设置冻结到任务快照。修改模板不影响已排队任务。两套引擎不是参数完全等价的实现：

- FFmpeg 支持模板使用的 H.264/HEVC、明确映射的视频编码器、质量/码率、尺寸/旋转/基础滤镜、帧率、音轨、源软字幕与保留/移除章节。
- 自动裁边、HandBrake 官方/导入预设、disc title、SRT/字幕烧录/foreign 搜索、CSV 章节、某些高级滤镜、copy-mask/fallback、DRC、turbo 等无等价映射，FFmpeg 预校验会明确拒绝。选择 FFmpeg 时自动裁边改为关闭。
- FFmpeg 质量模式限 x264/x265/VP9/AV1；其他编码器使用码率模式。硬件实例化失败不自动回退 CPU。x264 的速度/调优/档次/级别不自动套用到硬件编码器。
- FFmpeg 两遍限 x264/VP9/MPEG/Theora，使用任务 staging 内独立 passlog；首遍和第二遍都实际运行。
- HandBrake 网页播放参数限自定义参数和 x264/x265 对应位深，不覆盖官方预设；faststart 要求明确 MP4。
- 本机暂停/继续保持进程；暂停时间不计编码超时。FFmpeg 速度显示“× 实时”，HandBrake 显示帧/秒。

`GET /api/v1/engines` 返回引擎状态；`GET /api/v1/encoders?engine=ffmpeg` 返回该引擎报告/实例化结果。列表存在不等于能运行，微型探测通过不等于任意参数与素材都兼容。

## rffmpeg 管理员约定

支持的是 **FFmpeg/ffprobe 命令行兼容入口**，不是臆造 rffmpeg 私有命令行。管理员安装并配置自己的 [rffmpeg](https://github.com/joshuaboniface/rffmpeg)（包括其依赖、主机池、SSH 和版本适配），把 wrapper 的 ffmpeg / ffprobe 兼容入口路径填入配置。例如：

```toml
rffmpeg_bin = "/opt/rffmpeg/ffmpeg"
rffprobe_bin = "/opt/rffmpeg/ffprobe"
remote_probe_dir = "/media/remote-probes"
```

要求：

1. 本机/容器和所有远端使用**相同绝对路径**访问输入、输出、staging、探测片段和两遍 passlog。程序不上传媒体、不映射路径。
2. `remote_probe_dir` 是已有的、可写的、位于配置 storage root 内的共享目录。根挂载标记必须有效。小型探测也使用真实共享输入路径，不仅在远端生成 lavfi。
3. wrapper 必须透传标准 argv、退出码、FFmpeg stdout `-progress pipe:1`，ffprobe 必须输出 JSON。wrapper 的单个调用返回成功应表示远程 FFmpeg 已结束且共享输出可读。
4. 管理员关闭或明确管理 wrapper 自带的本机 fallback；SpringHub 不主动回退，但无法单凭 wrapper 输出证明其内部使用了哪台主机。能力报告只能证明兼容入口和共享路径样例，不证明远端身份。
5. 私钥、known_hosts、SSH 配置/主机池由部署挂载并限制权限，不通过页面、模板 JSON 或 API 保存。保持主机密钥校验；不要禁用 StrictHostKeyChecking。非 root 容器用户必须能读所需只读配置。

### 远程控制的真实边界

远程运行任务**不提供暂停按钮**，API 也拒绝运行态暂停。停止本地 wrapper 不等于停止远端 FFmpeg。排队任务可暂停/取消；运行态取消和超时阻止最终发布、终止本地进程组，但**远端退出未确认**。

取消、超时或失败会保留本任务隐藏 staging；失败探测可能保留 `.springhub-codec-*` 目录。不要自动删除这些目录，先由管理员确认所有远端进程已退出，再手工清理。任务记录的取消表示 SpringHub 停止执行与发布，不代表远端资源已释放。应用无远端节点调度或远端进程管理能力，wrapper 管理主机池。

容器镜像包含 FFmpeg 和 SSH 客户端，**不捆绑特定 rffmpeg 版本和依赖**。需要管理员构建派生镜像或提供可执行 wrapper 及其运行依赖；只挂载一个脚本但缺少依赖不算配置完成。输入/输出共享挂载仍由 Docker/宿主机管理。不要授予 Docker socket、root、安装驱动或挂载特权。

## HTTP API

所有 `/api/v1/*` 沿用 Bearer token 鉴权。外部调用与网页共用队列、路径保护、参数校验、编码器准入和 staging 校验/无覆盖发布。

- `GET /api/v1/engines`
- `GET /api/v1/task-templates`
- `POST /api/v1/spec/validate`：支持 `{engine,template_id,spec,preset_id}`；没有模板时可仅传 spec。`spec` 默认是模板的结构化覆盖，列表（如音轨）整体替换。网页发送 `spec_mode:"replace"` 表示以编辑后的整份 spec 为准，仅保留模板身份；避免清空字段后被原模板重新补回。
- `POST /api/v1/probe`：`{engine,root,path}`。
- `POST /api/v1/jobs`：`{engine,template_id,spec,input,output}`。输入/输出仅配置 root ID 和相对路径。用户模板不可直接换引擎，须先复制/校验；内置模板可用于三种引擎。
- `GET /api/v1/jobs/<id>`、`GET /api/v1/jobs/<id>/logs`；控制沿用 `POST .../start`、`.../pause`、`.../cancel`。
- 返回任务含 `engine`、`template_id`、`template_name`、实际输出路径和 `actions`。客户端必须依据 actions 显示控制能力。

示例（替换模板 UUID、存储根和相对路径）：

```json
{
  "engine": "ffmpeg",
  "template_id": "从模板列表取得的 UUID",
  "input": {"root": "media", "path": "food/demo.mov"},
  "output": {"root": "media", "path": "converted/demo.mp4"},
  "spec": {"video": {"quality": 20}}
}
```

预校验不创建任务，不证明文件、轨道、硬件或实际编码可用；提交和执行前仍复验。HandBrake 预设依赖仍通过原有快照方案执行，不能跨到 FFmpeg。

## 命令行调用

命令行是已运行服务的 HTTP 客户端，不另开数据库或 worker。均输出 JSON，默认 URL 为 `http://127.0.0.1:8080`，可用 `--server` 或 `SPRINGHUB_URL`。令牌从 `SPRINGHUB_API_TOKEN` 读取，不使用命令行 token 参数；客户端拒绝重定向，避免凭据被转发。

从仓库根目录（WSL 开发环境）运行：

```bash
PYTHONPATH=next python3 -m springhub templates --server http://127.0.0.1:8080
```

```bash
PYTHONPATH=next python3 -m springhub engines --server http://127.0.0.1:8080
```

```bash
PYTHONPATH=next python3 -m springhub validate --engine ffmpeg --template TEMPLATE_UUID
```

```bash
PYTHONPATH=next python3 -m springhub submit --engine ffmpeg --template TEMPLATE_UUID --input-root media --input-path food/demo.mov --output-root media --output-path converted/demo.mp4 --wait
```

```bash
PYTHONPATH=next python3 -m springhub status JOB_UUID
```

```bash
PYTHONPATH=next python3 -m springhub logs JOB_UUID
```

支持 `jobs`、`start JOB_UUID`、`pause JOB_UUID`、`cancel JOB_UUID`；`validate`/`submit` 可用 `--spec 文件.json` 提供结构化覆盖。原有无子命令服务启动和 `--check` 仍兼容。

退出码：0 为调用成功（不等待的 submit 仅代表入队）；1 为查询/等待得到失败、取消或中断；2 为参数、HTTP 或网络错误；3 为等待超时，**不取消任务**。`--wait-timeout` 默认 3600 秒；等待期间服务自动启动设置仍生效，手动模式任务需另调用 start。

## 验证范围

真实 CPU 契约工具 `next/tools/verify_playback.py` 在隔离目录生成素材，校验 HandBrake/FFmpeg 各 9 个模板、编码格式/尺寸/位深/AAC/正时长、moov 顺序，并实际运行 FFmpeg 两遍。不是画质评分，不代表所有源格式、设备、GPU、Docker 或真实远程节点验收。回归覆盖 HTTP、CLI、模板只读/复制/导入导出、快照、两遍及远程残留保护。
