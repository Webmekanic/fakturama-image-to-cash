import re
import time
from datetime import datetime
from decimal import Decimal

from pywinauto.keyboard import send_keys

_SEND_KEYS_SPECIAL = "+^%~(){}"


def _sibling_edit(window, label_text: str):
    label = window.child_window(title=label_text, control_type="Text").wrapper_object()
    label_rect = label.rectangle()
    candidates = [
        edit
        for edit in window.descendants(control_type="Edit")
        if abs(edit.rectangle().top - label_rect.top) < 30 and edit.rectangle().left > label_rect.left
    ]
    return min(candidates, key=lambda edit: edit.rectangle().left)


def _type_into(edit, text: str):
    """Type text via real keystrokes. ValuePattern.SetValue does not reliably persist
    values in this app, so real keystrokes are used everywhere instead.
    """
    escaped = "".join(f"{{{c}}}" if c in _SEND_KEYS_SPECIAL else c for c in text)
    edit.set_focus()
    deadline = time.time() + 2.0
    while time.time() < deadline and not edit.has_keyboard_focus():
        time.sleep(0.02)
    if edit.get_value():
        edit.type_keys("^a")
        edit.type_keys("{DELETE}")
    edit.type_keys(escaped, with_spaces=True)

    deadline = time.time() + 1.5
    previous = None
    while time.time() < deadline:
        current = edit.get_value()
        if current == previous:
            return
        previous = current
        time.sleep(0.05)


def _select_combo_option(combo, options: list[str], target: str):
    index = options.index(target)
    combo.set_focus()
    combo.click_input()
    send_keys("{UP}" * len(options))
    send_keys("{DOWN}" * index)
    send_keys("{ENTER}")


def _results_pane(dialog, search_edit):
    """The results grid has no name; it is the tallest Pane below the search box."""
    search_bottom = search_edit.rectangle().bottom
    candidates = [p for p in dialog.descendants(control_type="Pane") if p.rectangle().top >= search_bottom]
    return max(candidates, key=lambda p: p.rectangle().height())


def _wait_for_grid_to_settle(pane, timeout: float = 5.0, poll_interval: float = 0.25):
    """Poll rendered frames until two consecutive captures match.
    """
    deadline = time.time() + timeout
    previous = None
    while time.time() < deadline:
        current = pane.capture_as_image().tobytes()
        if current == previous:
            return
        previous = current
        time.sleep(poll_interval)


def _format_display_date(iso_date: str) -> str:
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _amounts_equal(a: str, b: str) -> bool:
    strip = lambda text: re.sub(r"[^0-9.]", "", text)
    return Decimal(strip(a)) == Decimal(strip(b))
