# LiveCore · 智核

模块化、可扩展的 **B 站直播间智能互动框架 / Python SDK 核心层**。项目已经完成从底层协议、实时事件管线、AI/行为层到生产连接生命周期管理的阶段性收尾，可作为 Vite / Web API / Dashboard 的后端核心。

> 仅供技术实验与学习。请遵守哔哩哔哩用户协议，自行承担账号风险。自动刷屏、伪造在线、绕过风控不在本项目默认范围内。

## 当前状态

**M7 Finalization 已完成。** 主分支当前包含生产连接层、健康快照、多房间生命周期管理，以及 guest / authenticated 两种握手模式。

- WebSocket：心跳、指数退避 + jitter 重连、连接生命周期清理
- Protocol：B 站 packet header、zlib / Brotli、嵌套 packet 展开、malformed packet 防护
- Events：弹幕、礼物、上舰、Super Chat、人气等事件解析与分发
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
Parser → Dispatcher → Context / Rules
          │
          ▼
Engine → Postprocess → Suggestion Queue
          │
          ├── Behavior / Scheduler
          ├── SQLite Store
          └── Alert / Metrics
```

## Vite 对接

本仓库定位为 **Python 核心 SDK / backend connection layer**，Vite 不需要直接实现 B 站协议。

推荐在 Vite 与 LiveCore 之间增加一个很薄的 HTTP + WebSocket API adapter：

```python
from livecore import MultiRoomSupervisor
from livecore.bili_http import HttpConfig
from livecore.logger import RingLogger

log = RingLogger()
supervisor = MultiRoomSupervisor(log)

# guest mode：允许 B 站返回空 token
await supervisor.add(123456)

# authenticated mode：显式要求 token
await supervisor.add(
    123456,
    http_config=HttpConfig(require_token=True),
)

# 给 dashboard / Vite 返回健康状态
health = supervisor.health()
```

`ConnectionHealth` 提供：

- `room_id`
- `state`：`offline / connecting / live / reconnecting / error / stopping`
- `reconnects`
- `last_live_at`
- `live_for_sec`
- `last_error`（保留为健康模型字段，便于上层 API 扩展）

建议 API adapter 向 Vite 暴露类似接口：

```text
GET    /api/rooms
POST   /api/rooms/:room_id/start
DELETE /api/rooms/:room_id
GET    /api/rooms/:room_id/health
WS     /api/rooms/:room_id/events
```

这样 Vite 只负责 UI、状态管理与交互，LiveCore 负责连接、协议、事件和业务管线。

## 模块地图

| 模块 | 职责 | 阶段 |
| --- | --- | --- |
| `protocol.py` | B 站弹幕协议封包 / 解包、zlib / Brotli 展开、嵌套 packet | 1 / M6 |
| `client.py` | WebSocket 连接、25s 心跳、指数退避 + 抖动重连、生命周期清理 | 1 / M6 |
| `parser.py` | 服务器通知帧 → `LiveEvent` | 1 |
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

CI 使用 Python 3.11、3.12、3.13 运行完整 pytest suite。M7 最终 CI 已全部通过。

本地运行：

```bash
pip install -e .
pytest -q
```

## 项目边界

LiveCore 现在已经完成 **核心连接 SDK / 事件处理层** 的收尾。后续如果继续开发，建议不要再把 Vite UI 逻辑塞进本仓库，而是单独维护 API adapter / frontend 项目：

1. Python API adapter：管理 room lifecycle、health、event stream
2. Vite：消费 REST / WebSocket
3. Redis / MQ：只有在需要跨进程、跨机器扩展时再引入
4. Auth / rate limit：放在 API gateway 层，而不是污染核心协议层

这样可以保持 LiveCore 的轻量、可测试和平台无关性，并方便以后把相同抽象扩展到其他直播平台。
