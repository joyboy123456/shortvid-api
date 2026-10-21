# 抖音 / 快手提取 API（shortvid-api）— 使用文档（v8，2026-10-21）

**给一个抖音链接（视频或图文），拿回无水印文件。**

| 地址 | 说明 |
|---|---|
| `https://your-domain.example` | **推荐**。Cloudflare 隧道 + TLS，隐藏源站 |
| `http://YOUR_SERVER_IP:8765` | 直连 IP，少一跳更快，适合大文件 |
| `http://127.0.0.1:8765` | VPS 本机 |

- 输入：抖音分享短链、PC 长链，或**整段分享文案**（自动挑出链接）
- 支持：**视频** 和 **图文（图集）**
- 当前引擎：`dyxhsdownloader`（`/health` 显示 `"engine": "dyxhs"`）
- 快手：视频 + **图集**（v7/v8，见文末）

> ### 🔑 v8 变更（2026-10-21）：需要 API Key
> 除 `/health` 外，所有接口都要带 key，三种方式任选其一：
>
> ```bash
> curl -H "X-API-Key: $KEY"              "https://your-domain.example/api/parse?url=…"
> curl -H "Authorization: Bearer $KEY"   "https://your-domain.example/api/parse?url=…"
> curl "https://your-domain.example/api/parse?url=…&key=$KEY"     # 或 POST body 里的 key 字段
> ```
>
> - 没带 key → `401 {"ok":false,"error_code":"API_KEY_REQUIRED"}`；key 错误 → `401 … "INVALID_API_KEY"`；超出 QPS（默认不限）→ `429 RATE_LIMITED`
> - 返回 JSON 里的 `download_url`（以及 `/api/video` 302 跳转地址）**自带 `exp`+`sig` 签名，6 小时内可直接打开，不用再带 key**，返回内容里也不会出现 key 本身。
> - key 由服务器管理员在 `/root/douyin-api/.env`（`API_KEY=...`，权限 600）配置，多个 key 用 `API_KEYS=k1,k2`。改完 `systemctl restart douyin-api` 生效；换 key 后旧的签名链接随之失效。
> - 浏览器跨域调用支持 CORS 预检（`OPTIONS`，允许 `X-API-Key` / `Authorization` 头）。
> - 鉴权写法参考 [ucmao/media-parser](https://github.com/ucmao/media-parser)（MIT）的 `authenticate_api_key`，见 `auth.py`。
>
> 下文示例为简洁省略了 key，实际调用请加上 `-H "X-API-Key: $KEY"`（`download_url` 除外）。

> ### v6 变更（2026-10-17）
> 1. **OmniParser 默认关闭**：上游开始对同一链接返回随机无关视频（"投毒"），随后 WAF 以 `blocked_no_ticket` 拉黑本机 IP。现全部走 dyxhsdownloader，结果正确。需要时设 `OMNI_ENABLED=1` 恢复。
> 2. **代价**：`title` / `author` / `cover` 为空，`formats` 为空数组，**无法选画质**；文件按 `dy_<aweme_id>` 命名。
> 3. **新增图文支持**：`/api/parse` 返回 `media_type: "image"` 和 `images[]`，每张图一个下载地址。
> 4. **不再有每日 30 次配额**（那是 OmniParser 的限制）。仍请保持 ≥2 秒/次。
> 5. 旧版代码备份：`/root/douyin-api/server.py.bak-20261017`

> ⚠️ **核对结果**：上游没有元数据可校验。拿到文件后建议对照 `aweme_id`（分享链接跳转后的作品 ID）或内容本身确认是你要的那条。

---

## 一、最快用法

### 视频：一行下载

```bash
curl -L "https://your-domain.example/api/video?url=https://v.douyin.com/1KyuRCvMRN0/" -o 视频.mp4
```

`/api/video` 会 302 跳到代理流。浏览器地址栏粘贴同一地址也能直接下载。

### 图文：先解析，再逐张下载

```bash
B=https://your-domain.example
curl -s "$B/api/parse?url=https://v.douyin.com/kCcouifI_dQ/" \
  | jq -r '.images[] | "\(.download_url) \(.filename)"' \
  | while read u f; do curl -sL "$u" -o "$f"; sleep 2; done
```

> 图文作品调用 `/api/video` **不会跳转**，而是直接返回和 `/api/parse` 一样的 JSON（没有单个视频可下）。

---

## 二、`/api/parse` 返回格式

### 视频（实测 2026-10-17）

```bash
curl "https://your-domain.example/api/parse?url=https://v.douyin.com/1KyuRCvMRN0/"
```

```json
{
  "engine": "dyxhsdownloader",
  "title": "",
  "author": "",
  "cover": null,
  "media_type": "video",
  "content_length": 1214095,
  "source_url": "https://v.douyin.com/1KyuRCvMRN0/",
  "aweme_id": "7081608169876753702",
  "filename": "dy_7081608169876753702.mp4",
  "download_url": "https://your-domain.example/api/media?via=dyxhs&url=https%3A%2F%2Fv.douyin.com%2F1KyuRCvMRN0%2F&name=dy_7081608169876753702.mp4",
  "direct_url": null,
  "formats": []
}
```

### 图文（实测 2026-10-17，3 张图）

```bash
curl "https://your-domain.example/api/parse?url=https://v.douyin.com/kCcouifI_dQ/"
```

```json
{
  "engine": "dyxhsdownloader",
  "media_type": "image",
  "title": "",
  "author": "",
  "aweme_id": "7697453814726593807",
  "source_url": "https://v.douyin.com/kCcouifI_dQ/",
  "formats": [],
  "images": [
    {
      "index": 0,
      "content_type": "image/jpeg",
      "ext": "jpg",
      "size": 350586,
      "filename": "dy_7697453814726593807_1.jpg",
      "download_url": "https://your-domain.example/api/media?via=dyxhs_img&url=https%3A%2F%2Fv.douyin.com%2FkCcouifI_dQ%2F&index=0&name=dy_7697453814726593807_1.jpg"
    },
    { "index": 1, "size": 275090, "filename": "dy_7697453814726593807_2.jpg", "download_url": "…index=1…" },
    { "index": 2, "size": 304629, "filename": "dy_7697453814726593807_3.jpg", "download_url": "…index=2…" }
  ],
  "cover": "（= images[0].download_url）",
  "download_url": "（= images[0].download_url）",
  "direct_url": null,
  "filename": "dy_7697453814726593807_1.jpg"
}
```

### 字段说明

| 字段 | 含义 |
|---|---|
| `media_type` | `video` 或 `image`（图文） |
| `aweme_id` | 抖音作品 ID，用于核对和命名 |
| `download_url` | 本服务的代理下载地址，**长期有效**（每次请求实时向上游取） |
| `content_length` | 视频字节数（视频才有） |
| `images[]` | 图文的每张图：`index`（从 0 开始）、`size`（字节）、`ext`、`filename`、`download_url` |
| `title` / `author` / `cover` | **当前恒为空**（dyxhs 不提供元数据）；图文的 `cover` 指向第一张图 |
| `formats` / `direct_url` | **当前恒为 `[]` / `null`**（仅 OmniParser 开启时有值） |

---

## 三、所有端点

| 端点 | 用途 | 说明 |
|---|---|---|
| `GET/POST /api/parse?url=<链接或文案>` | 解析 | 视频：返回 `download_url`；图文：返回 `images[]` |
| `GET/POST /api/video?url=<链接>&name=x.mp4` | 一步下载视频 | 视频 → 302 到代理流；图文 → 返回 JSON |
| `GET /api/media?via=dyxhs&url=<链接>&name=x.mp4` | 视频代理流 | 视频 `download_url` 指向这里 |
| `GET /api/media?via=dyxhs_img&url=<链接>&index=N&name=x.jpg` | 单张图下载 | 图文 `images[].download_url` 指向这里；支持 Range |
| `GET /api/download?u=<直链>&name=x` | 通用直链代理 | 代理任意 URL，支持 Range 续传 |
| `GET /health` | 健康检查 | `engine` 显示当前引擎 |

### 参数

| 参数 | 适用端点 | 说明 |
|---|---|---|
| `url` | parse / video / media | 抖音链接或整段分享文案（parse/video 也接受 `q`） |
| `name` | video / media / download | 自定义下载文件名（中文需 percent-encode，见第六节） |
| `index` | media（`via=dyxhs_img`） | 图片序号，从 0 开始 |
| `via` | media | `dyxhs`（视频）/ `dyxhs_img`（图片）/ `omni`（仅 `OMNI_ENABLED=1` 时生效，否则回落 dyxhs） |

### POST 提交长文案

`/api/parse` 与 `/api/video` 支持 POST，表单或 JSON 均可：

```bash
curl -X POST "https://your-domain.example/api/parse" \
  --data-urlencode "url=7.92 复制打开抖音，看看【xxx】的作品 https://v.douyin.com/1KyuRCvMRN0/ ..."

curl -X POST "https://your-domain.example/api/parse" \
  -H "Content-Type: application/json" -d '{"url":"https://v.douyin.com/xxxx/"}'
```

---

## 四、Python 示例

> 经域名访问**必须带浏览器 User-Agent**，否则 Cloudflare 返回 403。

```python
import json, time, urllib.parse, urllib.request

API = "https://your-domain.example"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

def get(url, timeout=180):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)

def save(url, path):
    with get(url) as r, open(path, "wb") as f:
        while chunk := r.read(65536):
            f.write(chunk)
    print("已保存:", path)

def grab(share_text, outdir="."):
    info = json.load(get(f"{API}/api/parse?{urllib.parse.urlencode({'url': share_text})}"))
    if info.get("ok") is False:
        raise SystemExit("解析失败: " + info["error"])
    if info["media_type"] == "image":
        for im in info["images"]:
            save(im["download_url"], f"{outdir}/{im['filename']}")
            time.sleep(2)
    else:
        save(info["download_url"], f"{outdir}/{info['filename']}")
    return info

grab("https://v.douyin.com/kCcouifI_dQ/")
```

---

## 五、错误处理

| HTTP | 返回 | 原因与处理 |
|---|---|---|
| `401` | `API_KEY_REQUIRED` / `INVALID_API_KEY` | 没带 key / key 错误；签名链接过期或被改动也返回 401 |
| `429` | `RATE_LIMITED` | 超过 `QPS_LIMIT`（默认不限） |
| `400` | `未能从输入中提取抖音或快手链接…` | 输入里没有抖音/快手链接 |
| `400` | `缺少 url 参数` / `缺少 u 参数` | 忘传参数 |
| `502` | `所有解析引擎均失败…` | 视频链接失效/被删/私密，或上游临时故障；稍后重试 |
| `502` | `图集解析失败` | 图文链接失效，或上游取图失败 |
| `502` | `上游 HTTP xxx` / `上游连接失败` | 下载代理阶段上游出错 |
| `404` | `未知端点…` | 路径写错 |

---

## 六、注意事项

1. **无元数据、无画质选择**：dyxhs 只给一个视频流，画质由上游决定（可能是 h264 576×1024，也可能是 hevc 1080×1920）。HEVC 打不开时：`ffmpeg -i in.mp4 -c:v libx264 -c:a copy out.mp4`
2. **Range 续传**：图片（`via=dyxhs_img`）和 `/api/download` 支持（206）；**视频流（`via=dyxhs`）不支持**，会返回全量。
3. **图文解析会逐张探测**（index 0,1,2… 直到失败），图多时较慢（3 张约 7 秒）；同一链接 10 分钟内命中缓存。最多探测 `NOTE_MAX_IMAGES` 张（默认 35）。
4. **节流**：请保持 ≥2 秒/次，批量下载时 `sleep 2`。
5. **中文参数必须编码**（经域名时，CF 会对原始 UTF-8 返回空 400）：
   ```bash
   curl -L -G "https://your-domain.example/api/video" \
     --data-urlencode "url=https://v.douyin.com/1KyuRCvMRN0/" \
     --data-urlencode "name=我的视频.mp4" -o 我的视频.mp4
   ```
6. **支持抖音和快手**。其他平台返回 400。
7. **需要 API Key**（v8 起），见文首。`download_url` 为签名链接，6 小时有效（`SIGN_TTL`）。
8. **本服务是适配/转发层**：链接 → 真实文件地址依赖第三方 dyxhsdownloader，对方协议变化时可能短时失效。

---

## 七、环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMNI_ENABLED` | `0` | `1` 才启用 OmniParser（**当前不建议**，见 v6 变更） |
| `NOTE_MAX_IMAGES` | `35` | 图文最多探测张数 |
| `PARSE_TTL` | `600` | 解析结果缓存（秒） |
| `DIRECT_TTL` | `1500` | 直链缓存（秒，仅 Omni） |
| `OMNI_MIN_INTERVAL` | `2.5` | Omni 请求间隔（秒） |
| `STATE_FILE` | `/root/douyin-api/state.json` | Omni 配额熔断状态 |
| `API_KEY` / `API_KEYS` | （.env 中配置） | 访问 key；都为空时鉴权关闭（启动日志会提示） |
| `QPS_LIMIT` | `0` | 每个 key 每秒请求上限，0 = 不限 |
| `SIGN_TTL` | `21600` | download_url 签名有效期（秒） |
| `SIGN_SECRET` | 由第一个 key 派生 | 下载链接签名密钥 |
| `API_ENV_FILE` | `/root/douyin-api/.env` | 读取上述配置的文件（环境变量优先） |

设置方式：在 `/etc/systemd/system/douyin-api.service` 的 `[Service]` 下加 `Environment=OMNI_ENABLED=1`，然后 `systemctl daemon-reload && systemctl restart douyin-api`。

---

## 八、运维

```bash
systemctl status douyin-api cloudflared-douyin
systemctl restart douyin-api
curl -s http://127.0.0.1:8765/health
tail -n 50 /root/douyin-api/server.log         # API 日志（systemd 追加写入此文件）
journalctl -u cloudflared-douyin -n 30 --no-pager
```

| 服务 | 作用 | 配置 |
|---|---|---|
| `douyin-api.service` | API 本体（8765） | `/root/douyin-api/server.py` |
| `cloudflared-douyin.service` | CF 隧道（域名入口） | `/etc/douyin-api/cloudflared.yml` |

---

## 附：v6 实测记录（2026-10-17，经域名）

- `/health` → `{"ok": true, "engine": "dyxhs", ...}`
- 视频 `v.douyin.com/1KyuRCvMRN0/`：parse 约 5.4 s，`aweme_id 7081608169876753702`；`/api/video` 下载 1,214,095 字节，MD5 `a72beaca6417e582fa0a8afc71e2e728`（与 10-15 版本一致）
- 图文 `v.douyin.com/kCcouifI_dQ/`：parse 约 6.8 s，返回 3 张（350,586 / 275,090 / 304,629 字节），内容与原作（大风车·森系护肤品渲染）一致
- 单图 Range：`-r 0-999` → `206`，`content-range: bytes 0-999/275090`
- 错误分支：`url=abc` → 400；未知路径 → 404

---

## v7（2026-10-19）：新增快手

同样的三个端点直接支持快手，无需登录/Cookie：

- 支持链接：`v.kuaishou.com/xxx`、`kuaishou.com/f/xxx`、`kuaishou.com/short-video/<id>`、`fw/photo/<id>`（chenzhongtech / gifshow），或整段分享文案
- `/api/parse` 返回 `platform: "kuaishou"`、`photo_id`、**标题、作者、封面、时长、分辨率**、`direct_url`（kwimgs CDN 无水印直链，可裸连，支持 Range）、`download_url`（经本服务代理）
- `/api/video` 视频 302 到代理流；图集返回 JSON，`images[]` 每张一个 `download_url`
- 实现：`kuaishou.py`（短链跳转取 photoId → `v.m.chenzhongtech.com/fw/photo/<id>` 页面 `window.INIT_STATE` → `photo.mainMvUrls`），失败回退 `m.gifshow.com`
- 文件名：`ks_<photoId>_<标题前40字>.mp4`
- v8 起也支持 `c.kuaishou.com/fw/photo/<id>`、`m.kuaishou.com/...` 等子域名链接

```bash
curl -L "https://your-domain.example/api/video?url=https://www.kuaishou.com/f/X2tFbHVu5nnd5nd" -o 快手.mp4
```

---

## v8（2026-10-21）：快手图集 + API Key

**快手图集（photoType = `VERTICAL_ATLAS` / `HORIZONTAL_ATLAS`）**，`/api/parse` 返回：

```json
{
  "ok": true, "platform": "kuaishou", "engine": "kuaishou-share",
  "photo_id": "3x86rz8fnvdtcwa", "photo_type": "VERTICAL_ATLAS", "media_type": "image",
  "title": "是你的喜欢让我的喜欢变得有意义 #可爱壁纸 …", "author": "…", "cover": "https://…",
  "image_count": 31,
  "images": [
    {"index": 0, "filename": "ks_3x86rz8fnvdtcwa_…_1.webp",
     "direct_url": "https://p2.a.yximgs.com/ufile/atlas/…_0.webp",
     "download_url": "https://your-domain.example/api/download?u=…&name=…&exp=…&sig=…"}
  ],
  "music": {"filename": "ks_…_music.m4a", "direct_url": "https://…m4a", "download_url": "…"},
  "download_url": "（第一张图）", "direct_url": null
}
```

- 图片优先取 `.webp` 原图（与 App 一致），`direct_url` 可裸连；`music` 为图集配乐（有则返回）
- `/api/video` 对图集返回同样的 JSON（无单个视频可 302）
- 实现移植自 [ucmao/media-parser](https://github.com/ucmao/media-parser)（MIT）的快手 atlas 解析：atlas 同时从 `photo.ext_params.atlas` 和与 `photo` 同级的 `atlas` 收集，CDN 取 `cdn`（字符串/列表）或 `cdnList[].cdn`

批量下载图集：

```bash
B=https://your-domain.example
curl -s -H "X-API-Key: $KEY" "$B/api/parse?url=https://v.kuaishou.com/fXITKr" \
  | jq -r '.images[] | "\(.download_url) \(.filename)"' \
  | while read u f; do curl -sL "$u" -o "$f"; done
```

### v8 实测（2026-10-21，本机 + 域名）

- 快手图集 `v.kuaishou.com/fXITKr`：31 张 webp + 配乐，首张/末张 download_url → 206 image/webp，配乐 → 206 audio/mp4
- 快手图集 `c.kuaishou.com/fw/photo/3xwiiyhbcpcy8dc`：6 张 + 配乐；整段分享文案 POST JSON 同样解析成功
- 快手视频 `c.kuaishou.com/fw/photo/3xnh6zqupyhfm5e`：`media_type: video`，download_url → 206 video/mp4
- 抖音视频 `www.douyin.com/video/7442274736408726819`、短链 `v.douyin.com/PSurH4fLass/`：带 key 200，download_url 200 video/mp4，`/api/video` 302 到签名地址
- 鉴权：无 key 401 `API_KEY_REQUIRED`，错 key 401 `INVALID_API_KEY`，篡改 sig 401，`/health` 免 key 200，`OPTIONS` 204

