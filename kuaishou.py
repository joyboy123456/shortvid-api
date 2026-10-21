"""快手解析：分享链接/文案 → photoId → 移动分享页 window.INIT_STATE → 无水印 mp4 或图集。
无需登录 / Cookie。直链来自 photo.mainMvUrls（kwimgs CDN，裸连可下，支持 Range）。

图集（atlas，photoType = VERTICAL_ATLAS / HORIZONTAL_ATLAS）解析思路移植自
ucmao/media-parser（MIT License, https://github.com/ucmao/media-parser）
src/parsers/kuaishou_parser.py：atlas 可能挂在 photo.ext_params.atlas 或与 photo 同级，
两处都收集，优先 .webp 版本；CDN 取 atlas.cdn（字符串或列表）或 atlas.cdnList[].cdn；
路径若已是完整 URL 则原样使用；配乐 = musicCdnList[0].cdn + music。"""
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
    re.compile(r"https?://(?:[a-z0-9\-]+\.)?kuaishou\.com/(?:f|short-video|fw/photo|fw/long-video|photo)/[A-Za-z0-9_\-]+[^\s\"'，。]*"),
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
                    return v   # 整个 payload：photo 及与之同级的 atlas
            last = "INIT_STATE 中无作品数据（可能已删除/私密/需登录）"
        except Exception as ex:
            last = str(ex)
    raise RuntimeError(last or "解析失败")


def _norm(u):
    if not u:
        return None
    u = str(u).replace("\\u002F", "/")
    return "https:" + u if u.startswith("//") else u


def _cdns(atlas):
    """atlas.cdn 可能是字符串或列表；退回 atlas.cdnList[].cdn"""
    c = atlas.get("cdn") or []
    if isinstance(c, str):
        c = [c]
    if not c:
        c = [i.get("cdn") for i in atlas.get("cdnList") or [] if isinstance(i, dict) and i.get("cdn")]
    return [x for x in c if isinstance(x, str) and x] or ["p2.a.yximgs.com"]


def _join(cdn, path):
    path = _norm(path)
    if not path:
        return None
    if path.startswith(("http://", "https://")):
        return path
    cdn = _norm(cdn).rstrip("/")
    if not cdn.startswith(("http://", "https://")):
        cdn = "https://" + cdn
    return cdn + "/" + path.lstrip("/")


def _atlas(payload):
    """返回 (images, music)。移植自 media-parser 的 _get_atlas_variants / get_image_list"""
    photo = payload.get("photo") or {}
    variants = [a for a in ((photo.get("ext_params") or {}).get("atlas"), payload.get("atlas"), photo.get("atlas"))
                if isinstance(a, dict) and a.get("list")]
    if not variants:
        return [], None
    atlas = next((a for a in variants if any(str(x).lower().endswith(".webp") for x in a["list"])), variants[0])
    cdn = _cdns(atlas)[0]
    images = [u for u in (_join(cdn, x) for x in atlas["list"]) if u]
    music = None
    src = next((a for a in variants if a.get("music")), None)
    if src:
        mc = [i.get("cdn") for i in src.get("musicCdnList") or [] if isinstance(i, dict) and i.get("cdn")]
        music = _join(mc[0] if mc else cdn, src["music"])
    return images, music


def parse(url):
    pid = resolve_id(url)
    if not pid:
        raise RuntimeError("未能从链接中解析出快手作品 ID")
    hit = _cache.get(pid)
    if hit and hit[0] > time.time():
        return hit[1]
    payload = _state(pid)
    p = payload["photo"]
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
        "music": None,
        "photo_type": p.get("photoType"),
    }
    images, music = _atlas(payload)
    if images:
        res["images"] = images
        res["music"] = music
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
