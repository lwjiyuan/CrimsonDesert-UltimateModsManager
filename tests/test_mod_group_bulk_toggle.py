from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pytestqt")


class _Checkbox:
    def __init__(self, checked: bool):
        self.checked = checked

    def isChecked(self) -> bool:
        return self.checked


class _Card:
    def __init__(self, mod_id: int, checked: bool):
        self.mod_id = mod_id
        self._checkbox = _Checkbox(checked)
        self.pending = None

    def set_checked(self, checked: bool) -> None:
        self._checkbox.checked = checked

    def set_pending(self, pending) -> None:
        self.pending = pending


class _Group:
    def __init__(self, mod_ids: list[int]):
        self._mod_ids = mod_ids

    def get_mod_ids(self) -> list[int]:
        return self._mod_ids


class _Manager:
    def __init__(self):
        self.changes = []

    def set_enabled(self, mod_id: int, enabled: bool) -> None:
        self.changes.append((mod_id, enabled))


def test_group_action_label_tracks_group_state(qtbot, monkeypatch):
    from cdumm.gui.components import mod_card

    monkeypatch.setattr(mod_card, "tr", lambda key: key)
    group = mod_card.FolderGroup("Group", group_id=7)
    qtbot.addWidget(group)

    assert group._select_all_btn.isHidden()

    group.set_bulk_toggle_state(all_checked=False, has_cards=True)
    assert not group._select_all_btn.isHidden()
    assert group._select_all_btn.text() == "toggle.select_all"

    group.set_bulk_toggle_state(all_checked=True, has_cards=True)
    assert group._select_all_btn.text() == "toggle.deselect_all"

    group.set_bulk_toggle_state(all_checked=False, has_cards=False)
    assert group._select_all_btn.isHidden()


def test_group_action_emits_group_id(qtbot):
    from cdumm.gui.components.mod_card import FolderGroup

    group = FolderGroup("Group", group_id=7)
    qtbot.addWidget(group)
    received = []
    group.select_all_in_group.connect(received.append)

    group._on_select_all_clicked()

    assert received == [7]


def test_group_action_only_changes_target_group():
    from cdumm.gui.pages.mods_page import ModsPage

    target_cards = [_Card(1, False), _Card(2, True)]
    other_card = _Card(3, True)
    manager = _Manager()
    page = SimpleNamespace(
        _folder_groups={7: _Group([1, 2]), 8: _Group([3])},
        _mod_cards=[*target_cards, other_card],
        _mod_manager=manager,
        _applied_state={1: False, 2: False, 3: True},
        _pause_db_watcher=lambda: None,
        _resume_db_watcher=lambda: None,
        _update_stats=lambda: None,
        _sync_select_all=lambda: None,
        _sync_group_bulk_actions=lambda: None,
    )

    ModsPage._on_select_all_in_group(page, 7)
    assert all(card._checkbox.isChecked() for card in target_cards)
    assert other_card._checkbox.isChecked()
    assert manager.changes == [(1, True), (2, True)]

    ModsPage._on_select_all_in_group(page, 7)
    assert not any(card._checkbox.isChecked() for card in target_cards)
    assert other_card._checkbox.isChecked()
    assert manager.changes[-2:] == [(1, False), (2, False)]
