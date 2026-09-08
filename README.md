# LiveCore · 智核

模块化、可扩展的 **B 站直播间智能互动框架 / Python SDK 核心层**。项目已经完成从底层协议、实时事件管线、AI/行为层到生产连接生命周期管理的阶段性收尾，可作为 Vite / Web API / Dashboard 的后端核心。

> 仅供技术实验与学习。请遵守哔哩哔哩用户协议，自行承担账号风险。自动刷屏、伪造在线、绕过风控不在本项目默认范围内。

## 当前状态

**M7 Finalization 已完成。** 主分支当前包含生产连接层、健康快照、多房间生命周期管理、guest / authenticated 两种握手模式，以及面向 LiveCore 前端的统一事件监控投影。

- WebSocket：心跳、指数退避 + jitter 重连、连接生命周期清理
- Protocol：B 站 packet header、zlib / Brotli、嵌套 packet 展开、malformed packet 防护
- Events：弹幕、礼物、上舰、Super Chat、人气等事件解析与分发，并保留协议字段到 `meta`
- Monitor：统一输出 `事件类型 + 用户 + 礼物 + 金额 + meta 摘要`
- Engine：上下文 → dispatcher → rules → postprocess → suggestion queue
- Behavior：观看行为与节奏模拟，默认不执行出站动作
- Stability：SQLite 异步持久化、配置热更新、告警、multi-room supervision
- HTTP：danmu endpoint / token / real room id 握手，bounded timeout 与响应校验
- Supervisor：单房间 / 多房间连接生命周期 + `ConnectionHealth` 健康快照
- Python：CI 覆盖 3.11 / 3.12 / 3.13

## 分层架构

```
Vite / Dashboard / Web API
          │
          ▼
ConnectionSupervisor / MultiRoomSupervisor
          │
          ▼
BiliLiveClient ── WebSocket / heartbeat / reconnect
          │
          ▼
Protocol ── packet expand / Brotli / zlib
          │
          ▼
Parser → LiveEvent → Monitor Projection
          │                    │
          ▼                    ▼
Dispatcher / Engine       Vite WebSocket
          │
          ▼
Context / Rules / Postprocess / Suggestion Queue
```

## Vite 对接

本仓库定位为 **Python 核心 SDK / backend connection layer**，Vite 不需要直接实现 B 站协议。

当前 `livecore` 前端 adapter 通过 HTTP + WebSocket 消费 SDK：

```text
GET    /api/rooms
POST   /api/rooms/:room_id/start
DELETE /api/rooms/:room_id
GET    /api/rooms/:room_id/health
WS     /api/rooms/:room_id/events
```

WebSocket 的事件消息结构：

```json
{
  "type": "event",
  "event": {
    "id": "evt123",
    "ts": 1750000000.0,
    "kind": "gift",
    "room_id": 123456,
    "user": {"uid": 10001, "name": "观众", "guard": 3, "medal": "粉丝牌"},
    "gift": {"name": "小心心", "num": 3, "price": 100},
    "meta": {"gift_id": 1, "total_coin": 300},
    "monitor": {
      "kind": "gift",
      "kind_label": "礼物",
      "user": {"uid": 10001, "name": "观众", "guard": 3, "medal": "粉丝牌"},
      "gift": {"name": "小心心", "num": 3, "unit_price": 100},
      "amount": {"value": 300, "currency": "gold_coin"},
      "text": "观众 投喂 小心心 x3",
      "meta_summary": "gift_id=1 total_coin=300",
      "raw_cmd": "SEND_GIFT",
      "popularity": 0
    }
  }
}
```

`monitor` 是给 UI / 日志系统使用的稳定投影；`meta` 仍然保留协议相关字段，方便高级消费者继续使用。CLI 长连接示例与前端使用同一个 `monitor_event()` 投影，因此两边不会出现两套事件格式。

## 长连接监控示例

```bash
python examples/run_long_connection.py 1814378608
```

典型输出：

```text
[03:12:08] [礼物/gift] user=观众 (guard=3, medal=粉丝牌) | gift=小心心 x3 | amount=300 gold_coin | meta=gift_id=1 total_coin=300
[03:12:11] [弹幕/danmaku] user=观众 (medal=粉丝牌) | gift=- | amount=- | meta=mode=1 font_size=25 color=16777215 | text=晚上好
```

JSONL 模式可以直接交给日志采集器：

```bash
python examples/run_long_connection.py 1814378608 --json
```

`Ctrl+C` / `SIGTERM` 会进入 supervisor shutdown，等待连接、心跳与重连任务清理后再退出，不使用 `loop.stop()` 或 `os._exit()`。

## 模块地图

| 模块 | 职责 | 阶段 |
| --- | --- | --- |
| `protocol.py` | B 站弹幕协议封包 / 解包、zlib / Brotli 展开、嵌套 packet | 1 / M6 |
| `client.py` | WebSocket 连接、25s 心跳、指数退避 + 抖动重连、生命周期清理 | 1 / M6 |
| `parser.py` | 服务器通知帧 → `LiveEvent` | 1 |
| `monitor.py` | `LiveEvent` → UI / CLI 稳定监控投影 | M7 |
| `dispatcher.py` | 按事件类型路由，支持通配符订阅 | 2 |
| `context.py` | 单房间滚动上下文 + 去重窗口 | 2 |
| `rules.py` | 关键词 / 事件规则匹配、情绪降级 | 2 |
| `scheduler.py` | 冷启动、最小间隔、打卡与氛围节奏 | 2 / 4 |
| `postprocess.py` | 长度截断、刷屏词拦截、表情后缀 | 2 |
| `adapters.py` | `OutboundAdapter` / `AiAdapter` 协议与默认模拟器 | 2 |
| `engine.py` | 收流 → 上下文 → 分发 → 规则 → 后处理 → 建议队列 | 3 |
| `bili_http.py` | 进房握手、host / token / 真实房间号解析、timeout / validation | 3 / M6 |
| `behavior.py` | 高斯抖动、打字耗时、活跃度调频、观看行为 | 4 |
| `config.py` | JSON 配置外置 + mtime 热更新 | 5 |
| `alert.py` | 连续失败计数、冷却去重、可插拔告警 | 5 |
| `store.py` | SQLite 持久化事件与回复、异步写盘、上下文恢复 | 5 |
| `multi.py` | 多直播间监督、房间隔离、热更新 | 5 |
| `supervisor.py` | 单 / 多房间连接生命周期与健康快照 | M7 |

## 安全边界

- 默认出站适配器是 `SimulatorAdapter`，只在本地记录，**不发网络请求**。
- `alert.py` 的 webhook / email 通道默认关闭；email 通道不包含 SMTP 凭据。
- `store.py` 默认不写盘。
- 空 token 默认按 **guest mode** 处理；只有 `HttpConfig(require_token=True)` 才会强制 token 校验。
- token、密钥、房间号一律放在本地 `config.json`（已在 `.gitignore` 中），模板见 `config.example.json`。

## 测试与兼容性

CI 使用 Python 3.11、3.12、3.13 运行完整 pytest suite。

本地运行：

```bash
pip install -e .
pytest -q
```

## 项目边界

LiveCore 已完成 **核心连接 SDK / 事件处理层** 的收尾。Vite UI 继续独立维护，通过 adapter 消费 `monitor` 投影即可；无需把 B 站协议逻辑塞进浏览器。

1. Python API adapter：管理 room lifecycle、health、event stream
2. Vite：消费 REST / WebSocket，并直接渲染 `event.monitor`
3. Redis / MQ：只有在需要跨进程、跨机器扩展时再引入
4. Auth / rate limit：放在 API gateway 层，而不是污染核心协议层

这样可以保持 LiveCore 的轻量、可测试和平台无关性，并方便以后把相同抽象扩展到其他直播平台。
