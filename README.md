# shortvid-api — 抖音 / 快手提取 API（v8）

**输入抖音链接 → 无水印视频流 / 图文全部图片。**

> **v6（2026-10-17）**：OmniParser 上游返回无关视频并封 IP，已默认关闭（`OMNI_ENABLED=0`），全部走 dyxhsdownloader；新增图文 `images[]` 支持；无标题/作者元数据、无画质选择。下方 v5 及以前的内容为历史记录，以 `API文档.md` 为准。

> 🔑 **v8（2026-10-21）**：新增快手图集解析；除 `/health` 外所有接口需 API Key（`X-API-Key` 头 / `Authorization: Bearer` / `?key=`）。key 放在服务器 `/root/douyin-api/.env`（`API_KEY=...`，chmod 600，**不入库**）；返回的 `download_url` 自带签名，可直接打开。详见 `API文档.md` 文首与 `auth.py`。

> 📖 **使用文档见 [`API文档.md`](API文档.md)** —— 面向调用方，含全部端点和实测示例。

## 访问地址

| 地址 | 说明 |
|---|---|
| **`https://your-domain.example`** | **推荐**。Cloudflare 隧道 + TLS，隐藏源站 |
| `http://YOUR_SERVER_IP:8765` | 直连 IP，少一跳更快，适合大文件 |
| `http://127.0.0.1:8765` | 本机 |

## 运行（systemd 托管，开机自启）

```bash
systemctl status douyin-api cloudflared-douyin
systemctl restart douyin-api
/root/douyin-api/restart.sh        # 等价于 systemctl restart douyin-api
```

| 服务 | 作用 | 配置 |
|---|---|---|
| `douyin-api.service` | API 本体（8765） | `/root/douyin-api/server.py` |
| `cloudflared-douyin.service` | CF 隧道（域名入口） | `/etc/douyin-api/cloudflared.yml` |

> 用隧道而非 nginx 反代：本机 **443 被 xray 占用**，隧道由 cloudflared 主动外连，不占入站端口。

## 端点

| 端点 | 说明 |
|---|---|
| `GET/POST /api/parse?url=<链接或整段文案>` | 视频 download_url / 图文 images[] |
| `GET/POST /api/video?url=<链接>` | 视频 → 302 到代理流；图文 → 返回 JSON |
| `GET /api/media?via=dyxhs&url=…` | 视频代理流（download_url 指向这里） |
| `GET /api/media?via=dyxhs_img&url=…&index=N` | 图文单张图片 |
| `GET /api/download?u=<直链>&name=x.mp4` | 通用直链代理，支持 Range 续传 |
| `GET /health` | 健康检查（含 `quota_exhausted`） |

## 引擎链路

1. **主引擎 OmniParser**（`d.hel.cc.cd`）：`GET /api/session/ticket` 拿一次性票据（120s，缓存复用）→ `POST /api/parse`（头 `X-Device-ID`/`X-Session-Ticket`）。返回标题/作者/封面/多档直链。票据 401 自动刷新重试一次。
2. **兜底 dyxhsdownloader**（`dyxhsdownloader.com/api/download?source=…&kind=video`）：无元数据，纯视频流。

> ⚠️ 这两步的「链接 → 真实视频地址」能力来自第三方站点。本服务是**适配层 + 转发层**，不是解析器本身。

## v5 新增（域名 + 隧道，本轮实测）

| 项 | 内容 |
|---|---|
| 域名 | `your-domain.example`（CNAME → 独立隧道 `599d74c9-…`，不影响 fpv/cliproxyapi） |
| 服务化 | 两个 systemd 单元，开机自启，崩溃自动重启 |
| 配额状态持久化 | 新增 `state.json`，重启后不再无效试探上游（解析 6.7s → 2.0s） |
| `/api/media` 熔断回落 | 主引擎不可用时自动转兜底，避免 `download_url` 变死链 |
| `restart.sh` 改为委托 systemd | 避免手工进程与 systemd 抢 8765 端口 |

### 域名带来的两个新坑（已记入文档）

| 现象 | 原因 | 处理 |
|---|---|---|
| Python 脚本 403 | Cloudflare 拦截 `Python-urllib` 默认 UA | 请求必须带浏览器 UA |
| URL 含原始中文 → 400 空响应 | CF 边缘拒绝未编码的 UTF-8 字节 | 中文参数必须 percent-encode（`curl -G --data-urlencode`） |


| 解析结果重复请求上游 | 同一链接反复解析 | 解析结果缓存 10 分钟（命中后 ~2ms 返回） |
| 输出泄露上游 `proxy_path` | 含带 ticket 的完整上游地址，可被绕过直连上游 | `_sanitize()` 剥离上游内部字段 |
| `download_url` 是相对路径 | 复制出来无法直接用 | 依据 `Host`/`X-Forwarded-*` 生成绝对地址（反代场景已验证） |
| 中文文件名双重编码 | `我的视频.mp4` → `æçè§é¢.mp4`（http.server 按 latin-1 解 path） | `_raw_path()` 还原 UTF-8 |
| `--data-urlencode` 无 `-G` 时 | 发 POST → 501 | 补 `do_POST`，支持表单与 JSON |

## ⚠️ 上游配额（最关键的限制）

OmniParser 的免费解析**按 IP 每日 30 次**。用尽后返回：

```json
{"message": "当前 IP 今日免费解析次数已达上限 (30/30 次)。请注册登录账号或稍后重试！",
 "quota_exceeded": true, "current_usage": 30, "daily_limit": 30}
```

**换设备指纹无效**（按 IP 计数，非按设备）。v4 的处理：

- 识别 `quota_exceeded` → 熔断至次日 0 点，期间不再无效请求上游
- 熔断期间自动走 dyxhs 兜底：**视频照样能下**，仅缺标题/作者元数据
- `/health` 的 `quota_exhausted` / `quota_reset_at` 可查状态
- 重复链接 10 分钟缓存命中，**不消耗配额**

> 本次测试即为验证机制而耗尽当日 30 次配额。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `OMNI_MIN_INTERVAL` | `2.5` | 上游最小请求间隔（秒） |
| `PARSE_TTL` | `600` | 解析结果缓存时长（秒） |
| `DIRECT_TTL` | `1500` | 直链缓存时长（秒） |
| `STATE_FILE` | `/root/douyin-api/state.json` | 配额熔断状态持久化（跨重启） |
| `OMNI_DEVICE_REALM` | `web` | 设备指纹命名空间 |

## 实测（2026-10-15，本机房 IP）

主链路（配额充足时）：
- 短链 `v.douyin.com/1KyuRCvMRN0/` → 标题「优雅永不过时…」、作者「鹅脖沾蒜泥」、3 档画质、完整 MP4（h264 576×1024 + AAC，8.13s，1,214,095 字节）
- PC 长链 `www.douyin.com/video/7419155125425272136` → 标题/作者齐全，返回 7 档画质
- 三档画质分别下载并 `ffprobe` 验证：576×1024 / 720×1280 / 576×1024
- `curl` / `wget` / Python 三种客户端下载结果 MD5 一致
- 直链可裸连（无需 Referer），抖音 CDN 侧不封本机 IP
- Range 支持：206 精确分片

经域名 `https://your-domain.example` 复测：
- `/health` 200，TLS 证书有效（`ssl_verify_result: 0`），解析+下载全通
- 隧道下下载 1,214,095 字节 / 7.98s（约 152 KB/s）
- `curl` / `wget` / Python（带 UA）/ `-G --data-urlencode` 中文名 全部通过
- CF 不缓存 API（`cf-cache-status: DYNAMIC`）

降级链路（配额用尽时）：
- 仍可下载完整 MP4，但**画质不固定**：实测同一链接两次分别得到 h264 576×1024 与 **hevc 1080×1920**
- `fmt=N` 在降级期间不生效（兜底只有一个流）
- 忽略 Range，返回全量

其他：
- 错误分支：400（无有效链接）/ 502（链接失效）/ 404（路径错误）
- 上游风控为**临时性**（60s 冷却后新指纹即恢复），非 IP 永久封禁
- 上游另有**每日 30 次/IP 免费配额**（本轮测试耗尽，用于验证熔断机制）

## 备选引擎评测记录

- **hellotik.app**：Next.js。协议为 v3 加密（`gate-e5eea8`/`gate-4a160a` 发票 → AES-GCM 加密 payload）。客户端有 `unencryptedFallback` 死代码但服务端已拒绝（422 TICKET_REQUEST_MISMATCH）。理论可用 Python cryptography 复刻，成本高，暂不需要。
- **dyxhsdownloader**：`/api/parse` 被 Turnstile 锁死；`/api/download` 可用作兜底。
- 直连 iesdouyin 解析（v1 遗留）：机房 IP 被风控，住宅 IP 可行（`DOUYIN_PROXY` 环境变量已接好）。

## 根本局限

解析能力依赖第三方站点，对方无 SLA、协议可能变更。要完全自主需给 v1 直连引擎配住宅代理出口——**卡点是 IP，不是代码**。

## 致谢 / 第三方代码

快手图集（atlas）解析思路与 API-Key 鉴权模式参考并移植自
[ucmao/media-parser](https://github.com/ucmao/media-parser)（MIT License，Copyright (c) ucmao）。
相关代码见 `kuaishou.py`（`_atlas` / `_cdns` / `_join`）与 `auth.py`。

