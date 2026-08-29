from .address import _address_preview_edit
from .exceptions import ManualReviewRequired
from .order_invoice import _PAID_STATUS_VALUES, _payment_method_combo
from .ui_helpers import _amounts_equal, _format_display_date, _sibling_edit


def verify_final_records(window, order_data, order_title: str, invoice_title: str):
    mismatches = []
    debtor = order_data.debtor
    expected_address_fields = [
        debtor.company,
        debtor.contact_name,
        debtor.billing_address.street,
        debtor.billing_address.zip_code,
        debtor.billing_address.city,
        debtor.billing_address.country,
    ]

    def _check_address(actual: str, label: str):
        missing = [field for field in expected_address_fields if field and field not in actual]
        if missing:
            mismatches.append(f"{label} address {actual!r} is missing expected fields: {missing}")

    expected_order_date = _format_display_date(order_data.order_date)
    expected_total = f"{order_data.source_gross_total:.2f}"

    if not window.child_window(title=order_title, control_type="TabItem").exists(timeout=5):
        raise ManualReviewRequired(f"expected Order tab {order_title!r} not found")
    order_tab = window.child_window(title=order_title, control_type="TabItem").wrapper_object()
    order_tab.set_focus()
    order_tab.click_input()

    order_cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    order_cust_ref = order_cust_ref_edit.get_value()
    order_date = _sibling_edit(window, "Date").get_value()
    order_address = _address_preview_edit(order_cust_ref_edit.parent()).get_value()
    order_total = window.child_window(title="Total", control_type="Edit").wrapper_object().get_value()

    if order_cust_ref != order_data.external_reference:
        mismatches.append(f"Order Cust.Ref. {order_cust_ref!r} != extracted {order_data.external_reference!r}")
    if order_date != expected_order_date:
        mismatches.append(f"Order Date {order_date!r} != extracted {expected_order_date!r}")
    _check_address(order_address, "Order")
    if not _amounts_equal(order_total, expected_total):
        mismatches.append(f"Order Total {order_total!r} != expected {expected_total!r}")

    if not window.child_window(title=invoice_title, control_type="TabItem").exists(timeout=5):
        raise ManualReviewRequired(f"expected Invoice tab {invoice_title!r} not found")
    invoice_tab = window.child_window(title=invoice_title, control_type="TabItem").wrapper_object()
    invoice_tab.set_focus()
    invoice_tab.click_input()

    invoice_cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    invoice_cust_ref = invoice_cust_ref_edit.get_value()
    invoice_order_date = _sibling_edit(window, "Order Date").get_value()
    invoice_address = _address_preview_edit(invoice_cust_ref_edit.parent()).get_value()
    invoice_total = window.child_window(title="Total", control_type="Edit").wrapper_object().get_value()

    if invoice_cust_ref != order_data.external_reference:
        mismatches.append(f"Invoice Cust.Ref. {invoice_cust_ref!r} != extracted {order_data.external_reference!r}")
    if invoice_order_date != expected_order_date:
        mismatches.append(f"Invoice Order Date {invoice_order_date!r} != extracted {expected_order_date!r}")
    _check_address(invoice_address, "Invoice")
    if not _amounts_equal(invoice_total, expected_total):
        mismatches.append(f"Invoice Total {invoice_total!r} != expected {expected_total!r}")

    paid_cb = window.child_window(title="paid", control_type="CheckBox").wrapper_object()
    is_paid = paid_cb.get_toggle_state() == 1
    expects_paid = order_data.paid_status.strip().lower() in _PAID_STATUS_VALUES

    payment_method_combo = _payment_method_combo(window, paid_cb)
    actual_payment_method = payment_method_combo.selected_text()
    if actual_payment_method != order_data.debtor.payment_method:
        mismatches.append(
            f"Invoice payment method {actual_payment_method!r} != expected {order_data.debtor.payment_method!r}"
        )

    if is_paid != expects_paid:
        mismatches.append(f"Invoice paid={is_paid} but extracted paid_status={order_data.paid_status!r}")
    elif expects_paid:
        value_edit = window.child_window(title="Value", control_type="Edit").wrapper_object()
        if not _amounts_equal(value_edit.get_value(), invoice_total):
            mismatches.append(f"Invoice Value {value_edit.get_value()!r} != Total {invoice_total!r}")
        date_edit = _sibling_edit(window, "at")
        expected_payment_date = _format_display_date(order_data.payment_date)
        if date_edit.get_value() != expected_payment_date:
            mismatches.append(f"Invoice payment date {date_edit.get_value()!r} != expected {expected_payment_date!r}")

    if mismatches:
        raise ManualReviewRequired("final verification failed: " + "; ".join(mismatches))
