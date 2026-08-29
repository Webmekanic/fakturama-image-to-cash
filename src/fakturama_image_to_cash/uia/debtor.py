import time

from .address import _address_dialog, _address_preview_edit, _assign_address_role, _fill_main_address
from .exceptions import ManualReviewRequired
from .ui_helpers import _results_pane, _select_combo_option, _type_into, _wait_for_grid_to_settle
from .vision import _ask_vision_debtor_match


def resolve_debtor(window, debtor, client=None) -> str:
    """Search the Order's address selector for debtor.company and act on the result.
    """
    dialog = _address_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(debtor.company, with_spaces=True)
    time.sleep(1.0)  # Fakturama rebuilds part of the dialog's widget tree after filtering,
    search_edit = dialog.descendants(control_type="Edit")[0] 
    results_pane = _results_pane(dialog, search_edit)
    _wait_for_grid_to_settle(results_pane)

    match = _ask_vision_debtor_match(results_pane.capture_as_image(), debtor, client=client)

    def click_button(name):
        btn = [b for b in dialog.descendants(control_type="Button") if b.window_text() == name][0]
        btn.set_focus()
        btn.click_input()

    if match["row_count"] == 0:
        click_button("Cancel")
        return "no_match"

    if match["exact_match_count"] != 1:
        click_button("Cancel")
        raise ManualReviewRequired(f"debtor search for {debtor.company!r} is ambiguous: {match}")

    # No row-level UIA element exists to invoke/select (zero non-chrome descendants
    # under the results pane) - a click at the live-resolved pane's own rectangle,
    # offset by the vision model's row estimate, is the only way to pick it.
    rect = results_pane.rectangle()
    y_offset = int(match["exact_match_y_fraction"] * rect.height())
    x_offset = int(rect.width() * 0.3)
    results_pane.click_input(coords=(x_offset, y_offset))
    click_button("OK")
    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    preview = _address_preview_edit(cust_ref_edit.parent())
    if not preview.get_value().strip():
        raise ManualReviewRequired(f"selecting {debtor.company!r} did not populate the Order's address")

    return "selected"


def _discard_and_reopen_debtor_editor(window):
    """Close the still-empty New Debtor tab and reopen a fresh one via "Create a new
    contact" - only safe to call before any Debtor fields have been filled in yet.
    """
    debtor_tab = window.child_window(title_re=r"\*?New Debtor", control_type="TabItem").wrapper_object()
    debtor_tab.click_input()
    tab_rect = debtor_tab.rectangle()
    win_rect = window.rectangle()
    window.wrapper_object().click_input(
        coords=(tab_rect.right - win_rect.left - 10, tab_rect.top - win_rect.top + tab_rect.height() // 2)
    )

    save_parts = window.child_window(title="Save Parts", control_type="Window")
    if save_parts.exists(timeout=2):
        dw = save_parts.wrapper_object()
        for item in dw.descendants(control_type="ListItem"):
            item_rect = item.rectangle()
            item.click_input(coords=(5, item_rect.height() // 2))
        ok_btn = [b for b in dw.descendants(control_type="Button") if b.window_text() == "OK"][0]
        ok_btn.click_input()

    new_contact_btn = [
        b for b in window.descendants(control_type="SplitButton") if b.window_text() == "Create a new contact"
    ][0]
    new_contact_btn.invoke()
    window.child_window(title="Company", control_type="Edit").wait("exists", timeout=10)


def resolve_or_create_payment_method(window, payment_method: str):
    """Ensure payment_method exists as a term of payment and select it on the open
    Debtor editor's Payment combo.
    """
    misc_tab = window.child_window(title="Miscellaneous", control_type="TabItem")
    if misc_tab.exists(timeout=1):
        misc_tab.wrapper_object().click_input()

    payment_combo = window.child_window(title="Payment", control_type="ComboBox").wrapper_object()
    payment_combo.expand()
    options = [i.window_text() for i in payment_combo.descendants(control_type="ListItem")]
    payment_combo.collapse()
    if payment_method in options:
        _select_combo_option(payment_combo, options, payment_method)
        return

    # Data > terms of payment - same nav-label-double-click pattern as Data > VATs.
    terms_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "terms of payment"][0]
    terms_label.set_focus()
    terms_label.double_click_input()

    create_btn = [
        b for b in window.descendants(control_type="Button") if b.window_text() == "Create a new term of payment"
    ][0]
    create_btn.invoke()

    name_edit = window.child_window(title="Name", control_type="Edit").wrapper_object()
    desc_edit = window.child_window(title="Description", control_type="Edit").wrapper_object()
    _type_into(name_edit, payment_method)
    _type_into(desc_edit, payment_method)

    # Fakturama's payment-code combo is missing its i18n string and exposes the raw
    # resource key as its accessible name - still a stable, real selector. Option text
    # carries a trailing space (e.g. "Credit transfer ", not "Credit transfer").
    code_map = {
        "Bank Transfer": "Credit transfer ",
        "Credit Card": "Credit card ",
        "SEPA Direct Debit": "SEPA direct debit ",
    }
    code_target = code_map.get(payment_method)
    if code_target is None:
        raise ManualReviewRequired(f"no payment-code mapping is known for payment method {payment_method!r}")

    code_combo = window.child_window(title="!editorPaymentPaymentcode!", control_type="ComboBox").wrapper_object()
    code_combo.expand()
    code_options = [i.window_text() for i in code_combo.descendants(control_type="ListItem")]
    code_combo.collapse()
    if code_target not in code_options:
        raise ManualReviewRequired(f"payment code {code_target!r} not found in dropdown: {code_options}")
    _select_combo_option(code_combo, code_options, code_target)

    cash_discount = window.child_window(title="Cash discount", control_type="Edit").wrapper_object()
    discount_days = window.child_window(title="Discount Days", control_type="Edit").wrapper_object()
    net_days = window.child_window(title="Net Days", control_type="Edit").wrapper_object()
    _type_into(cash_discount, "0")
    _type_into(discount_days, "0")
    _type_into(net_days, "0")

    save_btn = [
        b for b in window.descendants(control_type="Button") if b.window_text() == "Save the current contents"
    ][0]
    try:
        save_btn.invoke()
    except Exception:
        save_btn.click_input()

    _discard_and_reopen_debtor_editor(window)

    misc_tab = window.child_window(title="Miscellaneous", control_type="TabItem")
    if misc_tab.exists(timeout=1):
        misc_tab.wrapper_object().click_input()

    payment_combo = window.child_window(title="Payment", control_type="ComboBox").wrapper_object()
    payment_combo.expand()
    options = [i.window_text() for i in payment_combo.descendants(control_type="ListItem")]
    payment_combo.collapse()
    if payment_method not in options:
        raise ManualReviewRequired(f"created payment method {payment_method!r} did not appear in the Payment dropdown")
    _select_combo_option(payment_combo, options, payment_method)


def create_debtor(window, debtor):
    new_contact_btn = [
        b for b in window.descendants(control_type="SplitButton") if b.window_text() == "Create a new contact"
    ][0]
    new_contact_btn.invoke()
    window.child_window(title="Company", control_type="Edit").wait("exists", timeout=10)

    resolve_or_create_payment_method(window, debtor.payment_method)

    addresses_tab = window.child_window(title="Addresses", control_type="TabItem")
    addresses_tab.wrapper_object().click_input()

    company_spec = window.child_window(title="Company", control_type="Edit")
    company_edit = company_spec.wrapper_object()
    scope = company_edit.parent()

    name_label = [t for t in scope.descendants(control_type="Text") if t.window_text() == "First Name Last Name"][0]
    label_top = name_label.rectangle().top
    name_edits = sorted(
        (e for e in scope.descendants(control_type="Edit") if abs(e.rectangle().top - label_top) < 15),
        key=lambda e: e.rectangle().left,
    )
    first_name_edit, last_name_edit = name_edits[0], name_edits[1]

    # Naive split on the first space; falls back to the company name if contact_name is blank.
    first_name, _, last_name = debtor.contact_name.partition(" ")
    _type_into(company_edit, debtor.company)
    _type_into(first_name_edit, first_name or debtor.company)
    _type_into(last_name_edit, last_name)

    _fill_main_address(window, debtor.billing_address, debtor.email, debtor.telephone)

    same_address = debtor.billing_address == debtor.delivery_address
    _assign_address_role(window, invoice_address=True, delivery_address=same_address)

    if not same_address:
        plus_btn = min(
            (b for b in window.descendants(control_type="Button") if b.window_text() == "+"),
            key=lambda b: b.rectangle().top,
        )
        plus_btn.click_input()
        window.child_window(title_re=r"additional address #\d+", control_type="TabItem").wait("exists", timeout=5)
        _fill_main_address(window, debtor.delivery_address)
        _assign_address_role(window, invoice_address=False, delivery_address=True)

    misc_tab = window.child_window(title="Miscellaneous", control_type="TabItem").wrapper_object()
    misc_tab.click_input()

    if debtor.alias:
        alias_edit = window.child_window(title="Alias name", control_type="Edit").wrapper_object()
        _type_into(alias_edit, debtor.alias)

    discount_edit = window.child_window(title="Discount", control_type="Edit").wrapper_object()
    _type_into(discount_edit, "0")

    net_gross_combo = window.child_window(title="Net or Gross", control_type="ComboBox").wrapper_object()
    net_gross_combo.expand()
    net_gross_options = [i.window_text() for i in net_gross_combo.descendants(control_type="ListItem")]
    net_gross_combo.collapse()
    _select_combo_option(net_gross_combo, net_gross_options, "Net")

    save_btn = [
        b for b in window.descendants(control_type="Button") if b.window_text() == "Save the current contents"
    ][0]
    try:
        save_btn.invoke()
    except Exception:
        save_btn.click_input()

    # Fakturama warns on a same name+street match against an existing contact.
    dup_dialog = window.child_window(title="Duplicate Contact", control_type="Window")
    if dup_dialog.exists(timeout=3):
        ok_btn = [
            b for b in dup_dialog.wrapper_object().descendants(control_type="Button") if b.window_text() == "OK"
        ][0]
        ok_btn.set_focus()
        ok_btn.click_input()

    # Switching editors drops the Order's own controls from the UIA tree until its tab
    # is reactivated - return to it, keeping the Order open. title_re handles both
    # "New Order" and "*New Order" (Fakturama's unsaved-changes marker).
    order_tab = window.child_window(title_re=r"\*?New Order", control_type="TabItem").wrapper_object()
    try:
        order_tab.set_focus()
    except Exception:
        pass
    order_tab.click_input()


def resolve_or_create_debtor(window, debtor, client=None) -> str:
    """Select debtor.company on the Order if it exists; otherwise create and select it.

    debtor: an extraction.Debtor. Returns "selected_existing" or "created_and_selected".
    Raises ManualReviewRequired on an ambiguous match, or if a freshly created debtor
    cannot be found again on re-search.
    """
    outcome = resolve_debtor(window, debtor, client=client)
    if outcome == "selected":
        return "selected_existing"

    create_debtor(window, debtor)

    outcome = resolve_debtor(window, debtor, client=client)
    if outcome != "selected":
        raise ManualReviewRequired(f"newly created debtor {debtor.company!r} was not found on re-search")
    return "created_and_selected"
