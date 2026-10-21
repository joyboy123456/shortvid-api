"""简单 API-Key 鉴权 + 下载链接签名 + 可选 QPS 限流。

鉴权方式参考 ucmao/media-parser（MIT License, https://github.com/ucmao/media-parser）
src/api/access.py 的 authenticate_api_key：从 `Authorization: Bearer <key>` 或 `?key=` 取 key，
缺失返回 401 API_KEY_REQUIRED、错误返回 401 INVALID_API_KEY。这里简化为：
  - 不用数据库，key 来自环境变量 API_KEYS（逗号分隔）或 .env 文件里的 API_KEY / API_KEYS；
  - 额外支持 `X-API-Key: <key>` 头；
  - 未配置任何 key 时鉴权关闭（启动日志会提示）。

本服务返回的 download_url（/api/media、/api/download）会附带 exp + sig（HMAC-SHA256），
浏览器/播放器可直接打开而无需再带 key，且 key 本身不会出现在返回的链接里。
"""
import hashlib
import hmac
import os
import threading
import time
import urllib.parse

ENV_FILE = os.environ.get("API_ENV_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
SIGN_TTL = int(os.environ.get("SIGN_TTL", str(6 * 3600)))  # 下载链接签名有效期（秒）


def _read_env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


_file = _read_env_file(ENV_FILE)


def _cfg(name, default=""):
    return os.environ.get(name) or _file.get(name) or default


def _load_keys():
    raw = ",".join(x for x in (_cfg("API_KEYS"), _cfg("API_KEY")) if x)
    return [k.strip() for k in raw.split(",") if k.strip()]


KEYS = _load_keys()
ENABLED = bool(KEYS)
QPS_LIMIT = float(_cfg("QPS_LIMIT", "0") or 0)   # 每个 key 每秒请求数上限，0 = 不限
# 签名密钥：优先 SIGN_SECRET，否则由第一个 API key 派生（换 key 后旧链接自动失效）
_SIGN_SECRET = (_cfg("SIGN_SECRET") or (KEYS[0] if KEYS else "")).encode()


def extract_key(headers, params):
    k = (headers.get("X-API-Key") or "").strip()
    if not k:
        a = headers.get("Authorization") or ""
        if a.lower().startswith("bearer "):
            k = a[7:].strip()
    if not k:
        k = (params.get("key") or "").strip()
    return k


def check_key(key):
    """返回 (ok, error_code)"""
    if not ENABLED:
        return True, None
    if not key:
        return False, "API_KEY_REQUIRED"
    if any(hmac.compare_digest(key.encode(), k.encode()) for k in KEYS):
        return True, None
    return False, "INVALID_API_KEY"


def _sig_payload(path, params):
    items = sorted((k, str(v)) for k, v in params.items() if k not in ("sig", "key"))
    return (path + "?" + urllib.parse.urlencode(items)).encode()


def sign(path, params):
    """给内部下载链接签名，返回完整 query 串"""
    p = {k: v for k, v in params.items() if v is not None}
    if ENABLED:
        p["exp"] = int(time.time()) + SIGN_TTL
        p["sig"] = hmac.new(_SIGN_SECRET, _sig_payload(path, p), hashlib.sha256).hexdigest()[:32]
    return path + "?" + urllib.parse.urlencode(p)


def check_sig(path, params):
    sig = params.get("sig") or ""
    try:
        exp = int(params.get("exp") or 0)
    except ValueError:
        return False
    if not sig or exp < time.time():
        return False
    want = hmac.new(_SIGN_SECRET, _sig_payload(path, params), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, want)


_rl = {}
_rl_lock = threading.Lock()


def rate_ok(subject):
    """滑动 1 秒窗口限流；QPS_LIMIT=0 时总是放行"""
    if QPS_LIMIT <= 0:
        return True
    now = time.time()
    with _rl_lock:
        q = [t for t in _rl.get(subject, []) if now - t < 1.0]
        if len(q) >= QPS_LIMIT:
            _rl[subject] = q
            return False
        q.append(now)
        _rl[subject] = q
        return True
