#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""STRM媒体信息补全插件的离线验证（不依赖真实 MoviePilot）。

验证内容：
  1. nfo.py 解析：v3.0/v3.1 的全部关键用例（含 DV.P5.60fps 帧率修复）。
  2. __init__.py 导入：用桩模块模拟 app.*，确认插件类能加载、init_plugin、
     get_form/get_service/get_page 正常工作。
  3. 端到端：用桩模块 + 临时目录（含软链接 .strm）跑一次 _do_scan，
     确认 .strm.nfo 真实生成且内容正确；再跑一次确认增量跳过。
  4. TransferComplete 事件：模拟对象形式与字典形式的 transferinfo，
     确认 _extract_strm_paths 能提取出目标路径。
"""
import json
import os
import sys
import tempfile
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent / "plugins.v3" / "strmnfo"
sys.path.insert(0, str(PLUGIN_DIR))

# ---------------- 桩 app.* 模块 ----------------

# app.sdk.plugin._PluginBase
app = types.ModuleType("app")
app_plugins = types.ModuleType("app.sdk.plugin")


class _PluginBase:
    """最小桩：只实现插件用到的基类方法。"""

    def __init__(self):
        self._data = {}

    def stop_service(self):
        pass

    def update_config(self, config):
        self._saved_config = config

    def get_data(self, key):
        return self._data.get(key)

    def save_data(self, key, value):
        self._data[key] = value

    def post_message(self, mtype=None, title=None, text=None):
        self._last_message = (mtype, title, text)


app_plugins._PluginBase = _PluginBase

# app.sdk.events
app_sdk_events = types.ModuleType("app.sdk.events")


class _EventManager:
    def __init__(self):
        self.handlers = {}

    def register(self, etype):
        def decorator(f):
            self.handlers.setdefault(getattr(etype, "value", str(etype)), []).append(f)
            return f
        return decorator


class Event:
    def __init__(self, event_type=None):
        self.event_type = event_type
        self.event_data = {}


app_sdk_events.eventmanager = _EventManager()
app_sdk_events.Event = Event

# app.schemas.types
app_schemas = types.ModuleType("app.schemas")
app_schemas_types = types.ModuleType("app.schemas.types")


class _EventType:
    class _Val:
        def __init__(self, v):
            self.value = v

    TransferComplete = _Val("TransferComplete")
    PluginAction = _Val("PluginAction")


class _NotificationType:
    Plugin = "Plugin"


app_schemas_types.EventType = _EventType
app_schemas_types.NotificationType = _NotificationType
app_schemas.types = app_schemas_types

# app.sdk.config / app.sdk.logging
app_sdk = types.ModuleType("app.sdk")
app_sdk_config = types.ModuleType("app.sdk.config")
app_sdk_logging = types.ModuleType("app.sdk.logging")


class _Settings:
    TZ = "Asia/Shanghai"


class _Logger:
    def info(self, *a):
        print("[info]", *a)

    def warning(self, *a):
        print("[warn]", *a)

    def error(self, *a):
        print("[error]", *a)

    def debug(self, *a):
        pass


app_sdk_config.settings = _Settings()
app_sdk_logging.logger = _Logger()
app_sdk.config = app_sdk_config
app_sdk.logging = app_sdk_logging
app_sdk.events = app_sdk_events
app_sdk.plugin = app_plugins

for name, mod in [
    ("app", app),
    ("app.sdk.plugin", app_plugins),
    ("app.sdk.events", app_sdk_events),
    ("app.schemas", app_schemas),
    ("app.schemas.types", app_schemas_types),
    ("app.sdk", app_sdk),
    ("app.sdk.config", app_sdk_config),
    ("app.sdk.logging", app_sdk_logging),
]:
    sys.modules[name] = mod

# apscheduler 桩（MoviePilot 自带，本机没有，桩一个最小实现）
apscheduler = types.ModuleType("apscheduler")
apscheduler_schedulers = types.ModuleType("apscheduler.schedulers")
apscheduler_schedulers_bg = types.ModuleType("apscheduler.schedulers.background")
apscheduler_triggers = types.ModuleType("apscheduler.triggers")
apscheduler_triggers_cron = types.ModuleType("apscheduler.triggers.cron")


class _BackgroundScheduler:
    def __init__(self, timezone=None):
        self.jobs = []

    def add_job(self, func=None, trigger=None, run_date=None, name=None):
        self.jobs.append((func, trigger, run_date, name))

    def start(self):
        self.started = True

    def shutdown(self, wait=True):
        self.started = False


class _CronTrigger:
    def __init__(self, expr=""):
        self.expr = expr

    @classmethod
    def from_crontab(cls, expr):
        return cls(expr)


apscheduler_schedulers_bg.BackgroundScheduler = _BackgroundScheduler
apscheduler_triggers_cron.CronTrigger = _CronTrigger
apscheduler.schedulers = apscheduler_schedulers
apscheduler.triggers = apscheduler_triggers
apscheduler_schedulers.background = apscheduler_schedulers_bg
apscheduler_triggers.cron = apscheduler_triggers_cron
for _name, _mod in [
    ("apscheduler", apscheduler),
    ("apscheduler.schedulers", apscheduler_schedulers),
    ("apscheduler.schedulers.background", apscheduler_schedulers_bg),
    ("apscheduler.triggers", apscheduler_triggers),
    ("apscheduler.triggers.cron", apscheduler_triggers_cron),
]:
    sys.modules[_name] = _mod

# ---------------- 1. nfo.py 解析用例 ----------------
from nfo import parse_filename, build_nfo, generate_nfo_for_strm  # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  FAIL {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ok {label}")


print("== 1. nfo 解析 ==")
cases = [
    # (文件名 stem, 期望的关键字段)
    ("一瓯春.Spring.of.the.Blade.2026.S01E01.2160p.WEB-DL.DV.P5.60fps.H265.10bit.AAC2.0.[tmdbid=294990]",
     {"width": 3840, "height": 2160, "video_codec": "hevc", "hdr": "dolbyvision",
      "framerate": 60.0, "bitdepth": 10}),
    ("Movie.2024.2160p.WEB-DL.DV.HDR10.23.976fps.H265.TrueHD.7.1.Atmos",
     {"framerate": 23.976, "hdr": "dolbyvision"}),
    ("一笑随歌.Fated.Hearts.S01E01.2025.2160p.WEB-DL.DDP5.1.HDR.Vivid.H.265.[tmdbid=272484]",
     {"hdr": "hdr10"}),
    ("Movie.2160p.BluRay.DV.P7.24fps.H265.DTS-HD.MA.5.1",
     {"framerate": 24.0, "hdr": "dolbyvision"}),
    ("Movie.1080p24fps.H264.AC3.5.1",
     {"framerate": 24.0, "video_codec": "h264"}),
    ("生化危机：爆发夜.2160p.WEB-DL.22.74Mbps.H265.DDP5.1",
     {"video_bitrate": 22740000}),
    ("Movie.2160p.WEB-DL.H265.TrueHD.7.1.Atmos.AC3.5.1",
     {"video_codec": "hevc"}),
]
for stem, want in cases:
    info = parse_filename(stem)
    for k, v in want.items():
        check(f"{stem[:30]}.. {k}", info.get(k), v)
    if "DDP" in stem or "TrueHD" in stem:
        tracks = info.get("audio_tracks") or []
        print(f"     audio_tracks={tracks}")

# 多轨
info = parse_filename("Movie.2160p.WEB-DL.H265.TrueHD.7.1.Atmos.AC3.5.1")
check("多轨数量", len(info.get("audio_tracks", [])), 2)
check("多轨1", (info["audio_tracks"][0]["codec"], info["audio_tracks"][0]["channels"], info["audio_tracks"][0]["atmos"]),
      ("truehd", 8, True))
check("多轨2", (info["audio_tracks"][1]["codec"], info["audio_tracks"][1]["channels"]),
      ("ac3", 6))

# DDP 全写法
for variant in ["DDP5.1", "DDP 5.1", "DDP.5.1", "DDP-5.1", "DDP_5.1",
                "DD+5.1", "DD+ 5.1", "EAC3 5.1", "EAC35.1", "Dolby Digital Plus 5.1"]:
    info = parse_filename(f"Movie.2160p.WEB-DL.{variant}.H265")
    t = (info.get("audio_tracks") or [{}])[0]
    check(f"DDP变体 {variant}", (t.get("codec"), t.get("channels")), ("eac3", 6))

# DTS:X 标记
info = parse_filename("Movie.2160p.BluRay.DTS-HD.MA.7.1.DTS-X.H265")
t = (info.get("audio_tracks") or [{}])[0]
check("DTS:X", (t.get("codec"), t.get("channels"), t.get("dtsx")), ("dtshd", 8, True))
info = parse_filename("Movie.2160p.BluRay.DTS.X264.H265")
check("DTS x264 不误判 DTS:X", any(tr.get("dtsx") for tr in info.get("audio_tracks", [])), False)

# NFO 生成有效性
info = parse_filename("一瓯春.Spring.of.the.Blade.2026.S01E01.2160p.WEB-DL.DV.P5.60fps.H265.10bit.AAC2.0.[tmdbid=294990]")
xml = build_nfo(info, episode=True)
check("NFO含dolbyvision", "dolbyvision" in xml, True)
check("NFO含3840", "<width>3840</width>" in xml, True)
check("NFO根元素剧集", xml.startswith('<?xml') and "<episodedetails>" in xml, True)

# ---------------- 2. 插件导入与配置 ----------------
print("== 2. 插件导入与配置 ==")
import importlib.util  # noqa: E402

spec = importlib.util.spec_from_file_location("strmnfo_init", PLUGIN_DIR / "__init__.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
StrmNfo = mod.StrmNfo
check("插件名", StrmNfo.plugin_name, "STRM媒体信息补全")
check("插件版本", StrmNfo.plugin_version, "1.0.3")

plugin = StrmNfo()
plugin.init_plugin({
    "enabled": True, "onlyonce": False, "watch_paths": "/media/Link",
    "exclude_paths": "", "cron": "0 */6 * * *", "force": False,
    "max_per_run": 500, "notify": False,
})
check("启用状态", plugin.get_state(), True)
check("命令注册", len(plugin.get_command()), 1)
check("服务注册", len(plugin.get_service()), 1)
form, defaults = plugin.get_form()
check("表单默认监控目录", defaults.get("watch_paths"), "/media/Link")
page = plugin.get_page()
check("详情页可生成", isinstance(page, list) and len(page) == 1, True)

# ---------------- 3. 端到端扫描（临时目录 + 软链接） ----------------
print("== 3. 端到端扫描 ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "Link"
    show = root / "一瓯春 (2026) [tmdbid=294990]" / "Season 1"
    show.mkdir(parents=True)
    # openstrm 原文件（带技术标签）
    orig_dir = Path(tmp) / "openstrm"
    orig_dir.mkdir()
    orig = orig_dir / "一瓯春.Spring.of.the.Blade.2026.S01E01.2160p.WEB-DL.DV.P5.60fps.H265.10bit.AAC2.0.strm"
    orig.write_text("https://example.com/video.mkv", encoding="utf-8")
    # MoviePilot 软链接：名字干净（无标签），靠跟目标解析
    link_clean = show / "一瓯春(2026).S01E01.strm"
    os.symlink(orig, link_clean)
    # 软链接：名字本身带标签
    link_tagged = show / "一笑随歌.Fated.Hearts.S01E01.2025.2160p.WEB-DL.DDP5.1.HDR.Vivid.H.265.strm"
    os.symlink(orig, link_tagged)
    # 无法解析的干净名（目标也没有标签）
    orig_clean = orig_dir / "180天重启计划.S01E01.strm"
    orig_clean.write_text("https://example.com/x.mkv", encoding="utf-8")
    link_unparsed = show / "180天重启计划(2025).S01E01.strm"
    os.symlink(orig_clean, link_unparsed)

    plugin._watch_paths = str(root)
    plugin._force = False
    plugin._do_scan(trigger="test")
    nfo_clean = link_clean.with_suffix(".strm.nfo")
    nfo_tagged = link_tagged.with_suffix(".strm.nfo")
    nfo_unparsed = link_unparsed.with_suffix(".strm.nfo")
    check("软链接干净名靠目标生成", nfo_clean.exists(), True)
    check("软链接带标签名生成", nfo_tagged.exists(), True)
    check("无法解析的不生成", nfo_unparsed.exists(), False)
    if nfo_clean.exists():
        content = nfo_clean.read_text(encoding="utf-8")
        check("干净名nfo帧率60", "<framerate>60.0</framerate>" in content, True)
        check("干净名nfo DV", "dolbyvision" in content, True)
    stats = plugin.get_data("stats") or {}
    check("统计生成2", stats.get("generated"), 2)
    check("统计无法解析1", stats.get("unparsed"), 1)
    # 再跑一次：增量跳过
    plugin._do_scan(trigger="test2")
    stats2 = plugin.get_data("stats") or {}
    check("第二轮跳过2", stats2.get("skipped"), 2)
    check("第二轮不再生成", stats2.get("generated"), 2)

# ---------------- 4. TransferComplete 事件提取 ----------------
print("== 4. TransferComplete 事件提取 ==")


class _TI:
    def __init__(self):
        self.target_path = "/mnt/Link/电影/阿凡达 (2009)/阿凡达.2160p.WEB-DL.DV.HDR10.H265.strm"
        self.file_list_new = [
            "/mnt/Link/电影/阿凡达 (2009)/阿凡达.2160p.WEB-DL.DV.HDR10.H265.strm",
            "/mnt/Link/电视剧/一瓯春/Season 1/一瓯春.S01E01.strm",
        ]


paths = StrmNfo._extract_strm_paths({"transferinfo": _TI()})
check("对象形式提取2条", len(paths), 2)
check("对象形式含target_path", paths[0].endswith("阿凡达.2160p.WEB-DL.DV.HDR10.H265.strm"), True)

paths2 = StrmNfo._extract_strm_paths({"transferinfo": {
    "target_path": "/mnt/Link/电影/阿凡达 (2009)/阿凡达.2160p.WEB-DL.DV.HDR10.H265.strm",
    "file_list_new": [],
}})
check("字典形式提取1条", len(paths2), 1)
check("空transferinfo", StrmNfo._extract_strm_paths({}), [])
check("None transferinfo", StrmNfo._extract_strm_paths({"transferinfo": None}), [])
paths3 = StrmNfo._extract_strm_paths({"transferinfo": {
    "target_path": ["/media/Link/电影/a.strm", "/media/Link/电影/b.strm"],
    "file_list_new": [
        {"path": "/media/Link/电视剧/c.strm"},
        {"filepath": "/media/Link/电视剧/d.strm"},
        "",
    ],
}})
check("字典条目+列表target提取4条", len(paths3), 4)

# ---------------- 6. TransferComplete 事件端到端 ----------------
print("== 6. TransferComplete 事件端到端 ==")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "Link"
    show = root / "电影" / "阿凡达 (2009)"
    show.mkdir(parents=True)
    strm = show / "阿凡达.2160p.WEB-DL.DV.HDR10.H265.TrueHD.7.1.Atmos.strm"
    strm.write_text("https://example.com/avatar.mkv", encoding="utf-8")

    class _TI2:
        def __init__(self):
            self.target_path = str(strm)
            self.file_list_new = [str(strm)]

    plugin._watch_paths = str(root)
    ev = Event("TransferComplete")
    ev.event_data = {"transferinfo": _TI2(), "mediainfo": None}
    plugin.on_transfer_complete(ev)
    # 等后台线程跑完
    plugin._executor.shutdown(wait=True)
    plugin._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="strmnfo")
    nfo = strm.with_suffix(".strm.nfo")
    check("事件触发后生成nfo", nfo.exists(), True)
    if nfo.exists():
        content = nfo.read_text(encoding="utf-8")
        check("事件nfo含DV", "dolbyvision" in content, True)
        check("事件nfo含TrueHD", "truehd" in content, True)

# ---------------- 5. package.v3.json ----------------
print("== 5. package.v3.json ==")
pkg = json.loads((Path(__file__).resolve().parent / "package.v3.json").read_text(encoding="utf-8"))
check("清单含StrmNfo", "StrmNfo" in pkg, True)
check("清单版本", pkg["StrmNfo"]["version"], "1.0.3")

# ---------------- 7. v1.0.1 回归：死锁、stop_service、原子写入 ----------------
print("== 7. v1.0.1 回归 ==")
import threading  # noqa: E402

# 7a. 死锁回归：扫描中既有可生成文件（走 _save_run），也有无法解析文件
# （走 _record_failure），两者都在扫描持锁期间再次加锁；普通 Lock 会卡死。
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp) / "Link"
    root.mkdir(parents=True)
    ok = root / "电影.2160p.WEB-DL.HDR.H265.strm"
    ok.write_text("https://example.com/a.mkv", encoding="utf-8")
    bad = root / "干净名字.S01E01.strm"
    bad.write_text("https://example.com/b.mkv", encoding="utf-8")
    plugin._watch_paths = str(root)
    plugin._force = False
    t = threading.Thread(target=lambda: plugin._do_scan(trigger="deadlock-test"))
    t.start()
    t.join(timeout=10)
    check("扫描不死锁（10秒内结束）", not t.is_alive(), True)
    check("死锁回归：可生成文件已生成", ok.with_suffix(".strm.nfo").exists(), True)
    check("死锁回归：无临时文件残留", list(root.rglob("*.tmp")), [])

# 7b. stop_service 释放资源，且可重新初始化
plugin2 = StrmNfo()
plugin2.init_plugin({
    "enabled": True, "onlyonce": True, "watch_paths": "/media/Link",
    "exclude_paths": "", "cron": "0 */6 * * *", "force": False,
    "max_per_run": 500, "notify": False,
})
check("stop前启用", plugin2.get_state(), True)
check("stop前调度器已建", plugin2._scheduler is not None, True)
plugin2.stop_service()
check("stop后停用", plugin2.get_state(), False)
check("stop后线程池释放", plugin2._executor, None)
check("stop后调度器释放", plugin2._scheduler, None)
plugin2.init_plugin({
    "enabled": True, "onlyonce": False, "watch_paths": "/media/Link",
    "exclude_paths": "", "cron": "0 */6 * * *", "force": False,
    "max_per_run": 500, "notify": False,
})
check("重初始化后启用", plugin2.get_state(), True)
check("重初始化后线程池重建", plugin2._executor is not None, True)
plugin2.stop_service()

print()
if failures:
    print(f"失败 {len(failures)} 项")
    sys.exit(1)
print("全部通过")
