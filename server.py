#!/usr/bin/env python3
"""抖音视频提取 API（v3 — OmniParser 引擎，元数据 + 无水印直链 + 下载代理）

引擎链路（依次回退）:
  1. OmniParser (d.hel.cc.cd)
     GET  /api/session/ticket → 一次性票据（120s 有效）
     POST /api/parse  {url, cookie:null}
          头: X-Device-ID / X-Client-Type: web / X-Session-Ticket
          → 标题/作者/封面/多档无水印直链 + 自带 /api/download 代理
     实测：机房 IP 可用，无频控（同设备连续 5 次正常）
  2. dyxhsdownloader.com /api/download?source=<链接>&kind=video
     无元数据，纯视频流兜底

端点:
  GET /api/parse?url=<分享链接或整段文案>   → 元数据 + 直链 + 代理下载地址
  GET /api/video?url=<链接>&name=x.mp4      → 直接流式返回视频文件（一步到位）
  GET /api/download?u=<直链>&name=x.mp4     → 通用直链代理（Range 续传）
  GET /health

运行: python3 server.py --port 8765
"""

import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import kuaishou

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

BROWSER_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "sec-ch-ua": '"Chromium";v="130", "Not?A_Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

OMNI = "https://d.hel.cc.cd"

DYXHS = "https://dyxhsdownloader.com"
# 2026-10-17: OmniParser 开始对本机返回“投毒”结果（随机无关视频），随后 WAF 拉黑 IP。
# 默认关闭，只走 dyxhsdownloader（视频+图集都可靠，但无标题等元数据）。设 OMNI_ENABLED=1 可重新启用。
OMNI_ENABLED = os.environ.get("OMNI_ENABLED", "0") == "1"
NOTE_MAX_IMAGES = int(os.environ.get("NOTE_MAX_IMAGES", "35"))

# 上游用 X-Device-ID 识别自动化：带 "api-" 之类前缀会立刻 403。
# 所以设备指纹必须伪装成浏览器生成的 32 位 hex，且被封后能换新。
_DEVICE_REALM = os.environ.get("OMNI_DEVICE_REALM", "web")
_dev = {
    "id": None,
    "blocked_until": 0,   # 被封禁到期时间（解封后轮换）
    "quota_until": 0,     # 每日配额用尽后的熔断到期时间
    "last_call": 0,       # 上次请求时间（用于节流）
    "lock": threading.Lock(),
}
OMNI_MIN_INTERVAL = float(os.environ.get("OMNI_MIN_INTERVAL", "2.5"))  # 秒/次，防触发风控


def new_device_id():
    """生成看起来像浏览器随机指纹的设备 ID（32 位 hex，无前缀）"""
    return secrets.token_hex(16)


def omni_device():
    """取当前可用设备指纹；被封禁时自动轮换"""
    now = time.time()
    with _dev["lock"]:
        if _dev["id"] is None:
            _dev["id"] = new_device_id()
        if _dev["blocked_until"] and now >= _dev["blocked_until"]:
            # 封禁过期 → 换新指纹
            _dev["id"] = new_device_id()
            _dev["blocked_until"] = 0
        return _dev["id"]


def omni_mark_blocked(seconds=60):
    """标记当前指纹被封，冷却后自动轮换"""
    with _dev["lock"]:
        _dev["blocked_until"] = time.time() + seconds
        _dev["id"] = None


def omni_mark_quota_exhausted():
    """上游免费配额按 IP 每日 30 次。用尽后换指纹无效，直接熔断到次日 0 点"""
    now = time.time()
    lt = time.localtime(now)
    tomorrow = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) + 86400
    with _dev["lock"]:
        _dev["quota_until"] = tomorrow + 60          # 留 1 分钟余量
        _dev["blocked_until"] = 0
        _dev["id"] = None
        until = _dev["quota_until"]
    _save_state(until)   # 持久化，重启后不丢
    sys.stderr.write(f"[omni] 配额熔断至 {time.strftime('%m-%d %H:%M', time.localtime(tomorrow))}\n")


# 状态文件：仅保存配额熔断到期时间（跨重启保留，避免反复无效试探上游）
STATE_FILE = os.environ.get("STATE_FILE", "/root/douyin-api/state.json")


def _save_state(quota_until):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"quota_until": quota_until}, f)
    except Exception:
        pass


def _load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        until = float(data.get("quota_until") or 0)
        if until > time.time():
            _dev["quota_until"] = until
            sys.stderr.write(
                f"[omni] 载入状态：配额熔断中，至 {time.strftime('%m-%d %H:%M', time.localtime(until))}\n"
            )
    except Exception:
        pass


def omni_quota_blocked():
    with _dev["lock"]:
        return _dev["quota_until"] > time.time()


def omni_quota_reset_at():
    with _dev["lock"]:
        return _dev["quota_until"]


def omni_throttle():
    """全局节流：上游对高频调用敏感，串行化并保持最小间隔"""
    with _dev["lock"]:
        wait = OMNI_MIN_INTERVAL - (time.time() - _dev["last_call"])
        if wait > 0:
            time.sleep(wait)
        _dev["last_call"] = time.time()

URL_PATTERNS = [
    re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_\-]+/?"),
    re.compile(r"https?://(?:www\.)?douyin\.com/video/\d+[^\s\"']*"),
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/video/\d+[^\s\"']*"),
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/note/\d+[^\s\"']*"),
    re.compile(r"https?://(?:www\.)?douyin\.com/note/\d+[^\s\"']*"),
]

# 票据缓存
_ticket = {"value": None, "expire_at": 0}


def extract_url(text: str):
    for pat in URL_PATTERNS:
        m = pat.search(text or "")
        if m:
            return m.group(0).rstrip(".,，。!！?？)）")
    return None


def http_json(url, method="GET", body=None, headers=None, timeout=30):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", errors="replace"))
        except Exception:
            return e.code, {}
    except Exception as exc:
        return 0, {"_conn_error": str(exc)}


# ---------------------------------------------------------------- OmniParser


def omni_ticket():
    now = time.time()
    if _ticket["value"] and _ticket["expire_at"] > now + 15:
        return _ticket["value"]
    status, data = http_json(f"{OMNI}/api/session/ticket", headers=dict(BROWSER_HEADERS), timeout=15)
    if status == 200 and data.get("success") and data.get("ticket"):
        _ticket["value"] = data["ticket"]
        _ticket["expire_at"] = now + int(data.get("expires_in") or 120)
        return _ticket["value"]
    return None


# 解析结果缓存：同一链接短时间内重复请求不再打上游（上游对高频敏感）
_parse_cache = {}
PARSE_TTL = float(os.environ.get("PARSE_TTL", "600"))
_direct_cache = {}   # youtube 之类直链缓存：src_url -> {target, referer, expire_at}
DIRECT_TTL = float(os.environ.get("DIRECT_TTL", "1500"))


def cache_get(url):
    hit = _parse_cache.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    return None


def cache_put(url, result):
    _parse_cache[url] = (time.time() + PARSE_TTL, result)
    if len(_parse_cache) > 500:
        for k in sorted(_parse_cache, key=lambda k: _parse_cache[k][0])[:100]:
            _parse_cache.pop(k, None)


def parse_via_omni(url: str):
    """OmniParser 解析。返回统一 dict 或 None"""
    cached = cache_get(url)
    if cached:
        return cached

    # 配额熔断期间不浪费请求（反正上游会拒），直接走兜底
    if omni_quota_blocked():
        return None

    for attempt in (1, 2):  # 票据 401 时自动重取重试一次
        omni_throttle()       # 全局节流，避免触发上游风控
        ticket = omni_ticket()
        if not ticket:
            return None
        headers = {
            "X-Device-ID": omni_device(),
            "X-Client-Type": "web",
            "X-Session-Ticket": ticket,
            "Origin": OMNI,
            "Referer": f"{OMNI}/",
        }
        headers.update(BROWSER_HEADERS)
        status, data = http_json(
            f"{OMNI}/api/parse",
            method="POST",
            body={"url": url, "cookie": None},
            headers=headers,
            timeout=45,
        )
        if status == 401 and isinstance(data, dict) and data.get("code") == "INVALID_SESSION_TICKET":
            _ticket["value"] = None
            continue
        # 每日配额用尽：换指纹无用（按 IP 计），必须熔断到次日
        if isinstance(data, dict) and data.get("quota_exceeded"):
            usage = data.get("current_usage")
            limit = data.get("daily_limit")
            sys.stderr.write(f"[omni] 每日配额用尽 {usage}/{limit} → 熔断至次日\n")
            omni_mark_quota_exhausted()
            return None
        # 自动化判定/临时风控：轮换设备指纹并短暂冷却
        if status in (403, 429):
            code = (data or {}).get("code") or ""
            sys.stderr.write(f"[omni] {status} {code} → 轮换设备指纹，冷却 60s\n")
            omni_mark_blocked(60)
            return None
        if status == 200 and data.get("success") and data.get("data"):
            result = _normalize_omni(data["data"], url)
            cache_put(url, result)
            return result
        return None
    return None


def _normalize_omni(d, source_url):
    formats = []
    for f in d.get("formats") or []:
        if not f.get("is_video"):
            continue
        formats.append(
            {
                "label": f.get("quality_label") or f.get("type_label") or "mp4",
                "direct_url": f.get("url"),
                "proxy_path": f.get("proxy_url"),  # 相对路径，上游自带下载代理
                "ext": f.get("ext") or "mp4",
            }
        )
    images = d.get("images") or []
    return {
        "engine": "omniparser",
        "platform": d.get("platform") or "",
        "title": (d.get("title") or "").strip(),
        "author": d.get("author") or "",
        "cover": d.get("cover"),
        "duration_seconds": d.get("duration_seconds"),
        "media_type": d.get("media_type") or ("image" if images else "video"),
        "formats": formats,
        "images": images if isinstance(images, list) else [],
        "source_url": source_url,
    }


# ---------------------------------------------------------------- dyxhs 兜底


def parse_via_dyxhs(url: str):
    """dyxhsdownloader 兜底：探测视频流，无元数据"""
    from urllib.request import Request, urlopen

    api = f"{DYXHS}/api/download?{urllib.parse.urlencode({'source': url, 'kind': 'video'})}"
    try:
        req = Request(api, headers={"User-Agent": UA, "Referer": f"{DYXHS}/"})
        with urlopen(req, timeout=45) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "video" not in ct:
                return None
            resp.read(512)  # 探测
            length = resp.headers.get("Content-Length")
            return {
                "engine": "dyxhsdownloader",
                "title": "",
                "author": "",
                "cover": None,
                "media_type": "video",
                "content_length": int(length) if length and length.isdigit() else None,
                "source_url": url,
            }
    except Exception:
        return None


_resolve_cache = {}


def resolve_douyin(url: str):
    """短链 → (kind, aweme_id)。kind: 'note' | 'video' | ''"""
    hit = _resolve_cache.get(url)
    if hit:
        return hit
    final = url
    if re.match(r"https?://v\.douyin\.com/", url):
        class _NoRedir(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        opener = urllib.request.build_opener(_NoRedir)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)"})
        try:
            opener.open(req, timeout=15)
        except urllib.error.HTTPError as e:
            final = e.headers.get("Location") or url
        except Exception:
            pass
    m = re.search(r"/(note|video|slides)/(\d+)", final) or re.search(r"modal_id=(\d+)", final)
    if not m:
        res = ("", "")
    elif m.re.pattern.startswith("modal"):
        res = ("video", m.group(1))
    else:
        res = ("note" if m.group(1) in ("note", "slides") else "video", m.group(2))
    _resolve_cache[url] = res
    return res


def dyxhs_image_url(url, index):
    return f"{DYXHS}/api/download?{urllib.parse.urlencode({'source': url, 'kind': 'image', 'index': index})}"


def parse_note_dyxhs(url: str):
    """图集：逐个探测 index=0..N，直到 404"""
    cached = cache_get("note:" + url)
    if cached:
        return cached
    from urllib.request import Request, urlopen
    images = []
    for i in range(NOTE_MAX_IMAGES):
        req = Request(dyxhs_image_url(url, i), headers={"User-Agent": UA, "Referer": f"{DYXHS}/", "Range": "bytes=0-0"})
        try:
            with urlopen(req, timeout=45) as resp:
                ct = resp.headers.get("Content-Type", "")
                if "image" not in ct:
                    break
                cr = resp.headers.get("Content-Range", "")
                size = int(cr.rsplit("/", 1)[-1]) if "/" in cr and cr.rsplit("/", 1)[-1].isdigit() else None
                ext = "png" if "png" in ct else ("webp" if "webp" in ct else "jpg")
                images.append({"index": i, "content_type": ct, "ext": ext, "size": size})
        except Exception:
            break
    if not images:
        return None
    result = {"engine": "dyxhsdownloader", "media_type": "image", "title": "", "author": "",
              "cover": None, "images_meta": images, "source_url": url}
    cache_put("note:" + url, result)
    return result


# ---------------------------------------------------------------- HTTP 服务


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _base_url(self):
        """依据请求 Host 拼出绝对地址（便于在文档/前端直接使用）"""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"127.0.0.1:{self.server.server_port}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    def _json(self, code, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        """支持 POST 提交长分享文案（curl --data-urlencode 默认走 POST）"""
        parsed = urllib.parse.urlparse(self._raw_path())
        if parsed.path not in ("/api/parse", "/api/video"):
            self._json(404, {"ok": False, "error": "未知端点。POST 仅支持 /api/parse 与 /api/video"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        fields = {}
        text = body.decode("utf-8", errors="replace")
        if "application/json" in ctype:
            try:
                obj = json.loads(text or "{}")
                if isinstance(obj, dict):
                    fields = {k: str(v) for k, v in obj.items() if v is not None}
            except Exception:
                fields = {}
        else:
            fields = {k: v[0] for k, v in urllib.parse.parse_qs(text).items()}
        # URL 查询串参数优先
        for k, v in urllib.parse.parse_qs(parsed.query).items():
            fields[k] = v[0]
        self._handle_parse(fields, is_video=(parsed.path == "/api/video"))

    def _raw_path(self):
        """self.path 被 http.server 按 latin-1 解码，这里还原 UTF-8 原始串，
        否则中文参数（如 name=我的视频.mp4）会变成乱码并被二次编码"""
        p = self.path
        try:
            return p.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return p

    def do_GET(self):
        parsed = urllib.parse.urlparse(self._raw_path())
        flat = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}

        if parsed.path == "/health":
            self._json(200, {
                "ok": True,
                "engine": "omniparser+dyxhs" if OMNI_ENABLED else "dyxhs",
                "device": omni_device(),
                "quota_exhausted": omni_quota_blocked(),
                "quota_reset_at": int(omni_quota_reset_at()) or None,
                "ts": int(time.time()),
            })
            return

        if parsed.path in ("/api/parse", "/api/video"):
            self._handle_parse(flat, is_video=(parsed.path == "/api/video"))
            return

        if parsed.path == "/api/media":
            via = flat.get("via", "omni")
            url = flat.get("url", "")
            name = flat.get("name", "video.mp4")
            try:
                fmt_idx = int(flat.get("fmt") or 0)
            except ValueError:
                fmt_idx = 0
            if not url.startswith("http"):
                self._json(400, {"ok": False, "error": "缺少 url 参数"})
                return
            if via == "dyxhs_img":
                try:
                    idx = int(flat.get("index") or 0)
                except ValueError:
                    idx = 0
                self._proxy_range(dyxhs_image_url(url, idx), name or f"img_{idx}.jpg", referer=f"{DYXHS}/")
            elif via == "omni" and OMNI_ENABLED:
                self._stream_omni(url, fmt_idx, name)
            else:
                self._stream_dyxhs(url, name)
            return

        if parsed.path == "/api/download":
            target = flat.get("u", "")
            name = flat.get("name", "video.mp4")
            if not target.startswith("http"):
                self._json(400, {"ok": False, "error": "缺少 u 参数"})
                return
            self._proxy_range(target, name)
            return

        self._json(404, {"ok": False, "error": "未知端点。可用: /api/parse?url=  /api/video?url=  /api/download?u="})

    def _handle_parse(self, params, is_video=False):
        """解析公共逻辑：GET 与 POST 共用"""
        raw = params.get("url") or params.get("q") or ""
        if kuaishou.is_kuaishou(raw) and not extract_url(raw):
            self._handle_kuaishou(kuaishou.extract(raw), params, is_video)
            return
        url = extract_url(raw)
        if not url:
            self._json(400, {"ok": False, "error": "未能从输入中提取抖音或快手链接，请粘贴分享链接或整段分享文案"})
            return

        name = params.get("name") or ""
        kind, aweme_id = resolve_douyin(url)
        base = self._base_url()
        if kind == "note":
            note = parse_note_dyxhs(url)
            if not note:
                self._json(502, {"ok": False, "error": "图集解析失败", "source": url, "aweme_id": aweme_id})
                return
            out = {k: v for k, v in note.items() if k != "images_meta"}
            out["aweme_id"] = aweme_id
            out["formats"] = []
            out["images"] = []
            for im in note["images_meta"]:
                fn = f"dy_{aweme_id}_{im['index'] + 1}.{im['ext']}"
                out["images"].append({**im, "filename": fn,
                    "download_url": base + f"/api/media?{urllib.parse.urlencode({'via': 'dyxhs_img', 'url': url, 'index': im['index'], 'name': fn})}"})
            out["cover"] = out["images"][0]["download_url"]
            out["download_url"] = out["cover"]
            out["direct_url"] = None
            out["filename"] = out["images"][0]["filename"]
            self._json(200, out)  # /api/video 对图集也返回 JSON（没有单个视频可 302）
            return

        result = parse_via_omni(url) if OMNI_ENABLED else None
        first = None
        if result:
            first = next((f for f in result["formats"] if f.get("direct_url") or f.get("proxy_path")), None)
            if not first and result["media_type"] == "video":
                result = None  # 有元数据但无可用视频格式 → 兜底

        if result and first:
            safe_title = re.sub(r"[\\/:*?\"<>|\s]+", "_", result["title"])[:60] or f"dy_{int(time.time())}"
            fname = name or f"{safe_title}.{first.get('ext','mp4')}"
            result["filename"] = fname
            result["download_url"] = base + f"/api/media?{urllib.parse.urlencode({'via': 'omni', 'url': url, 'fmt': result['formats'].index(first), 'name': fname})}"
            # 原始直链（有时效，约几十分钟）
            result["direct_url"] = first.get("direct_url")
        else:
            fb = parse_via_dyxhs(url)
            if not fb:
                self._json(502, {"ok": False, "error": "所有解析引擎均失败，请确认链接有效后重试", "source": url})
                return
            fb["aweme_id"] = aweme_id
            fb["filename"] = name or (f"dy_{aweme_id}.mp4" if aweme_id else f"dy_{int(time.time())}.mp4")
            fb["download_url"] = base + f"/api/media?{urllib.parse.urlencode({'via': 'dyxhs', 'url': url, 'name': fb['filename']})}"
            fb["direct_url"] = None
            result = fb

        if not is_video:
            self._json(200, self._sanitize(result))
        else:
            # /api/video → 302 到媒体代理
            self.send_response(302)
            self.send_header("Location", result["download_url"])
            self.send_header("Content-Length", "0")
            self.end_headers()
    def _handle_kuaishou(self, url, params, is_video):
        try:
            r = kuaishou.parse(url)
        except Exception as exc:
            self._json(502, {"ok": False, "platform": "kuaishou", "error": f"快手解析失败: {exc}", "source": url})
            return
        base = self._base_url()
        name = params.get("name") or ""
        safe = re.sub(r"[\\/:*?\"<>|\s#@]+", "_", r["title"])[:40].strip("_")
        stem = f"ks_{r['photo_id']}" + (f"_{safe}" if safe else "")
        out = {k: v for k, v in r.items() if k != "videos"}
        out["formats"] = []
        if r["media_type"] == "video":
            fname = name or f"{stem}.mp4"
            out["filename"] = fname
            out["direct_url"] = r["videos"][0]
            out["download_url"] = base + "/api/download?" + urllib.parse.urlencode({"u": r["videos"][0], "name": fname})
            out["formats"] = [{"ext": "mp4", "width": r.get("width"), "height": r.get("height"), "direct_url": u} for u in r["videos"][:1]]
        else:
            imgs = []
            for i, u in enumerate(r["images"]):
                ext = "webp" if ".webp" in u else ("png" if ".png" in u else "jpg")
                fn = f"{stem}_{i + 1}.{ext}"
                imgs.append({"index": i, "filename": fn, "direct_url": u,
                             "download_url": base + "/api/download?" + urllib.parse.urlencode({"u": u, "name": fn})})
            out["images"] = imgs
            out["filename"] = imgs[0]["filename"]
            out["download_url"] = imgs[0]["download_url"]
            out["direct_url"] = None
            is_video = False  # 图集没有单个视频可 302，返回 JSON
        out["ok"] = True
        if is_video:
            self.send_response(302)
            self.send_header("Location", out["download_url"])
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(200, out)

    # ---- OmniParser 流式 ----

    def _sanitize(self, result):
        """对外输出前清理：隐去上游内部字段，避免用户绕过本服务直连上游 IP"""
        out = dict(result)
        out["formats"] = [
            {k: v for k, v in f.items() if k != "proxy_path"} for f in result.get("formats") or []
        ]
        return out

    def _stream_omni(self, url, fmt_idx, name):
        # 直链缓存命中 → 完全不打上游（抖音 CDN 直链裸连可用，实测无需 Referer）
        ck = f"{url}#{fmt_idx}"
        hit = _direct_cache.get(ck)
        if hit and hit["expire_at"] > time.time():
            self._proxy_range(hit["target"], name, referer=hit["referer"])
            return
        data = parse_via_omni(url)
        videos = [f for f in (data or {}).get("formats", []) if f.get("direct_url") or f.get("proxy_path")]
        if not videos:
            # 主引擎不可用（配额熔断/风控）→ 自动回落兜底，避免 download_url 变成死链
            self._stream_dyxhs(url, name)
            return
        f = videos[min(fmt_idx, len(videos) - 1)]
        target = f.get("direct_url")
        referer = "https://www.douyin.com/"
        if not target and f.get("proxy_path"):
            # 上游代理是相对路径
            target = OMNI + f["proxy_path"]
            referer = f"{OMNI}/"
        if target:
            _direct_cache[ck] = {"target": target, "referer": referer, "expire_at": time.time() + DIRECT_TTL}
        self._proxy_range(target, name, referer=referer)

    def _stream_dyxhs(self, url, name):
        api = f"{DYXHS}/api/download?{urllib.parse.urlencode({'source': url, 'kind': 'video'})}"
        req = urllib.request.Request(api, headers={"User-Agent": UA, "Referer": f"{DYXHS}/"})
        try:
            upstream = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            self._json(502, {"ok": False, "error": f"上游 HTTP {e.code}"})
            return
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"上游连接失败: {exc}"})
            return
        with upstream:
            self.send_response(200)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "video/mp4"))
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            quoted = urllib.parse.quote(name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _proxy_range(self, target, name, referer=None):
        req = urllib.request.Request(
            target,
            headers={"User-Agent": UA, "Referer": referer or "https://www.douyin.com/", "Range": self.headers.get("Range", "bytes=0-")},
        )
        try:
            upstream = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as e:
            self._json(502, {"ok": False, "error": f"上游 HTTP {e.code}"})
            return
        except Exception as exc:
            self._json(502, {"ok": False, "error": f"上游连接失败: {exc}"})
            return
        with upstream:
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
            length = upstream.headers.get("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            cr = upstream.headers.get("Content-Range")
            if cr:
                self.send_header("Content-Range", cr)
            self.send_header("Accept-Ranges", "bytes")
            quoted = urllib.parse.quote(name)
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
            self.end_headers()
            try:
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass


def main():
    port = 8765
    host = "0.0.0.0"
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
        if a == "--host" and i + 1 < len(args):
            host = args[i + 1]

    _load_state()   # 恢复配额熔断状态
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"抖音提取 API v3 已启动: http://{host}:{port}")
    print(f"  解析:     GET /api/parse?url=<分享链接或文案>")
    print(f"  直接下载: GET /api/video?url=<链接>  (302 → 视频流)")
    print(f"  引擎: OmniParser（元数据+直链）→ dyxhsdownloader 兜底")
    print(f"  设备指纹: {omni_device()[:8]}… (被封自动轮换，节流 {OMNI_MIN_INTERVAL}s/次)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
