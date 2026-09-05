"""端到端接线示例：多直播间 + 热更新配置 + 告警 + 持久化。

运行：

    cp config.example.json config.json   # 填入房间号后
    python examples/run_supervisor.py

注意：默认出站适配器是模拟器，**不会向直播间发送任何内容**。要真正发弹幕，
必须自行实现 ``OutboundAdapter`` 并注入 —— 本项目不提供发送实现。

按 Ctrl+C 优雅退出。
"""

from __future__ import annotations

import asyncio
import signal

from livecore.alert import Alerter
from livecore.config import ConfigStore
from livecore.logger import RingLogger
from livecore.multi import RoomSupervisor
from livecore.store import SqliteStore

CONFIG_PATH = "config.json"


def load_config() -> ConfigStore:
    store = ConfigStore(CONFIG_PATH, autoload=False)
    try:
        store.load()
    except Exception as exc:  # 配置缺失时给出可操作的提示，而不是堆栈
        raise SystemExit(
            f"无法读取 {CONFIG_PATH}：{exc}\n请先 cp config.example.json {CONFIG_PATH}"
        ) from exc
    if not store.rooms():
        raise SystemExit(f"{CONFIG_PATH} 的 rooms 为空，请先填入至少一个房间号")
    return store


def build_supervisor(cfg: ConfigStore, log: RingLogger) -> tuple[RoomSupervisor, SqliteStore | None]:
    # 持久化与告警都按配置开关，默认全部关闭
    storage = cfg.storage_config()
    db = SqliteStore(storage.path) if storage.enabled else None
    if db is not None:
        db.open()
        db.prune(storage.retention_days)

    sup = RoomSupervisor(
        rooms=cfg.rooms(),
        config=cfg.engine_config(),
        store=db,
        log=log,
        alerter=Alerter(cfg.alert_config(), log=log),
    )
    sup.bind_config(cfg)
    return sup, db


async def main() -> None:
    cfg = load_config()

    log = RingLogger()
    log.on(lambda e: print(f"[{e.level:5}] {e.layer:8} {e.message}"))

    sup, db = build_supervisor(cfg, log)
    print(f"启动 {len(sup.room_ids)} 个直播间：{sup.room_ids}")
    await sup.start_all()

    stop = asyncio.Event()

    def _request_stop(*_a) -> None:
        print("\n收到退出信号，正在收尾…")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            pass  # Windows 上部分信号不可用，退化为 KeyboardInterrupt

    # 主循环只负责驱动配置热更新，其余由各房间的后台任务处理
    try:
        while not stop.is_set():
            cfg.maybe_reload()
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await sup.stop_all()
        if db is not None:
            db.close()
        print("已退出")


if __name__ == "__main__":
    asyncio.run(main())
