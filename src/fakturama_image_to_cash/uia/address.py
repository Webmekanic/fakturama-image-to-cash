import time

from .exceptions import ManualReviewRequired
from .ui_helpers import _select_combo_option, _type_into


def _address_dialog(window, timeout: int = 10):
    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    pane = cust_ref_edit.parent()
    upper_icon = min(pane.descendants(control_type="Image"), key=lambda img: img.rectangle().top)
    try:
        upper_icon.set_focus()
    except Exception:
        pass
    upper_icon.click_input()

    dialog_spec = window.child_window(title="Select the address", control_type="Window")
    dialog_spec.wait("exists", timeout=timeout)
    return dialog_spec.wrapper_object()


def _address_preview_edit(pane):
    blank_edits = [e for e in pane.descendants(control_type="Edit") if not e.window_text()]
    return max(blank_edits, key=lambda e: e.rectangle().height())


def _zip_city_edits(window):
    label = [t for t in window.descendants(control_type="Text") if t.window_text() == "ZIP - City"][0]
    label_rect = label.rectangle()
    candidates = sorted(
        (
            e
            for e in window.descendants(control_type="Edit")
            if abs(e.rectangle().top - label_rect.top) < 15 and not e.window_text()
        ),
        key=lambda e: e.rectangle().left,
    )
    return candidates[0], candidates[1]


def _fill_main_address(window, address, email: str = "", telephone: str = ""):
    street_edit = window.child_window(title="Street", control_type="Edit").wrapper_object()
    _type_into(street_edit, address.street)

    if email:
        email_edit = window.child_window(title="E-Mail", control_type="Edit").wrapper_object()
        _type_into(email_edit, email)
    if telephone:
        telephone_edit = window.child_window(title="Telephone", control_type="Edit").wrapper_object()
        _type_into(telephone_edit, telephone)

    zip_edit, city_edit = _zip_city_edits(window)
    if address.zip_code:
        _type_into(zip_edit, address.zip_code)
    if address.city:
        _type_into(city_edit, address.city)

    if address.country:
        country_combo = window.child_window(title="Country", control_type="ComboBox").wrapper_object()
        country_combo.expand()
        options = [i.window_text() for i in country_combo.descendants(control_type="ListItem")]
        country_combo.collapse()
        if address.country not in options:
            raise ManualReviewRequired(f"country {address.country!r} not found in the Country dropdown")
        _select_combo_option(country_combo, options, address.country)


def _assign_address_role(window, invoice_address: bool = False, delivery_address: bool = False):
    addr_type_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "address type"][0]

    docs_tab = window.child_window(title="Documents", control_type="TabItem")
    if docs_tab.exists(timeout=1):
        boundary = docs_tab.wrapper_object().rectangle().top
        line_down_btns = [b for b in window.descendants(control_type="Button") if b.window_text() == "Line down"]
        if line_down_btns:
            line_down = line_down_btns[0]
            for _ in range(10):
                if addr_type_label.rectangle().bottom <= boundary:
                    break
                line_down.click_input()
                time.sleep(0.05)
    scope = addr_type_label.parent()
    label_rect = addr_type_label.rectangle()
    addr_type_edit = [
        e for e in scope.descendants(control_type="Edit") if abs(e.rectangle().top - label_rect.top) < 10
    ][0]
    edit_rect = addr_type_edit.rectangle()
    arrow_btn = min(
        (
            d
            for d in scope.descendants()
            if abs(d.rectangle().top - edit_rect.top) < 10 and d.rectangle().left >= edit_rect.right
        ),
        key=lambda d: d.rectangle().left,
    )
    addr_type_edit.set_focus()
    arrow_btn.click_input()

    def _wait_for_checkbox(name: str, timeout: float = 1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = [c for c in window.descendants(control_type="CheckBox") if c.window_text() == name]
            if matches:
                return matches[0]
            time.sleep(0.05)
        return None

    # The first click on this popup within a freshly opened editor reliably produces
    # zero CheckBox controls even after polling for a full second; an immediate second
    # click on the same still-focused arrow button then opens it correctly every time -
    # some part of Fakturama's own popup Shell is evidently lazily constructed.
    probe_name = "Invoice address" if invoice_address else "Delivery address"
    if _wait_for_checkbox(probe_name) is None:
        arrow_btn.click_input()

    def _require_checkbox(name: str, timeout: float = 2.0):
        cb = _wait_for_checkbox(name, timeout=timeout)
        if cb is None:
            raise ManualReviewRequired(f"address role popup did not render a {name!r} checkbox")
        return cb

    expected_parts = []
    if invoice_address:
        expected_parts.append("Invoice address")
        cb = _require_checkbox("Invoice address")
        if cb.get_toggle_state() == 0:
            cb.set_focus()
            cb.click_input()
    if delivery_address:
        expected_parts.append("Delivery address")
        cb = _require_checkbox("Delivery address")
        if cb.get_toggle_state() == 0:
            cb.set_focus()
            cb.click_input()

    # Click elsewhere to close the popup and commit its selection to the field.
    window.child_window(title="Company", control_type="Edit").wrapper_object().click_input()

    value = addr_type_edit.get_value()
    if not all(part in value for part in expected_parts):
        raise ManualReviewRequired(f"address role assignment did not persist: expected {expected_parts}, got {value!r}")
