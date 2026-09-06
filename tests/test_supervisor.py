from __future__ import annotations

import pytest

from livecore.logger import RingLogger
from livecore.supervisor import ConnectionHealth


def test_connection_health_defaults_to_offline():
    h = ConnectionHealth(room_id=1)
    assert h.state == "offline"
    assert h.reconnects == 0
    assert h.live_for_sec == 0.0


def test_connection_health_live_for_sec():
    h = ConnectionHealth(room_id=1, last_live_at=0.0)
    assert h.live_for_sec == 0.0


def test_supervisor_exports():
    from livecore.supervisor import ConnectionSupervisor, MultiRoomSupervisor
    log = RingLogger()
    supervisor = MultiRoomSupervisor(log)
    assert supervisor.health() == []
    with pytest.raises(ValueError):
        ConnectionSupervisor(0, log)
