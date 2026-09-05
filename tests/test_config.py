"""Tests for livecore.config — externalised config with hot reload."""

from __future__ import annotations

import json
import os
import time

import pytest

from livecore.config import AlertConfig, ConfigError, ConfigStore, StorageConfig
from livecore.types import EngineConfig


@pytest.fixture()
def cfg_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"rooms": [1, 2], "engine": {"min_gap_sec": 30}}), encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------- loading


def test_load_reads_rooms_and_engine_overrides(cfg_path):
    store = ConfigStore(cfg_path)
    assert store.rooms() == [1, 2]
    assert store.engine_config().min_gap_sec == 30
    # 未覆盖的字段保持默认
    assert store.engine_config().reply_max_len == 24


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="配置文件不存在"):
        ConfigStore(str(tmp_path / "nope.json"))


def test_malformed_json_raises_config_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError, match="合法 JSON"):
        ConfigStore(str(path))


def test_non_object_root_raises_config_error(tmp_path):
    path = tmp_path / "list.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigError, match="JSON 对象"):
        ConfigStore(str(path))


def test_unknown_keys_are_ignored_not_fatal(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"engine": {"typo_key": 1, "min_gap_sec": 5}}), encoding="utf-8")
    store = ConfigStore(str(path))
    assert store.engine_config().min_gap_sec == 5


def test_defaults_applied_for_missing_sections(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    store = ConfigStore(str(path))
    assert store.rooms() == []
    assert isinstance(store.alert_config(), AlertConfig)
    assert isinstance(store.storage_config(), StorageConfig)
    assert store.ai_settings() == {"provider": "none"}


def test_nested_sections_merge_with_defaults(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"alert": {"enabled": True}}), encoding="utf-8")
    store = ConfigStore(str(path))
    assert store.alert_config().enabled is True
    # 其余告警字段仍取默认值
    assert store.alert_config().failure_threshold == 3


def test_rooms_filters_non_numeric(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"rooms": [1, "2", "abc", None]}), encoding="utf-8")
    assert ConfigStore(str(path)).rooms() == [1, 2]


# ---------------------------------------------------------------- hot reload


def test_reload_picks_up_changes(cfg_path):
    store = ConfigStore(cfg_path)
    assert store.engine_config().min_gap_sec == 30
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [7], "engine": {"min_gap_sec": 60}}, fh)
    assert store.reload() is True
    assert store.engine_config().min_gap_sec == 60
    assert store.rooms() == [7]


def test_reload_returns_false_when_unchanged(cfg_path):
    store = ConfigStore(cfg_path)
    assert store.reload() is False


def test_reload_keeps_snapshot_on_bad_edit(cfg_path):
    store = ConfigStore(cfg_path)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("{ broken")
    assert store.reload() is False
    # 旧配置仍然生效
    assert store.engine_config().min_gap_sec == 30


def test_error_listener_notified_on_bad_edit(cfg_path):
    store = ConfigStore(cfg_path)
    seen: list[Exception] = []
    store.on_error(seen.append)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("{ broken")
    store.reload()
    assert len(seen) == 1
    assert isinstance(seen[0], ConfigError)


def test_reload_listener_receives_old_and_new(cfg_path):
    store = ConfigStore(cfg_path)
    seen: list[tuple[dict, dict]] = []
    store.on_reload(lambda old, new: seen.append((old, new)))
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [9]}, fh)
    store.reload()
    assert len(seen) == 1
    old, new = seen[0]
    assert old["rooms"] == [1, 2]
    assert new["rooms"] == [9]


def test_maybe_reload_respects_poll_interval(cfg_path):
    store = ConfigStore(cfg_path, poll_sec=60)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [42]}, fh)
    # 轮询窗口内不检查磁盘
    assert store.maybe_reload() is False
    assert store.rooms() == [1, 2]


def test_maybe_reload_detects_change_after_window(cfg_path):
    store = ConfigStore(cfg_path, poll_sec=0)
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"rooms": [42]}, fh)
    time.sleep(0.01)
    assert store.maybe_reload() is True
    assert store.rooms() == [42]


def test_maybe_reload_survives_deleted_file(cfg_path):
    store = ConfigStore(cfg_path, poll_sec=0)
    os.remove(cfg_path)
    assert store.maybe_reload() is False
    assert store.rooms() == [1, 2]


# ---------------------------------------------------------------- writing


def test_save_writes_and_reloads(tmp_path):
    path = str(tmp_path / "c.json")
    store = ConfigStore(path, autoload=False)
    store.save({"rooms": [5], "engine": {"min_gap_sec": 8}})
    with open(path, "r", encoding="utf-8") as fh:
        assert json.load(fh)["rooms"] == [5]
    assert ConfigStore(path).engine_config().min_gap_sec == 8


def test_save_uses_atomic_replace(tmp_path):
    path = str(tmp_path / "c.json")
    store = ConfigStore(path, autoload=False)
    store.save({"rooms": [1]})
    assert not os.path.exists(path + ".tmp")


def test_engine_config_returns_real_dataclass(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    assert isinstance(ConfigStore(str(path)).engine_config(), EngineConfig)
