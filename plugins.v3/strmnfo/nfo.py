# -*- coding: utf-8 -*-
"""
从 .strm 生成 .strm.nfo（Kodi streamdetails 格式），供 Emby 的
STRM Probe NFO 插件读取入库。

两种取数模式：

1. 文件名模式（默认，v1.0.x 行为不变）
   —— 只读文件名里的技术标签，不访问网络、不下载任何文件。
   上限：分辨率是标称值（2160p -> 3840x2160）、码率/时长只能靠文件名里
   显式写明，色深/配置/色域只识别不写。

2. 探测模式（v1.1.0 新增，可选开关）
   —— 读 .strm 里第一行的 URL，用 HTTP Range 只拉几百 KB 头部，
   再用 ffprobe 解析真实参数。拿到：精确宽高（如 3840x1608）、真实视频码率、
   帧率、HDR/SDR/DV、色深、色彩原色/传递函数/色彩空间、像素格式、Profile、
   每条音轨的编码/声道/声道布局/码率/语言/采样率/标题/默认标记、字幕轨
   编码与语言/标题/默认标记。
   总码率、文件体积、真实时长。

两种模式产出的都是同一套 Kodi 字段，差别在于值准不准；探测模式还会额外
写扩展字段（bitdepth / colourprimaries / colourtransfer / colourspace /
pixelformat / profile / samplerate / title / channellayout / default），
这些字段 Kodi/Emby 原生不认，需要 Emby 侧插件（STRM Probe NFO）扩展
解析才能显示，未扩展时是无害的附加信息。
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ---------------- 映射表 ----------------

RESOLUTIONS = {
    "2160p": (3840, 2160),
    "1080p": (1920, 1080),
    "1080i": (1920, 1080),
    "720p": (1280, 720),
    "720i": (1280, 720),
    "480p": (720, 480),
    "576p": (720, 576),
    "4k": (3840, 2160),
    "8k": (7680, 4320),
    "uhd": (3840, 2160),
    "fhd": (1920, 1080),
}

VIDEO_CODECS = [
    (r"h\.?265|hevc|x265", "hevc"),
    (r"h\.?264|avc|x264", "h264"),
    (r"\bav1\b", "av1"),
    (r"\bvp9\b", "vp9"),
    (r"mpeg-?2", "mpeg2"),
    (r"vc-?1", "vc1"),
]

# 音频编码：组合正则（顺序重要，先匹配更具体的），以及匹配文本到标准名的映射。
AUDIO_CODEC_RE = re.compile(
    r"truehd|true-hd"
    r"|dts-?hd[\s._-]*ma|dtshdma"
    r"|dts-?hd[\s._-]*hr"
    r"|dts-?hd"
    r"|eac3|ddp|dd\+|dolby[\s._-]*digital[\s._-]*plus|dolby\+"
    r"|dts(?![\s._\-:]*x(?![a-z0-9]))"
    r"|ac3|dolby[\s._-]*digital"
    r"|aac|flac|mp3|opus",
    re.IGNORECASE,
)

AUDIO_CODEC_MAP = [
    (r"truehd|true-hd", "truehd"),
    (r"dts-?hd[\s._-]*ma|dtshdma", "dtshd"),   # DTS-HD Master Audio
    (r"dts-?hd[\s._-]*hr", "dtshd"),           # DTS-HD High Resolution
    (r"dts-?hd", "dtshd"),                     # DTS-HD（未注明 MA/HR）
    (r"eac3|ddp|dd\+|dolby[\s._-]*digital[\s._-]*plus|dolby\+", "eac3"),
    (r"dts", "dts"),
    (r"ac3|dolby[\s._-]*digital", "ac3"),
    (r"aac", "aac"),
    (r"flac|flac", "flac"),
    (r"mp3", "mp3"),
    (r"opus", "opus"),
]

# ffprobe 的 codec_name -> NFO 里的标准名
FFPROBE_VIDEO_CODEC = {
    "hevc": "hevc", "h265": "hevc",
    "h264": "h264", "avc": "h264",
    "av1": "av1", "vp9": "vp9",
    "mpeg2video": "mpeg2", "vc1": "vc1",
}
FFPROBE_AUDIO_CODEC = {
    "aac": "aac", "eac3": "eac3", "ac3": "ac3",
    "truehd": "truehd", "dts": "dts", "dtshd": "dtshd",
    "flac": "flac", "mp3": "mp3", "opus": "opus",
}
FFPROBE_SUB_CODEC = {
    "subrip": "subrip", "srt": "subrip",
    "ass": "ass", "ssa": "ass",
    "hdmv_pgs_subtitle": "hdmv_pgs_subtitle",
    "dvd_subtitle": "dvd_subtitle",
    "mov_text": "mov_text",
    "webvtt": "webvtt",
}

# 声道：5.1 -> 6，7.1 -> 8，2.0 -> 2。
# 数字前不要求分隔符（修 DDP5.1），但前后不能紧贴其他数字，
# 避免把日期（2024.11）或版本号误判为声道。
CHANNEL_RE = re.compile(r"(?<![0-9])([1-9])\.([01])(?![0-9])")


def _codec_name(matched_text: str):
    low = matched_text.lower()
    for pat, name in AUDIO_CODEC_MAP:
        if re.search(pat, low):
            return name
    return None


def parse_audio_tracks(low: str) -> list:
    """多轨解析：找出文件名里所有音频编码，各自配上附近的声道。

    每条音轨 = {"codec", "channels", "atmos", "dtsx"}。
    """
    matches = list(AUDIO_CODEC_RE.finditer(low))
    atmos_pos = [m.start() for m in re.finditer(r"atmos", low)]
    dtsx_pos = [m.start() for m in re.finditer(r"dts[\s._\-:]*x(?![a-z0-9])", low)]
    tracks = []
    for i, m in enumerate(matches):
        codec = _codec_name(m.group(0))
        if codec is None:
            continue
        seg_end = matches[i + 1].start() if i + 1 < len(matches) else len(low)
        seg = low[m.end():seg_end]
        channels = None
        cm = CHANNEL_RE.search(seg)
        if cm:
            channels = int(cm.group(1)) + (1 if cm.group(2) == "1" else 0)
        else:
            prev_end = matches[i - 1].end() if i > 0 else 0
            cm2 = CHANNEL_RE.search(low[prev_end:m.start()])
            if cm2:
                channels = int(cm2.group(1)) + (1 if cm2.group(2) == "1" else 0)
        atmos = any(m.start() <= p < seg_end for p in atmos_pos)
        dtsx = any(m.start() <= p < seg_end for p in dtsx_pos)
        tracks.append(
            {"codec": codec, "channels": channels, "atmos": atmos, "dtsx": dtsx}
        )
    if not tracks and dtsx_pos:
        p = dtsx_pos[0]
        cm = CHANNEL_RE.search(low[p:])
        channels = None
        if cm:
            channels = int(cm.group(1)) + (1 if cm.group(2) == "1" else 0)
        tracks.append(
            {"codec": "dtshd", "channels": channels, "atmos": False, "dtsx": True}
        )
    return tracks


# ---------------- 解析（文件名） ----------------


def parse_filename(stem: str) -> dict:
    """从 .strm 文件名（不含扩展名）里解析技术标签，返回 nfo 字段字典。"""
    name = stem
    low = name.lower()
    info: dict = {}

    # 分辨率：先找 3840x2160 / 3832x1600 这种显式宽高，再找 2160p / 4K / UHD
    m = re.search(r"(\d{3,4})x(\d{3,4})", name)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    else:
        for tag, (w, h) in RESOLUTIONS.items():
            if re.search(r"\b" + re.escape(tag) + r"\b", low):
                info["width"], info["height"] = w, h
                break

    # 视频编码
    for pat, codec in VIDEO_CODECS:
        if re.search(pat, low):
            info["video_codec"] = codec
            break

    # HDR：Dolby Vision 最优先（独立 DV / DoVi / Dolby Vision 都算），
    # 再依次是 HDR10+、HDR10、HLG、裸 HDR（按 HDR10 计）、SDR。
    if re.search(r"dolby[\s._-]*vision|dovi|(?<![a-z0-9])dv(?![a-z0-9])", low):
        info["hdr"] = "dolbyvision"
    elif re.search(r"hdr10\s*\+|hdr10[\s._-]*plus", low):
        info["hdr"] = "hdr10plus"
    elif re.search(r"hdr10", low):
        info["hdr"] = "hdr10"
    elif re.search(r"\bhlg\b", low):
        info["hdr"] = "hlg"
    elif re.search(r"\bhdr\b", low):
        info["hdr"] = "hdr10"
    elif re.search(r"\bsdr\b", low):
        info["hdr"] = "sdr"

    # 帧率：23.976fps / 24fps / 25fps / 30fps / 50fps / 60fps
    fps_src = re.sub(r"(?<![a-z0-9])p[578](?![a-z0-9])", "", low)
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", fps_src)
    if m:
        info["framerate"] = float(m.group(1))

    # 码率：22Mbps / 22.74Mbps -> 视频 bitrate（bps）
    m = re.search(r"(\d+(?:\.\d+)?)\s*mbps", low)
    if m:
        info["video_bitrate"] = int(float(m.group(1)) * 1_000_000)

    # 码深 / 配置 / 色域：识别进 info（探测模式可直接写入 nfo 扩展字段）
    m = re.search(r"(\d+)\s*-?\s*bit", low)
    if m:
        info["bitdepth"] = int(m.group(1))
    m = re.search(r"\b(main|high)\s*10\b", low)
    if m:
        info["profile"] = m.group(0).title()
        info.setdefault("bitdepth", 10)
    if re.search(r"bt[\s._-]*2020|rec[\s._-]*2020", low):
        info["color"] = "bt2020"
    elif re.search(r"bt[\s._-]*709|rec[\s._-]*709", low):
        info["color"] = "bt709"

    # 音频：多轨解析
    tracks = parse_audio_tracks(fps_src)
    if tracks:
        info["audio_tracks"] = tracks

    # 音轨语言：只认明确标记，没有就不写。
    lang_src = re.sub(
        r"中文字幕|英文字幕|日文字幕|韩文字幕|中英字幕|简体字幕|繁体字幕"
        r"|双语字幕|中字|英字",
        "",
        name,
    )
    lang_low = lang_src.lower()
    if re.search(r"国语|普通话|中配|中文配音|国粤", lang_src):
        info["audio_lang"] = "chi"
    elif re.search(r"粤语|粤語", lang_src):
        info["audio_lang"] = "yue"
    elif re.search(r"英语|英文|english", lang_low):
        info["audio_lang"] = "eng"
    elif re.search(r"日语|日文|japanese", lang_low):
        info["audio_lang"] = "jpn"
    elif re.search(r"韩语|韩文|korean", lang_low):
        info["audio_lang"] = "kor"

    # 时长：45分钟 / 90min / 01:23:45
    m = re.search(r"(\d+)\s*(?:分钟|min)", name)
    if m:
        info["duration"] = int(m.group(1)) * 60.0
    else:
        m = re.search(r"(\d{1,2}):(\d{2}):(\d{2})", name)
        if m:
            info["duration"] = (
                int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            )

    # 字幕：可多条
    chi_sub = any(k in name for k in ("中字", "简体", "繁体", "繁體", "中文字幕"))
    eng_sub = any(k in name for k in ("英字", "英文字幕"))
    if any(k in name for k in ("中英", "双语")):
        chi_sub = True
        eng_sub = True
    subs = []
    if chi_sub:
        subs.append(("subrip", "chi"))
    if eng_sub:
        subs.append(("subrip", "eng"))
    if subs:
        info["subtitles"] = subs

    return info


def is_episode(stem: str) -> bool:
    """判断是不是剧集（决定 nfo 根元素用 episodedetails 还是 movie）。"""
    low = stem.lower()
    return bool(
        re.search(r"\bs\d{1,2}e\d{1,2}\b", low)
        or re.search(r"\be\d{1,2}\b", low)
        or re.search(r"第\s*\d+\s*[集話话]", stem)
    )


# ---------------- 探测（真实参数） ----------------

# 只拉头部，不下载整集。900KB 足够 ffprobe 认全视频/音频/字幕轨。
PROBE_BYTES = 900_000
# 单次探测超时（秒），网络慢时自动降级回文件名模式。
PROBE_TIMEOUT = 25
# 语言代码：ffprobe 给 ISO639-2，这里统一成 Emby 认的两位/三位码
LANG_MAP = {
    "chi": "chi", "zho": "chi", "cmn": "chi", "zh": "chi", "chs": "chi", "cht": "chi",
    "eng": "eng", "en": "eng",
    "jpn": "jpn", "ja": "jpn",
    "kor": "kor", "ko": "kor",
    "yue": "yue", "cantonese": "yue",
    "und": None, "": None, None: None,
}


def _ffprobe_bin():
    """找一个可用的 ffprobe：环境变量 > PATH > 常见路径。"""
    env = os.environ.get("FFPROBE_PATH")
    if env and (os.path.isfile(env) or shutil.which(env)):
        return env
    p = shutil.which("ffprobe")
    if p:
        return p
    for cand in ("/usr/local/bin/ffprobe", "/usr/bin/ffprobe",
                 "/opt/homebrew/bin/ffprobe", "/ffprobe", "/app/ffprobe"):
        if os.path.isfile(cand):
            return cand
    return None


def _read_strm_url(strm_path):
    """读 .strm 里的第一行非空内容，作为媒体地址。"""
    try:
        text = Path(strm_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _fetch_head(url, max_bytes=PROBE_BYTES, timeout=PROBE_TIMEOUT):
    """HTTP Range 只取头部若干字节，写到临时文件；返回 (路径, 字节数,
    是否被截断, 文件真实总大小）。

    服务器不支持 Range（回 200）时，读到 max_bytes 就主动断开，绝不拉整集。
    真实总大小从响应头里拿：206 时看 Content-Range 的总量
    （bytes 0-899999/26450000000），200 时看 Content-Length；
    两个头都没有（比如 chunked）就返回 None，调用方按拿不到处理。
    注意 206 的 Content-Length 只是本次范围的长度，不能当总量用。
    """
    req = urllib.request.Request(url, headers={
        "Range": "bytes=0-%d" % (max_bytes - 1),
        "User-Agent": "Mozilla/5.0",
    })
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".part")
    tmp_path = handle.name
    served = 0
    truncated = False
    total_size = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 先读响应头：真实大小与本次读到多少无关
            total_size = _total_size_from_headers(resp)
            while served < max_bytes:
                chunk = resp.read(min(65536, max_bytes - served))
                if not chunk:
                    break
                handle.write(chunk)
                served += len(chunk)
            if served >= max_bytes:
                truncated = True
    except Exception:
        try:
            handle.close()
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        raise
    finally:
        try:
            handle.close()
        except Exception:
            pass
    return tmp_path, served, truncated, total_size


def _total_size_from_headers(resp):
    """从 HTTP 响应头解析文件真实总大小（字节），拿不到返回 None。"""
    try:
        # 206：Content-Range: bytes 0-899999/26450000000
        cr = resp.headers.get("Content-Range") if resp.headers else None
        if cr:
            m = re.search(r"/(\d+)\s*$", cr)
            if m:
                return int(m.group(1))
        # 200：整文件回包，Content-Length 就是总量
        status = getattr(resp, "status", None)
        if status == 200 and resp.headers:
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
    except (ValueError, TypeError, AttributeError):
        return None
    return None


def _fps(rate):
    """'25/1' -> 25.0；'30000/1001' -> 29.97。"""
    try:
        if "/" in str(rate):
            num, den = str(rate).split("/")[:2]
            den = float(den)
            return round(float(num) / den, 3) if den else None
        return float(rate)
    except (TypeError, ValueError):
        return None


def _hdr_from_stream(v):
    """从探测结果判定 HDR 类型，和 Emby 的 VideoRange 口径一致。"""
    for sd in v.get("side_data_list") or []:
        name = str(sd.get("side_data_type", "")).lower()
        if "dovi" in name or "dolby vision" in name:
            return "dolbyvision"
        if "hdr10+" in name or "hdr dynamic metadata" in name:
            return "hdr10plus"
    trc = str(v.get("color_transfer") or "").lower()
    if trc == "smpte2084":
        return "hdr10"
    if trc == "arib-std-b67":
        return "hlg"
    if trc in ("smpte428", "smpte431", "smpte432"):
        return "hdr10"
    return "sdr"


def _bitdepth_from_pixfmt(pix):
    pix = str(pix or "")
    if "12" in pix:
        return 12
    if "10" in pix:
        return 10
    if "9" in pix:
        return 9
    return 8


def parse_probe_json(data, source_url=""):
    """把 ffprobe 的 JSON 结果转成 nfo 字段字典。"""
    info = {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []

    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    if videos:
        v = videos[0]
        codec = FFPROBE_VIDEO_CODEC.get(str(v.get("codec_name") or "").lower())
        if codec:
            info["video_codec"] = codec
        if v.get("width"):
            info["width"] = int(v["width"])
        if v.get("height"):
            info["height"] = int(v["height"])
        br = v.get("bit_rate") or fmt.get("bit_rate")
        try:
            if br:
                info["video_bitrate"] = int(br)
        except (TypeError, ValueError):
            pass
        fps = _fps(v.get("r_frame_rate")) or _fps(v.get("avg_frame_rate"))
        if fps:
            info["framerate"] = fps
        info["hdr"] = _hdr_from_stream(v)
        info["bitdepth"] = _bitdepth_from_pixfmt(v.get("pix_fmt"))
        if v.get("pix_fmt"):
            info["pixelformat"] = v["pix_fmt"]
        if v.get("profile"):
            info["profile"] = v["profile"]
        for key, out in (("color_primaries", "colourprimaries"),
                         ("color_transfer", "colourtransfer")):
            if v.get(key):
                info[out] = v[key]
        # 色彩空间（矩阵系数），如 bt2020nc / bt709 —— 播放器“色彩空间”栏
        if v.get("color_space"):
            info["colourspace"] = v["color_space"]
        try:
            if v.get("height") and v.get("width"):
                info["aspect"] = round(float(v["width"]) / float(v["height"]), 3)
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if fmt.get("duration"):
        try:
            info["duration"] = float(fmt["duration"])
        except (TypeError, ValueError):
            pass
    if fmt.get("bit_rate"):
        try:
            info["total_bitrate"] = int(fmt["bit_rate"])
        except (TypeError, ValueError):
            pass
    if fmt.get("size"):
        try:
            info["size"] = int(fmt["size"])
        except (TypeError, ValueError):
            pass

    if audios:
        tracks = []
        for a in audios:
            codec = FFPROBE_AUDIO_CODEC.get(str(a.get("codec_name") or "").lower())
            tags = a.get("tags") or {}
            lang_raw = str(tags.get("language") or "").lower()
            lang = LANG_MAP.get(lang_raw, lang_raw or None)
            track = {
                "codec": codec,
                "channels": a.get("channels"),
                "sample_rate": a.get("sample_rate"),
                "profile": a.get("profile"),
                "atmos": bool(re.search(r"atmos", str(tags.get("title") or ""), re.I)),
                "dtsx": bool(re.search(r"dts[\s._-]*x", str(tags.get("title") or ""), re.I)),
            }
            # 音轨标题（如“英语”“国语”）—— 播放器“标题”栏
            if tags.get("title"):
                track["title"] = str(tags["title"])
            # 声道布局（如 7.1 / 5.1）—— 播放器“声道布局”栏；
            # ffprobe 的 stereo/mono 及 5.1(side) 写法归一化成 Emby 口径
            layout = _normalize_layout(a.get("channel_layout"))
            if layout:
                track["channel_layout"] = layout
            # 默认轨（mkv 里标了 default 的那条）—— 播放器“(默认)”标记
            disp_a = a.get("disposition") or {}
            if disp_a.get("default"):
                track["default"] = True
            try:
                if a.get("bit_rate"):
                    track["bitrate"] = int(a["bit_rate"])
            except (TypeError, ValueError):
                pass
            if lang:
                track["language"] = lang
            if codec:
                tracks.append(track)
            elif track.get("channels"):
                tracks.append(track)
        if tracks:
            info["audio_tracks"] = tracks

    if subs:
        sub_list = []
        for s in subs:
            codec = FFPROBE_SUB_CODEC.get(str(s.get("codec_name") or "").lower(),
                                          str(s.get("codec_name") or ""))
            tags = s.get("tags") or {}
            lang_raw = str(tags.get("language") or "").lower()
            lang = LANG_MAP.get(lang_raw, lang_raw or None)
            disp = s.get("disposition") or {}
            # (编码, 语言, forced, 标题, 默认) —— 位置固定，build_nfo 按下标取
            entry = [codec, lang,
                     True if disp.get("forced") else None,
                     str(tags["title"]) if tags.get("title") else None,
                     True if disp.get("default") else None]
            sub_list.append(tuple(entry))
        if sub_list:
            info["subtitles"] = sub_list

    return info


def _normalize_layout(layout):
    """ffprobe 的 channel_layout 归一化成 Emby 口径：stereo→2.0，
    mono→1.0，5.1(side)→5.1，其余原样。"""
    if not layout:
        return None
    low = str(layout).strip().lower()
    if low == "stereo":
        return "2.0"
    if low == "mono":
        return "1.0"
    return str(layout).split("(")[0].strip() or None


def probe_stream_info(url, timeout=PROBE_TIMEOUT, max_bytes=PROBE_BYTES):
    """探测 URL 的真实媒体信息；失败返回 None（调用方回退文件名模式）。"""
    ff = _ffprobe_bin()
    if not ff or not url or not re.match(r"^(http|https|ftp)s?://", url, re.I):
        return None
    tmp_path = None
    try:
        tmp_path, served, truncated, total_size = _fetch_head(url, max_bytes, timeout)
        if served < 65536:
            return None
        proc = subprocess.run(
            [ff, "-hide_banner", "-loglevel", "error", "-show_format",
             "-show_streams", "-of", "json", tmp_path],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        data = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
        info = parse_probe_json(data, source_url=url)
        # 真实大小优先用响应头的总量：头部被截断时 ffprobe 只能看到
        # 900KB 的临时文件，报的 size 是错的；响应头没给总量时才丢掉 size
        if total_size is not None:
            info["size"] = total_size
        elif truncated:
            info.pop("size", None)
        return info or None
    except Exception:
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------- 生成 nfo ----------------


def build_nfo(info: dict, episode: bool, extended: bool = False) -> str:
    """按 Kodi streamdetails 格式生成 .strm.nfo 内容。

    extended=True（探测模式产出）时额外写扩展字段：
      video: bitdepth / pixelformat / profile / colourprimaries / colourtransfer
             / colourspace
      audio: bitrate / samplerate / title / channellayout / default
      subtitle: title / default
      fileinfo: totalbitrate / size
    其中 audio 的 title / channellayout / default 与 subtitle 的 title /
    default 只有探测能拿到，文件名模式没有——写出时不设 extended 门槛，
    因为文件名解析根本不会产出这些键。
    这些字段 Kodi/Emby 原生不解析，属于给扩展版 STRM Probe NFO 的附加信息，
    未扩展时不会影响 Emby 读取标准字段。
    """
    root_tag = "episodedetails" if episode else "movie"
    root = ET.Element(root_tag)
    fileinfo = ET.SubElement(root, "fileinfo")
    sd = ET.SubElement(fileinfo, "streamdetails")

    video = ET.SubElement(sd, "video")
    if info.get("video_codec"):
        ET.SubElement(video, "codec").text = info["video_codec"]
    if info.get("width"):
        ET.SubElement(video, "width").text = str(info["width"])
    if info.get("height"):
        ET.SubElement(video, "height").text = str(info["height"])
    if info.get("aspect"):
        ET.SubElement(video, "aspect").text = str(info["aspect"])
    if info.get("video_bitrate"):
        ET.SubElement(video, "bitrate").text = str(info["video_bitrate"])
    if info.get("framerate"):
        ET.SubElement(video, "framerate").text = str(info["framerate"])
    if info.get("duration"):
        ET.SubElement(video, "durationinseconds").text = str(info["duration"])
    if info.get("hdr") and info["hdr"] != "sdr":
        # SDR 不写 hdrtype，插件默认就是 SDR
        ET.SubElement(video, "hdrtype").text = info["hdr"]
    if extended:
        if info.get("bitdepth"):
            ET.SubElement(video, "bitdepth").text = str(info["bitdepth"])
        if info.get("pixelformat"):
            ET.SubElement(video, "pixelformat").text = info["pixelformat"]
        if info.get("profile"):
            ET.SubElement(video, "profile").text = str(info["profile"])
        if info.get("colourprimaries"):
            ET.SubElement(video, "colourprimaries").text = info["colourprimaries"]
        if info.get("colourtransfer"):
            ET.SubElement(video, "colourtransfer").text = info["colourtransfer"]
        if info.get("colourspace"):
            ET.SubElement(video, "colourspace").text = info["colourspace"]

    for track in info.get("audio_tracks", []):
        audio = ET.SubElement(sd, "audio")
        if track.get("codec"):
            ET.SubElement(audio, "codec").text = track["codec"]
        if track.get("channels"):
            ET.SubElement(audio, "channels").text = str(track["channels"])
        if track.get("bitrate"):
            ET.SubElement(audio, "bitrate").text = str(track["bitrate"])
        lang = track.get("language") or info.get("audio_lang")
        if lang:
            ET.SubElement(audio, "language").text = lang
        # 标题 / 声道布局 / 默认轨：只有探测能拿到，文件名模式没有
        if track.get("title"):
            ET.SubElement(audio, "title").text = str(track["title"])
        if track.get("channel_layout"):
            ET.SubElement(audio, "channellayout").text = str(track["channel_layout"])
        if track.get("default"):
            ET.SubElement(audio, "default").text = "true"
        if extended and track.get("sample_rate"):
            try:
                ET.SubElement(audio, "samplerate").text = str(int(track["sample_rate"]))
            except (TypeError, ValueError):
                pass
        note = []
        if track.get("atmos"):
            note.append("Dolby Atmos")
        if track.get("dtsx"):
            note.append("DTS:X")
        if note:
            audio.append(ET.Comment(" " + " + ".join(note) + " "))

    for entry in info.get("subtitles", []):
        codec, lang = entry[0], entry[1]
        forced = entry[2] if len(entry) > 2 else None
        title = entry[3] if len(entry) > 3 else None
        default = entry[4] if len(entry) > 4 else None
        sub = ET.SubElement(sd, "subtitle")
        ET.SubElement(sub, "codec").text = codec
        if lang:
            ET.SubElement(sub, "language").text = lang
        if forced:
            ET.SubElement(sub, "forced").text = "true"
        if title:
            ET.SubElement(sub, "title").text = str(title)
        if default:
            ET.SubElement(sub, "default").text = "true"

    if extended:
        if info.get("total_bitrate"):
            ET.SubElement(fileinfo, "totalbitrate").text = str(info["total_bitrate"])
        if info.get("size"):
            ET.SubElement(fileinfo, "size").text = str(info["size"])

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


# ---------------- 供插件调用的入口 ----------------

# 探测模式才写的扩展字段标记：nfo 里出现任一标记，即视为已探测过。
# 用于“每文件夹只探测一个”下避免重复探测：强制覆盖扫描时，已探测过
# 的文件直接跳过（不重探、不覆盖），下一轮再探还没探过的文件夹。
PROBE_MARKER_TAGS = (
    "bitdepth", "colourspace", "colourprimaries", "colourtransfer",
    "pixelformat", "channellayout", "samplerate",
)


def nfo_has_probe_fields(nfo_path) -> bool:
    """nfo 里是否已有探测模式才写的扩展字段。读失败返回 False。"""
    try:
        text = Path(nfo_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(f"<{tag}>" in text for tag in PROBE_MARKER_TAGS)


def generate_nfo_for_strm(strm_path, force: bool = False, probe: bool = False,
                          probe_timeout: int = PROBE_TIMEOUT):
    """给一个 .strm 文件生成同名的 .strm.nfo。

    :param strm_path: .strm 文件路径（可以是 MoviePilot 生成的软链接）。
    :param force: True 时覆盖已有的 .strm.nfo。
    :param probe: True 时先探测真实媒体信息（读 .strm 里的 URL，只拉头部），
                  失败自动回退纯文件名解析。
    :return: (status, info)，status 取值：
        "generated" —— 已生成（info 里 source=probe/filename 标明来源）；
        "skipped" —— 已有 nfo 且未开启覆盖；
        "unparsed" —— 文件名里没有分辨率/编码/HDR 标签，无法生成；
        "error" —— 写入失败，info 为错误信息。
    """
    strm = Path(strm_path)
    nfo = strm.with_suffix(".strm.nfo")
    if nfo.exists() and not force:
        return "skipped", {}

    # 1) 文件名解析（作为基底/兜底）
    info = parse_filename(strm.stem)
    if strm.is_symlink() and not any(
        k in info for k in ("width", "video_codec", "hdr")
    ):
        try:
            target = strm.resolve()
            if target != strm:
                info = parse_filename(target.stem)
        except OSError:
            pass

    # 2) 探测模式：真实值覆盖标称值
    extended = False
    if probe:
        url = _read_strm_url(strm)
        probed = probe_stream_info(url, timeout=probe_timeout) if url else None
        if probed:
            merged = dict(info)
            merged.update(probed)
            # 提示性字段优先用探测值，但文件名给出的语言标记可补探测缺失
            if not merged.get("audio_tracks") and info.get("audio_tracks"):
                merged["audio_tracks"] = info["audio_tracks"]
            info = merged
            extended = True
            info["source"] = "probe"
        else:
            info["source"] = "filename"

    # 3) 至少解析出分辨率/编码/HDR 之一才生成
    if not any(k in info for k in ("width", "video_codec", "hdr")):
        return "unparsed", info

    try:
        content = build_nfo(info, is_episode(strm.stem), extended=extended)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=str(nfo.parent),
                prefix=nfo.name + ".", suffix=".tmp", delete=False,
            ) as tmp:
                tmp_path = tmp.name
                tmp.write(content)
            os.replace(tmp_path, nfo)
            tmp_path = None
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except OSError as err:
        return "error", {"error": str(err)}
    return "generated", info
