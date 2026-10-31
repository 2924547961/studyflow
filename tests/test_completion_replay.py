import importlib
import pytest
from chaoxing_learning_mcp import completion_replay as replay


def test_reservation_survives_module_reload_and_is_account_scoped(tmp_path,monkeypatch):
    monkeypatch.setattr(replay,'_root',tmp_path)
    assert replay.reserve_once('user-a','course','chapter','video')
    importlib.reload(replay);monkeypatch.setattr(replay,'_root',tmp_path)
    assert not replay.reserve_once('user-a','course','chapter','video')
    assert replay.reserve_once('user-b','course','chapter','video')
    assert replay.reserve_once('user-a','course','chapter','other-video')
    assert all(p.read_bytes()==b'1' for p in tmp_path.rglob('*.once'))
    assert not any('user' in str(p) for p in tmp_path.rglob('*.once'))


def test_used_single_video_chapter_cannot_be_replayed_after_upgrade(tmp_path,monkeypatch):
    monkeypatch.setattr(replay,'_root',tmp_path)
    assert replay.reserve_once('user','course','chapter','*')
    assert not replay.reserve_once('user','course','chapter','actual-video-fingerprint')
    assert replay.reserve_once('user','course','next-chapter','actual-video-fingerprint')


def test_missing_identity_never_creates_a_limit_file(tmp_path,monkeypatch):
    monkeypatch.setattr(replay,'_root',tmp_path)
    with pytest.raises(ValueError):replay.reserve_once('user','course','','video')
    assert not list(tmp_path.iterdir())
