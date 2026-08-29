from datetime import datetime

from pywinauto import Application

from .address import _address_preview_edit
from .exceptions import ManualReviewRequired
from .product import _price_mode_combo
from .ui_helpers import _amounts_equal, _format_display_date, _select_combo_option, _sibling_edit

FAKTURAMA_PATH = r"C:\Program Files\Fakturama2\Fakturama.exe"
MAIN_WINDOW_CLASS = "SWT_Window0"
NEW_ORDER_BUTTON_NAME = "Create: New Order"

_PAID_STATUS_VALUES = {"paid"}
_UNPAID_STATUS_VALUES = {"", "unpaid", "open", "not paid"}


def connect_main_window(path: str = FAKTURAMA_PATH, timeout: int = 30):
    app = Application(backend="uia").connect(path=path)
    window = app.window(class_name=MAIN_WINDOW_CLASS, control_type="Window")
    window.wait("exists", timeout=timeout)
    if window.wrapper_object().is_minimized():
        window.restore()
    window.wait("visible ready", timeout=timeout)
    return window


def open_new_order(main_window, timeout: int = 10):
    """Invoke the top-toolbar New Order button and wait for its editor tab to appear."""
    button = main_window.child_window(
        title=NEW_ORDER_BUTTON_NAME, control_type="Button"
    ).wrapper_object()
    button.invoke()
    main_window.child_window(title="New Order", control_type="TabItem").wait("exists", timeout=timeout)


def set_order_header(window, order_date: str, external_reference: str):
    date_edit = _sibling_edit(window, "Date")
    date_edit.set_focus()
    date_edit.type_keys("{HOME}")
    date_edit.type_keys(datetime.strptime(order_date, "%Y-%m-%d").strftime("%m%d%Y"))
    date_edit.type_keys("{TAB}")

    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    cust_ref_edit.iface_value.SetValue(external_reference)

    # SetValue on this combo suffers the same non-persistence bug as the product VAT
    # combo: it reads back correctly right up until save, then the saved Order
    # silently reverts to "Gross" - which then treats every net unit price as already
    # including VAT, silently dropping VAT off every line's total.
    price_mode_combo = _price_mode_combo(window)
    price_mode_combo.expand()
    price_mode_options = [i.window_text() for i in price_mode_combo.descendants(control_type="ListItem")]
    price_mode_combo.collapse()
    _select_combo_option(price_mode_combo, price_mode_options, "Net")


def _save_and_verify_rename(window, timeout: int = 10) -> str:
    """Save the active editor and confirm its tab actually renamed to the document
    number, not just that Save was clicked. The No. field holds that number before
    saving too (auto-proposed on open), so the expected tab title is known in advance."""
    expected_title = _sibling_edit(window, "No.").get_value()

    save_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Save the current contents"][
        0
    ]
    try:
        save_btn.invoke()
    except Exception:
        save_btn.click_input()

    if not window.child_window(title=expected_title, control_type="TabItem").exists(timeout=timeout):
        raise ManualReviewRequired(f"document did not save - expected tab {expected_title!r} not found")
    return expected_title


def save_order(window, timeout: int = 10) -> str:
    """Save the Order and verify its tab renamed to the Order number."""
    return _save_and_verify_rename(window, timeout=timeout)


def save_invoice(window, timeout: int = 10) -> str:
    """Save the Invoice and verify its tab renamed to the Invoice number."""
    return _save_and_verify_rename(window, timeout=timeout)


def create_linked_invoice(window, timeout: int = 10):
    """Click the Order's own Invoice follow-up button (not the toolbar's global one)
    and verify the new Invoice actually copied the Order's data - this is what proves
    it is the correct linked Invoice, not merely that some Invoice tab opened.
    """
    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    order_cust_ref = cust_ref_edit.get_value()
    order_date = _sibling_edit(window, "Date").get_value()
    order_address = _address_preview_edit(cust_ref_edit.parent()).get_value()
    order_total = window.child_window(title="Total", control_type="Edit").wrapper_object().get_value()

    invoice_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Invoice"][0]
    invoice_btn.invoke()
    window.child_window(title_re=r"\*?New Invoice", control_type="TabItem").wait("exists", timeout=timeout)

    invoice_cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    invoice_cust_ref = invoice_cust_ref_edit.get_value()
    invoice_order_date = _sibling_edit(window, "Order Date").get_value()
    invoice_address = _address_preview_edit(invoice_cust_ref_edit.parent()).get_value()
    invoice_total = window.child_window(title="Total", control_type="Edit").wrapper_object().get_value()

    mismatches = []
    if invoice_cust_ref != order_cust_ref:
        mismatches.append(f"Cust.Ref. order={order_cust_ref!r} invoice={invoice_cust_ref!r}")
    if invoice_order_date != order_date:
        mismatches.append(f"Order Date order={order_date!r} invoice={invoice_order_date!r}")
    if invoice_address != order_address:
        mismatches.append(f"address order={order_address!r} invoice={invoice_address!r}")
    if invoice_total != order_total:
        mismatches.append(f"Total order={order_total!r} invoice={invoice_total!r}")

    if mismatches:
        raise ManualReviewRequired(f"linked Invoice does not match its Order: {'; '.join(mismatches)}")


def _payment_method_combo(window, paid_cb):
    paid_rect = paid_cb.rectangle()
    candidates = [
        c
        for c in window.descendants(control_type="ComboBox")
        if abs(c.rectangle().top - paid_rect.top) < 15 and c.rectangle().left > paid_rect.right
    ]
    return min(candidates, key=lambda c: c.rectangle().left)


def apply_payment_status(window, paid_status: str, payment_date: str | None = None):
    normalized = paid_status.strip().lower()
    paid_cb = window.child_window(title="paid", control_type="CheckBox").wrapper_object()

    if normalized in _UNPAID_STATUS_VALUES:
        if paid_cb.get_toggle_state() != 0:
            raise ManualReviewRequired(f"Invoice is already paid but extracted status is {paid_status!r}")
        return

    if normalized not in _PAID_STATUS_VALUES:
        raise ManualReviewRequired(f"unsupported payment status: {paid_status!r}")

    if not payment_date:
        raise ManualReviewRequired("payment status is PAID but no payment_date was extracted")

    if paid_cb.get_toggle_state() == 0:
        paid_cb.set_focus()
        paid_cb.toggle()
    # Re-query fresh rather than trust the reference used for the toggle - it may have
    # gone stale, the same defensive pattern used elsewhere in this package.
    paid_cb = window.child_window(title="paid", control_type="CheckBox").wrapper_object()
    if paid_cb.get_toggle_state() != 1:
        raise ManualReviewRequired("checking 'paid' did not update the Invoice's toggle state")

    total = window.child_window(title="Total", control_type="Edit").wrapper_object().get_value()
    value_edit = window.child_window(title="Value", control_type="Edit").wrapper_object()
    if not _amounts_equal(value_edit.get_value(), total):
        raise ManualReviewRequired(f"payment Value {value_edit.get_value()!r} does not match Invoice Total {total!r}")

    date_edit = _sibling_edit(window, "at")
    date_edit.set_focus()
    date_edit.type_keys("{HOME}")
    date_edit.type_keys(datetime.strptime(payment_date, "%Y-%m-%d").strftime("%m%d%Y"))
    date_edit.type_keys("{TAB}")

    expected_date = _format_display_date(payment_date)
    actual_date = date_edit.get_value()
    if actual_date != expected_date:
        raise ManualReviewRequired(f"payment date {actual_date!r} does not match requested {expected_date!r}")
