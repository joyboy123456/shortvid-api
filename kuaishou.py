"""快手解析：分享链接/文案 → photoId → 移动分享页 window.INIT_STATE → 无水印 mp4 或图集。
无需登录 / Cookie。直链来自 photo.mainMvUrls（kwimgs CDN，裸连可下，支持 Range）。"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

MUA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
       "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

KS_PATTERNS = [
    re.compile(r"https?://v\.kuaishou\.com/[A-Za-z0-9_\-]+/?"),
    re.compile(r"https?://(?:www\.)?kuaishou\.com/(?:f|short-video|fw/photo|photo)/[A-Za-z0-9_\-]+[^\s\"'，。]*"),
    re.compile(r"https?://(?:[a-z0-9\-]+\.)*(?:chenzhongtech|gifshow|kuaishouapp)\.com/[^\s\"'，。]+"),
]
PAGE_HOSTS = ["https://v.m.chenzhongtech.com/fw/photo/", "https://m.gifshow.com/fw/photo/"]
_cache = {}


def is_kuaishou(text):
    return any(p.search(text or "") for p in KS_PATTERNS)


def extract(text):
    for p in KS_PATTERNS:
        m = p.search(text or "")
        if m:
            return m.group(0).rstrip(".,，。!！?？)）")
    return None


def _id_from(url):
    m = re.search(r"/(?:short-video|photo|fw/photo|fw/long-video)/([A-Za-z0-9_\-]+)", url) \
        or re.search(r"[?&](?:photoId|shareObjectId)=([A-Za-z0-9_\-]+)", url)
    return m.group(1) if m else None


_rid = {}


def resolve_id(url):
    if url in _rid:
        return _rid[url]
    for _ in range(3):
        pid = _resolve_once(url)
        if pid:
            _rid[url] = pid
            return pid
        time.sleep(1)
    return None


def _resolve_once(url):
    pid = _id_from(url)
    if pid and "/f/" not in url:
        return pid

    class _NoRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedir)
    cur = url
    for _ in range(5):
        try:
            opener.open(urllib.request.Request(cur, headers={"User-Agent": MUA}), timeout=15)
            break
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if not loc:
                break
            cur = urllib.parse.urljoin(cur, loc)
            pid = _id_from(cur)
            if pid:
                return pid
        except Exception:
            break
    return _id_from(cur)


def _state(pid):
    last = None
    for host in PAGE_HOSTS:
        try:
            req = urllib.request.Request(host + pid, headers={"User-Agent": MUA})
            html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
            m = re.search(r"window\.INIT_STATE\s*=\s*(\{.*?\})\s*</script>", html, re.S)
            if not m:
                last = "页面无 INIT_STATE"
                continue
            d = json.loads(m.group(1))
            for v in d.values():
                if isinstance(v, dict) and isinstance(v.get("photo"), dict):
                    return v["photo"]
            last = "INIT_STATE 中无作品数据（可能已删除/私密/需登录）"
        except Exception as ex:
            last = str(ex)
    raise RuntimeError(last or "解析失败")


def parse(url):
    pid = resolve_id(url)
    if not pid:
        raise RuntimeError("未能从链接中解析出快手作品 ID")
    hit = _cache.get(pid)
    if hit and hit[0] > time.time():
        return hit[1]
    p = _state(pid)
    ext = p.get("ext_params") or {}
    cover = next((c.get("url") for c in p.get("coverUrls") or [] if c.get("url")), None)
    res = {
        "engine": "kuaishou-share",
        "platform": "kuaishou",
        "photo_id": pid,
        "title": (p.get("caption") or "").strip(),
        "author": p.get("userName") or "",
        "cover": cover,
        "duration_seconds": round((p.get("duration") or 0) / 1000, 2) or None,
        "width": ext.get("w") or p.get("width"),
        "height": ext.get("h") or p.get("height"),
        "source_url": url,
        "videos": [u["url"] for u in p.get("mainMvUrls") or [] if u.get("url")],
        "images": [],
    }
    atlas = ext.get("atlas") or p.get("atlas") or {}
    if atlas.get("list"):
        cdns = atlas.get("cdn") or atlas.get("cdnList") or []
        cdn = cdns[0] if cdns and isinstance(cdns[0], str) else (cdns[0].get("cdn") if cdns else "p1.a.yximgs.com")
        res["images"] = [("https://" + cdn + path) if path.startswith("/") else path for path in atlas["list"]]
        res["media_type"] = "image"
    elif res["videos"]:
        res["media_type"] = "video"
    elif p.get("singlePicture") and cover:
        res["images"] = [cover]
        res["media_type"] = "image"
    else:
        raise RuntimeError("作品没有可下载的视频或图片")
    _cache[pid] = (time.time() + 1200, res)  # 直链约数小时有效，缓存 20 分钟
    return res
