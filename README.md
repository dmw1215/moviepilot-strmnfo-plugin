# STRM媒体信息补全（MoviePilot 插件）

MoviePilot 整理入库后，自动从 `.strm` 文件名里的技术标签生成同名的
`.strm.nfo`（Kodi streamdetails 格式），供 Emby 的
[STRM Probe NFO](https://github.com/oxios0x00/emby-strm-probe-nfo) 插件
读取入库，让媒体播放器显示分辨率 / 编码 / HDR / 音频等媒体信息。

**全程只读文件名，不访问云盘、不下载任何文件。**

## 解决的问题

Emby 默认不对 `.strm` 做 ffprobe，所以 `.strm` 条目没有结构化媒体信息，
媒体播放器里看不到 HDR / SDR、编码这些徽标。本插件在 MoviePilot 整理完的那一刻
就把 nfo 写好，Emby 下次扫描时由 STRM Probe NFO 插件读入。

## 工作方式（双保险）

1. **整理完成事件**：MoviePilot 每整理完一部影片 / 一季剧集（`TransferComplete`
   事件），插件立即给本次入库的 `.strm` 生成 nfo；
2. **定时全量扫描**：按配置的 cron 周期（默认每 6 小时）扫一遍监控目录，
   补齐事件漏掉的（比如插件停用期间入库的）。

另外支持「立即运行一次」开关和 `/strmnfo_scan` 远程命令，手动触发全量扫描。

## 安装

本包就是一个完整的 V3 插件仓库：根目录的 `package.v3.json` 是市场索引，
`plugins.v3/strmnfo/` 是插件源码。不要只复制 `strmnfo/` 目录到插件目录，
那样不会被当作已安装插件加载。按下面任一方式安装：

### 方式一：本地插件仓库（推荐，不用 GitHub）

1. 把本压缩包解压到 MoviePilot 容器能看到的某个目录，例如
   `/config/plugin-repo/strmnfo`（解压后该目录下应直接有
   `package.v3.json` 和 `plugins.v3/`）；
2. 在 MoviePilot 的环境变量里加上本地仓库路径（docker-compose 的
   `environment` 里加一行，或写进 `/config/app.env`）：
   `PLUGIN_LOCAL_REPO_PATHS=/config/plugin-repo/strmnfo`
3. 重启 MoviePilot，打开插件市场，就能看到「STRM媒体信息补全」，点安装。

### 方式二：GitHub 插件市场

把本仓库推到 GitHub（main 分支），在 MoviePilot 的 `PLUGIN_MARKET`
环境变量里加上仓库地址（多个地址用英文逗号分隔），然后在插件市场里
安装「STRM媒体信息补全」。按官方文档，插件市场安装需要能正常访问
GitHub，配上 `GITHUB_TOKEN` 可避免 API 限流，`GITHUB_PROXY` 可加速下载。

## 配置

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| 启用插件 | 总开关 | 关 |
| 立即运行一次 | 保存后 3 秒跑一次全量扫描（存量补齐用） | 关 |
| 监控目录 | 每行一个，插件只扫这些目录下的 `.strm` | `/media/Link` |
| 排除目录 | 每行一个，该目录下的 `.strm` 不处理 | 空 |
| 定时全量扫描周期 | cron 表达式 | `0 */6 * * *`（每 6 小时） |
| 覆盖已有 .strm.nfo | 开启后重新生成已有的 nfo | 关 |
| 单次整理事件最多处理 | 一次整理事件里最多处理多少个 `.strm`（防失控） | 500 |
| 全量扫描后发通知 | 扫描完成发一条通知 | 关 |

注意：**监控目录填的是 MoviePilot 容器内能看到的路径**。宿主机的
`/mnt/Link` 在容器内挂载为 `/media/Link`，所以默认值是 `/media/Link`。
如果你的挂载路径不同，在插件配置里改成容器内实际路径。

## Emby 侧（必须做，否则 nfo 不生效）

1. 下载 [StrmProbeNfo.dll](https://github.com/oxios0x00/emby-strm-probe-nfo/releases)
   （v0.2.2），放到 Emby 的 `plugins/` 目录，重启 Emby；
2. Emby → 插件 → STRM Probe NFO → 设置，**选上所有含 `.strm` 的媒体库**，
   点保存（默认一个库都不选，插件什么也不做）；
3. Emby 的媒体库实时监控开着时，新 nfo 落盘就会触发扫描并读入；
   没开实时监控时，由 STRM Probe NFO 插件的每日定时任务兜底。

要求 Emby Server **4.10.1.0** 或更高（.NET 8）。

## 能识别的文件名标签

- 分辨率：8K / 4K / UHD / 2160p / 1080p / FHD / 1080i / 720p / 720i / 480p，
  以及文件名里显式的 `3840x2160` / `3832x1600`（优先）
- 视频编码：AV1 / HEVC / H.265 / H.264 / AVC / x264 / x265 / VP9 / VC-1
- HDR：Dolby Vision / DV / DoVi / HDR10+ / HDR10 / HLG / SDR（DV 优先于 HDR）
- 音频编码（可多轨）：TrueHD / DTS-HD MA / DTS-HD HR / DTS / DTS:X /
  EAC3 / DDP / DD+ / AC3 / AAC / FLAC / MP3 / Opus，每轨各自带声道
- 音效标记：Dolby Atmos / DTS:X（只做标记写进 nfo 注释，不改编码本身，
  因为 nfo 格式没有这两个字段，媒体播放器不会因此显示 Atmos 徽标）
- 声道：2.0 / 5.1 / 7.1（接受 `DDP5.1` 这种无分隔写法）
- 音轨语言：国语 / 普通话 / 粤语 / 英语 / 日语 / 韩语（明确标记才写）
- 帧率：23.976 / 24 / 25 / 30 / 50 / 60 fps（`DV.P5.60fps` 正确识别为 60，
  不会误读成 5.6）
- 码率：`22Mbps` / `22.74Mbps`（写入视频 bitrate）
- 字幕：中字 / 简体 / 繁体 / 英字 / 双语

码深 / 配置 / 色域（10bit / Main 10 / bt2020）只识别不写入 nfo，
因为插件读的 nfo 格式没有这些字段。

## 已知限制

- **文件名里没有任何技术标签的文件生成不了**。比如只有片名 + 集数的
  `180天重启计划(2025).S01E01.strm`，软链接名和 openstrm 原文件名都没有
  标签，文件名方式无解，只能以后单独探测（ffprobe）或留空。
  实测一个 26974 个 `.strm` 的库：21386 个可生成（79%），5588 个无法解析。
- **分辨率是标称值**：`2160p` → `3840x2160`，真实探测才会给精确裁切值
  （如 `3832x1600`）；Main 10 / bt2020 / 采样率等文件名里没有的信息
  也写不进去。这是文件名方式本身的上限。
- **DTS-HD MA 写成 `dtshd`**（Kodi 标准）。如果媒体播放器显示异常或 Emby
  尝试转码，把 `nfo.py` 里 `AUDIO_CODEC_MAP` 的 `dtshd` 改回 `dts` 即可。
- **MoviePilot 整理时如果不经过 TransferComplete 事件**（个别整理路径），
  本次入库的 nfo 由定时全量扫描补上，最多晚一个周期。
- **覆盖策略**：默认不覆盖已有 `.strm.nfo`（增量跳过，重复运行安全）；
  只有打开「覆盖已有 .strm.nfo」才会重新生成。写入是原子的（临时文件
  + rename），Emby 不会读到半个 nfo。

## 版本历史

- **v1.0.3**：文案调整，REX 播放器统一改称媒体播放器。
- **v1.0.2**：补上插件图标 scraper.svg（插件目录与仓库 icons/ 各一份），
  修复插件列表图标 404。
- **v1.0.1**：修复扫描死锁（锁改为可重入 RLock）；默认监控目录改为容器内
  路径 `/media/Link`；补齐 `stop_service` 资源释放；事件路径提取兼容
  字典条目；nfo 原子写入。
- **v1.0.0**：初始版本。

## 文件结构

```
package.v3.json                  # 插件市场清单（V3）
plugins.v3/
  strmnfo/
    __init__.py                  # 插件主程序（事件 + 定时扫描 + 配置表单 + 记录页）
    nfo.py                       # 文件名解析 + nfo 生成（与 strm_nfo_gen.py v3.1 同规则）
```

## 致谢

插件框架参考了
[StrmWatcher](https://github.com/000515aa/moviepilot-plugin-strmwatcher)
（定时扫描、表单、记录页结构）和
[TgtoDriveSync](https://github.com/KingNuoNuo/MoviePilot-Plugins)
（TransferComplete 事件、防抖与后台线程模式）。
