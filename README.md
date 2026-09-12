# LiveCore · 智核

模块化、可扩展的 **B 站直播间智能互动框架 / Python SDK 核心层**。项目已经完成从底层协议、实时事件管线、AI/行为层到生产连接生命周期管理的阶段性收尾，可作为 Vite / Web API / Dashboard 的后端核心。

> 仅供技术实验与学习。请遵守哔哩哔哩用户协议，自行承担账号风险。自动刷屏、伪造在线、绕过风控不在本项目默认范围内。

## 当前状态

**M7 Finalization 已完成，连接层已完成平台抽象化改造。** 主分支当前包含生产连接层、健康快照、多房间生命周期管理、guest / authenticated 两种握手模式，以及面向 LiveCore 前端的统一事件监控投影。

- **Platforms**：`PlatformAdapter` 抽象层，B 站为默认适配器，接入新平台无需改动传输层
- WebSocket：30s 应用层心跳、指数退避 + jitter 重连、连接生命周期清理
- Protocol：B 站 packet header、zlib / Brotli、嵌套 packet 展开、malformed packet 防护
- Events：弹幕、礼物（含 protobuf 版 `SEND_GIFT_V2`）、进场（`INTERACT_WORD_V2` / `ENTRY_EFFECT`）、上舰、Super Chat、人气等事件解析与分发，并保留协议字段到 `meta`
- Monitor：统一输出 `事件类型 + 用户 + 礼物 + 金额 + meta 摘要`
- Engine：上下文 → dispatcher → rules → postprocess → suggestion queue
- Behavior：观看行为与节奏模拟，默认不执行出站动作
- Stability：SQLite 异步持久化、配置热更新、告警、multi-room supervision
- HTTP：danmu endpoint / token / real room id 握手，bounded timeout 与响应校验、Wbi 签名
- Supervisor：单房间 / 多房间连接生命周期 + `ConnectionHealth` 健康快照
- Python：CI 覆盖 3.11 / 3.12 / 3.13

### 连接稳定性：协议层 keepalive 必须关闭

`websockets` 库默认开启 RFC 6455 心跳探测（`ping_interval=20`、`ping_timeout=20`、`close_timeout=10`）。B 站弹幕服务器**不回应**协议层 ping 帧，因此默认配置下客户端会认为自己失联：

```text
套接字异常：sent 1011 (internal error) keepalive ping timeout; no close frame received
```

表现为**每 50 秒（20 + 20 + 10）必然断线重连一次**，即使应用层 30s 心跳一直在正常收发。

正确做法是关闭库的探测、只保留平台要求的应用层心跳：

```python
# livecore/platforms/base.py
transport_keepalive_ping: ClassVar[bool] = False   # BilibiliAdapter 覆写为 False
```

> 参考：B 站官方长链协议文档 —— 「握手后 30s 内未发送心跳包会被强制断开」，心跳周期 30s。

## 分层架构

```
Vite / Dashboard / Web API
          │
          ▼
ConnectionSupervisor / MultiRoomSupervisor
          │
          ▼
LiveConnection ── WebSocket / heartbeat / reconnect   ← 平台无关
          │
          ▼
PlatformAdapter ── bilibili / <your platform>          ← 唯一平台相关层
     │        │
     │        ├── Protocol ── packet expand / Brotli / zlib
     │        └── SchemaStore ── schemas/*.pb.json (mtime 热更新 + 覆盖)
     ▼
Parser → LiveEvent → Monitor Projection
          │                    │
          ▼                    ▼
Dispatcher / Engine       Vite WebSocket
          │
          ▼
Context / Rules / Postprocess / Suggestion Queue
```

## 平台适配层

传输层（`livecore/connection.py`）只知道「开 socket、按适配器说的发心跳、把裸帧交回适配器解码」。
所有平台细节都收敛在 `PlatformAdapter` 之后，所以接入新平台 = 写一个适配器 + 注册，不改动传输层、
supervisor、engine，也不影响其他平台。

```python
from livecore import LiveConnection, PlatformAdapter, register_adapter, get_adapter

class MyPlatformAdapter(PlatformAdapter):
    name = "my-platform"
    display_name = "My Platform"
    heartbeat_interval_sec = 30.0
    transport_keepalive_ping = True      # 该平台是否回应协议层 ping

    async def fetch_endpoint(self, room_id): ...          # 握手 → LiveEndpoint
    def convert_endpoint(self, endpoint): ...
    def build_auth_packet(self, endpoint): ...
    def build_heartbeat_packet(self): ...
    def decode_frame(self, raw, room_id): ...             # 裸帧 → LiveEvent

register_adapter(MyPlatformAdapter())
connection = LiveConnection(log, get_adapter("my-platform"))
```

适配器接口（`livecore/platforms/base.py`）：

| 成员 | 作用 |
| --- | --- |
| `normalize_room_id` / `fetch_endpoint` | 房间号校验与握手，返回平台无关的 `LiveEndpoint` |
| `build_auth_packet` / `build_heartbeat_packet` | 上行帧构造 |
| `decode_frame` | 下行帧 → `InboundFrame`（`AUTH_OK` / `HEARTBEAT` / `EVENT`） |
| `transport_options` | 透传给 `websockets.connect` 的参数（含 keepalive 开关） |
| `connection_headers` | 握手 HTTP 头 |

所有既有入口都保留且默认走 B 站，无需改动调用方：

- `LiveEngine(..., platform="bilibili")`
- `ConnectionSupervisor(room_id, log, platform=...)` / `MultiRoomSupervisor(log, platform=...)`
- `BiliLiveClient(log)` 仍可用（内部是绑定 B 站适配器的 `LiveConnection`）
- `fetch_danmu_endpoint(room_id)` 仍是公开的 B 站握手入口

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

### 新版协议字段：protobuf 热更新配置

B 站已把若干指令从纯 JSON 改为 Base64 protobuf（`data.pb`）。字段号属于**私有 schema、随时可能变**，
所以 `livecore` 不把它们写在代码里，而是放在一份**可热更新的配置**中：

```text
livecore/schemas/bilibili.pb.json     # 随包的默认字段表（唯一事实来源）
```

`livecore/schema.py` 负责加载、校验、按 mtime 轮询热更新。**改文件即生效**：运行中的长连接在下一次
收包时（`poll_sec`，默认 2s）就会用新字段号解析，不需要重启、不需要重连、不需要发版。

字段值支持嵌套路径：`9` 表示 `message(9)`，`"9.3"` 表示 `message(9).message/text(3)`。

```json
{
  "commands": {
    "INTERACT_WORD_V2": {
      "source": "pb",
      "fields": { "uid": 1, "uname": 2, "msg_type": 5, "medal_name": "9.3", "uinfo_name": "22.2.1" }
    },
    "SEND_GIFT_V2": {
      "source": "pb",
      "fields": { "uname": 2, "gift": 10, "gift_id": "10.1", "num": "10.3", "price": "10.5", "coin_type": "10.8" }
    },
    "ONLINE_RANK_V3": {
      "source": "pb",
      "fields": { "rank_type": 1, "list": 3 },
      "lists":  { "list": { "uid": 1, "uname": 4, "rank": 5, "guard_level": 6 } }
    }
  }
}
```

> `lists` 描述「重复子消息」的字段表（高能榜这类列表），每个元素都会包装成独立的读取器。

当前覆盖的指令：

| 指令 | 说明 | 关键字段 |
| --- | --- | --- |
| `INTERACT_WORD_V2` | 进场 / 关注 / 分享（取代 `INTERACT_WORD`） | `#1 uid`、`#2 uname`、`#5 msg_type`、`#9.{2 等级, 3 名称}`、`#22.2.1 昵称` |
| `SEND_GIFT_V2` | 礼物（取代 `SEND_GIFT`） | `#2 uname`、`#10.{1 gift_id, 2 名称, 3 数量, 5 单价, 8 货币, 9 batch, 12 combo}` |
| `ONLINE_RANK_V3` | 高能榜（取代 `ONLINE_RANK_V2`） | `#1 rank_type`、`#3[] {1 uid, 4 uname, 5 rank, 6 大航海等级}` |

**三种覆盖方式**（优先级由低到高，都不用改包内文件）：

1. 环境变量 `LIVECORE_SCHEMA_PATH` / `LIVECORE_SCHEMA_BILIBILI` 指向覆盖文件；
2. `config.json` → `protobuf.schema_path` / `protobuf.overlay_path`；
3. `config.json` → `protobuf.commands`（内联覆盖，只写要改的字段）。

```json
{
  "protobuf": {
    "poll_sec": 2.0,
    "commands": {
      "SEND_GIFT_V2": { "fields": { "price": "10.7" } }
    }
  }
}
```

覆盖是**深合并**，没写的字段保持原值；`load()` 校验失败时保留上一份可用快照并回调错误监听器，
**坏配置不会打断正在运行的连接**（只会退化为缺字段）。

### 字段漂移检测

字段号变化不会让解析器崩溃，只会静默丢数据 —— 所以提供专门的巡检工具：

```bash
python examples/inspect_pb_schema.py 21452505 --duration 90          # 有漂移时退出码非 0
python examples/inspect_pb_schema.py 21452505 --dump                 # 打印真实嵌套结构，便于补字段号
```

```text
[02:25:26] SEND_GIFT_V2: DRIFT  samples=1 fields=[1, 2, 3, 8, 10, 11, 13, 15]
    + field 3 present in traffic but NOT in schema (add it to commands.SEND_GIFT_V2.fields)
    + field 11 present in traffic but NOT in schema (add it to commands.SEND_GIFT_V2.fields)
[02:29:24] INTERACT_WORD_V2: ok  samples=8 fields=[1, 2, 4, 5, 6, 7, 8, 12, 15, 19, 22, 23, 24]
[02:29:24] ONLINE_RANK_V3: ok  samples=1 fields=[1, 3]
schema_drift=YES
```

上面这段就是本项目真实的巡检输出：它当场发现 `SEND_GIFT_V2` 的 `#3`（face）等字段没被映射
（报告里 `SEND_GIFT_V2` 因此是 `DRIFT`），修好配置后再跑就变成 `ok` / `schema_drift=no`。

> **游客连接会被限流（实测）**：2026-09-13 在 17 万人在线的房间用游客连接采样 45 秒，只收到
> **3 条弹幕**、且**一条礼物流都没有**；同时段只稳定收到进场 / 人气 / 点赞类事件。B 站已对未登录连接
> 做风控限流，昵称 mid 归零、昵称打码，礼物类指令基本不下发。
> 需要完整弹幕与礼物流时，请走认证模式：
>
> ```python
> BilibiliAdapter(uid=<mid>, auth_token="<SESSDATA>", buvid="<buvid3>")
> # 或 HttpConfig(require_token=True) + 本地 config.json 中的凭据
> ```
>
> `INTERACT_WORD_V2` 的 `#22.2.1`（pb 内昵称）在打码时同样为空，此时会回退为「观众」。

## 长连接监控示例

```bash
python examples/run_long_connection.py 1814378608
```

指定平台 / 限定运行时长 / 输出 JSONL：

```bash
python examples/run_long_connection.py 1814378608 --platform bilibili --duration 600
python examples/run_long_connection.py 1814378608 --json
```

可用参数：`--platform`（适配器名）、`--duration`、`--json`、`--raw`、`--buvid`、`--schema`、`--schema-overlay`、`--quiet-log`。
`--json` 模式下 stdout 是**纯 JSONL**，人类可读的连接状态与总结全部走 stderr，可直接接日志采集器。

典型输出：

```text
[03:12:08] [礼物/gift] user=观众 (guard=3, medal=粉丝牌) | gift=小心心 x3 | amount=300 gold_coin | meta=gift_id=1 total_coin=300
[03:12:11] [弹幕/danmaku] user=观众 (medal=粉丝牌) | gift=- | amount=- | meta=mode=1 font_size=25 color=16777215 | text=晚上好
[03:12:14] [进场/enter] user=夜飞霜雨 (medal=忆者) | gift=- | amount=- | meta=msg_type=1 medal_level=26 | text=夜飞霜雨 进入直播间
```

`Ctrl+C` / `SIGTERM` 会进入 supervisor shutdown，等待连接、心跳与重连任务清理后再退出，不使用 `loop.stop()` 或 `os._exit()`。

## 模块地图

| 模块 | 职责 | 阶段 |
| --- | --- | --- |
| `platforms/base.py` | `PlatformAdapter` / `LiveEndpoint` / `InboundFrame` 抽象 | M8 |
| `platforms/registry.py` | 适配器注册表与默认平台 | M8 |
| `platforms/bilibili.py` | B 站适配器（握手 / 上行帧 / 下行帧解码） | M8 |
| `connection.py` | 平台无关 WebSocket 连接、心跳、重连、帧分发 | M8 |
| `client.py` | 绑定 B 站适配器的 `BiliLiveClient`（兼容旧入口） | 1 / M8 |
| `protocol.py` | B 站弹幕协议封包 / 解包、zlib / Brotli 展开、嵌套 packet | 1 / M6 |
| `pb.py` | 零依赖 protobuf 字段读取器（`*_V2` 指令） | M8 |
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
- 长连接示例只读：不发送弹幕、不执行任何出站动作。
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
