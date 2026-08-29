import base64
import io
import json
import time
from datetime import datetime
from decimal import Decimal

from pywinauto import Application

FAKTURAMA_PATH = r"C:\Program Files\Fakturama2\Fakturama.exe"
MAIN_WINDOW_CLASS = "SWT_Window0"
NEW_ORDER_BUTTON_NAME = "Create: New Order"
VISION_MODEL = "claude-opus-5"

_MATCH_PROMPT = """This image is a cropped search-results table from an accounting app, \
filtered by {column} "{value}".

Respond with ONLY a JSON object of this shape:
{{"row_count": <int>, "exact_match_count": <int>, "exact_match_y_fraction": <float 0.0-1.0 or null>}}

row_count = number of data rows visible (excluding the header row).
exact_match_count = number of rows whose {column} column exactly equals "{value}" \
(ignoring case and surrounding whitespace).
exact_match_y_fraction = if exact_match_count is exactly 1, that row's vertical center \
as a fraction of the image height (0.0 = top, 1.0 = bottom); otherwise null."""


class ManualReviewRequired(Exception):
    """Raised when a resolution step cannot proceed safely without human review."""


def connect_main_window(path: str = FAKTURAMA_PATH, timeout: int = 30):
    """Attach to the running Fakturama process and return its main window via UI Automation.

    Waits for the window to exist before restoring it: a cold Fakturama start can take
    a while to produce any window at all. Restore only runs if actually minimized -
    Fakturama can start minimized (which UIA correctly reports as not visible), but
    calling restore() on an already-normal window raises a COMError.
    """
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


def _sibling_edit(window, label_text: str):
    """Find the Edit control immediately right of a named Text label on the same row.

    Used for header fields (e.g. Date) that expose no accessible name of their own.
    """
    label = window.child_window(title=label_text, control_type="Text").wrapper_object()
    label_rect = label.rectangle()
    candidates = [
        edit
        for edit in window.descendants(control_type="Edit")
        if abs(edit.rectangle().top - label_rect.top) < 30 and edit.rectangle().left > label_rect.left
    ]
    return min(candidates, key=lambda edit: edit.rectangle().left)


def _price_mode_combo(window):
    """The Net/Gross price-mode selector has no associated label; it is the only
    ComboBox in the header row above the Cust.Ref./VAT row."""
    candidates = [combo for combo in window.descendants(control_type="ComboBox") if combo.rectangle().top < 250]
    return candidates[0]


def set_order_header(window, order_date: str, external_reference: str):
    """Set the New Order editor's Date, Cust.Ref., and price mode (Net) fields.

    Date is entered via keyboard simulation into a segmented M/D/Y picker rather than
    ValuePattern.SetValue: SetValue only changes the displayed text - Fakturama's
    underlying document model silently reverts it as soon as another field is touched.
    """
    date_edit = _sibling_edit(window, "Date")
    date_edit.set_focus()
    date_edit.type_keys("{HOME}")
    date_edit.type_keys(datetime.strptime(order_date, "%Y-%m-%d").strftime("%m%d%Y"))
    date_edit.type_keys("{TAB}")

    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    cust_ref_edit.iface_value.SetValue(external_reference)

    _price_mode_combo(window).iface_value.SetValue("Net")


_SEND_KEYS_SPECIAL = "+^%~(){}"


def _type_into(edit, text: str):
    """Type text via real keystrokes. ValuePattern.SetValue does not reliably persist
    in this app (confirmed on the Debtor Company field), so avoid it here.

    send_keys treats +^%~(){} as modifiers/groups, so literal occurrences (e.g. "VAT 19%")
    must be escaped or they get silently swallowed.
    """
    escaped = "".join(f"{{{c}}}" if c in _SEND_KEYS_SPECIAL else c for c in text)
    edit.set_focus()
    edit.type_keys("^a")
    edit.type_keys("{DELETE}")
    edit.type_keys(escaped, with_spaces=True)


def _address_dialog(window, timeout: int = 10):
    """Click the upper existing-contact icon beside Addresses and return the resulting dialog.

    The icon is a bare Image control with no name or invoke pattern; a live-resolved
    click is the only way to activate it.
    """
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


def _results_pane(dialog, search_edit):
    """The results grid has no name; it is the tallest Pane below the search box."""
    search_bottom = search_edit.rectangle().bottom
    candidates = [p for p in dialog.descendants(control_type="Pane") if p.rectangle().top >= search_bottom]
    return max(candidates, key=lambda p: p.rectangle().height())


def _wait_for_grid_to_settle(pane, timeout: float = 5.0, poll_interval: float = 0.25):
    """Poll rendered frames until two consecutive captures match.

    The results grid exposes zero UIA descendants beyond scrollbar chrome and
    supports no Grid/Table/Selection pattern (confirmed), so there is no UIA
    property signalling "the filtered search has finished rendering". Comparing
    frames is the only available completion signal.
    """
    deadline = time.time() + timeout
    previous = None
    while time.time() < deadline:
        current = pane.capture_as_image().tobytes()
        if current == previous:
            return
        previous = current
        time.sleep(poll_interval)


def _ask_vision_match(image, value: str, column: str = "Company", client=None) -> dict:
    """Ask a vision-capable LLM to read a results grid, since it exposes no rows via UIA."""
    import anthropic

    client = client or anthropic.Anthropic()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                    {"type": "text", "text": _MATCH_PROMPT.format(column=column, value=value)},
                ],
            }
        ],
    )
    text = next((block.text for block in response.content if block.type == "text"), "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def _address_preview_edit(pane):
    """The address preview has no name. On the Order editor it's the only blank Edit
    in this pane, but the Invoice editor also has blank-named Order Date/Service date
    Edits here - so pick the tallest (the address box is multi-line, ~88px vs ~17px)."""
    blank_edits = [e for e in pane.descendants(control_type="Edit") if not e.window_text()]
    return max(blank_edits, key=lambda e: e.rectangle().height())


def resolve_debtor(window, company_name: str, client=None) -> str:
    """Search the Order's address selector for company_name and act on the result.

    Returns "selected" (exact match chosen and linked to the Order) or "no_match".
    Raises ManualReviewRequired if more than one row is an exact match.
    """
    dialog = _address_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(company_name, with_spaces=True)
    time.sleep(1.0)  # Fakturama rebuilds part of the dialog's widget tree after filtering,
    search_edit = dialog.descendants(control_type="Edit")[0]  # invalidating the old reference
    results_pane = _results_pane(dialog, search_edit)
    _wait_for_grid_to_settle(results_pane)

    match = _ask_vision_match(results_pane.capture_as_image(), company_name, client=client)

    def click_button(name):
        btn = [b for b in dialog.descendants(control_type="Button") if b.window_text() == name][0]
        btn.set_focus()
        btn.click_input()

    if match["row_count"] == 0:
        click_button("Cancel")
        return "no_match"

    if match["exact_match_count"] != 1:
        click_button("Cancel")
        raise ManualReviewRequired(f"debtor search for {company_name!r} is ambiguous: {match}")

    # No row-level UIA element exists to invoke/select (confirmed: zero non-chrome
    # descendants under the results pane) - a click at the live-resolved pane's own
    # rectangle, offset by the vision model's row estimate, is the only way to pick it.
    rect = results_pane.rectangle()
    y_offset = int(match["exact_match_y_fraction"] * rect.height())
    x_offset = int(rect.width() * 0.3)
    results_pane.click_input(coords=(x_offset, y_offset))
    click_button("OK")

    # Verify the Order was actually linked, not just that the dialog closed.
    cust_ref_edit = window.child_window(title="Cust.Ref.", control_type="Edit").wrapper_object()
    preview = _address_preview_edit(cust_ref_edit.parent())
    if not preview.get_value().strip():
        raise ManualReviewRequired(f"selecting {company_name!r} did not populate the Order's address")

    return "selected"


def create_debtor(window, company: str, first_name: str, last_name: str, street: str):
    """Open a New Debtor editor (Order stays open), fill it, and save."""
    new_contact_btn = [
        b for b in window.descendants(control_type="SplitButton") if b.window_text() == "Create a new contact"
    ][0]
    new_contact_btn.invoke()

    company_spec = window.child_window(title="Company", control_type="Edit")
    company_spec.wait("exists", timeout=10)
    company_edit = company_spec.wrapper_object()
    scope = company_edit.parent()

    name_label = [t for t in scope.descendants(control_type="Text") if t.window_text() == "First Name Last Name"][0]
    label_top = name_label.rectangle().top
    name_edits = sorted(
        (e for e in scope.descendants(control_type="Edit") if abs(e.rectangle().top - label_top) < 15),
        key=lambda e: e.rectangle().left,
    )
    first_name_edit, last_name_edit = name_edits[0], name_edits[1]
    street_edit = [e for e in scope.descendants(control_type="Edit") if e.window_text() == "Street"][0]

    _type_into(company_edit, company)
    _type_into(first_name_edit, first_name)
    _type_into(last_name_edit, last_name)
    _type_into(street_edit, street)

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

    # Switching editors drops the Order's own controls from the UIA tree until its
    # tab is reactivated - return to it, per the task's "keep the Order tab open".
    # title_re handles both "New Order" and "*New Order" (Fakturama's unsaved-changes marker).
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
    outcome = resolve_debtor(window, debtor.company, client=client)
    if outcome == "selected":
        return "selected_existing"

    # Naive split on the first space; falls back to the company name if contact_name is blank.
    first_name, _, last_name = debtor.contact_name.partition(" ")
    create_debtor(window, debtor.company, first_name or debtor.company, last_name, debtor.billing_address)

    outcome = resolve_debtor(window, debtor.company, client=client)
    if outcome != "selected":
        raise ManualReviewRequired(f"newly created debtor {debtor.company!r} was not found on re-search")
    return "created_and_selected"


def _product_dialog(window, timeout: int = 10):
    """Click the upper product-selection icon beside Items and return the resulting dialog.

    Same bare-Image-with-no-pattern situation as the address icon; live-resolved click.
    """
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    scope = items_label.parent()
    upper_icon = min(scope.descendants(control_type="Image"), key=lambda img: img.rectangle().top)
    try:
        upper_icon.set_focus()
    except Exception:
        pass
    upper_icon.click_input()

    dialog_spec = window.child_window(title="Select a product", control_type="Window")
    dialog_spec.wait("exists", timeout=timeout)
    return dialog_spec.wrapper_object()


def resolve_product(window, sku: str, client=None) -> str:
    """Search the Order's product selector for sku and act on the result.

    Unlike "Select the address", "Select a product" auto-selects and closes itself the
    instant the search narrows to one exact match - confirmed live (Total Gross rose by
    exactly the matched product's price, no OK click involved). What first looked like
    the dialog crashing was this auto-select. So: dialog gone after typing -> selected,
    no vision needed. Dialog still open -> no match or ambiguous, same as resolve_debtor.
    """
    dialog = _product_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(sku, with_spaces=True)
    time.sleep(1.0)  # let the dialog either auto-close or finish laying out results

    if not window.child_window(title="Select a product", control_type="Window").exists(timeout=1):
        return "selected"

    search_edit = dialog.descendants(control_type="Edit")[0]  # tree rebuilt after filtering
    results_pane = _results_pane(dialog, search_edit)
    _wait_for_grid_to_settle(results_pane)

    match = _ask_vision_match(results_pane.capture_as_image(), sku, column="Item No.", client=client)

    def click_button(name):
        btn = [b for b in dialog.descendants(control_type="Button") if b.window_text() == name][0]
        btn.set_focus()
        btn.click_input()

    if match["row_count"] == 0:
        click_button("Cancel")
        return "no_match"

    if match["exact_match_count"] != 1:
        click_button("Cancel")
        raise ManualReviewRequired(f"product search for {sku!r} is ambiguous: {match}")

    rect = results_pane.rectangle()
    y_offset = int(match["exact_match_y_fraction"] * rect.height())
    x_offset = int(rect.width() * 0.3)
    results_pane.click_input(coords=(x_offset, y_offset))
    click_button("OK")
    return "selected"


def _price_gross_edit(scope):
    """Price (gross) sits one row above cost price (net); a tight row tolerance (unlike
    _sibling_edit's) is needed so the two adjacent unlabeled Edits aren't confused."""
    label = [t for t in scope.descendants(control_type="Text") if t.window_text() == "Price (gross)"][0]
    label_rect = label.rectangle()
    candidates = [
        e
        for e in scope.descendants(control_type="Edit")
        if abs(e.rectangle().top - label_rect.top) < 10 and e.rectangle().left > label_rect.left
    ]
    return min(candidates, key=lambda e: e.rectangle().left)


def resolve_or_create_vat(window, vat_percent: str):
    """Ensure "VAT {pct}%" exists in the open product editor's VAT dropdown.

    Checked directly via UIA: unlike the custom-painted grids, this VAT ComboBox is a
    real SWT Combo whose options are readable via ExpandCollapse + ListItem children -
    confirmed live, so no vision call is needed here. Only "Free of Tax" exists by
    default (confirmed live), so creation is genuinely required for any real VAT rate.
    """
    vat_name = f"VAT {vat_percent}%"
    vat_combo = window.child_window(title="VAT", control_type="ComboBox").wrapper_object()
    vat_combo.expand()
    options = [i.window_text() for i in vat_combo.descendants(control_type="ListItem")]
    vat_combo.collapse()
    if vat_name in options:
        vat_combo.iface_value.SetValue(vat_name)
        return

    vats_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "VATs"][0]
    vats_label.set_focus()
    vats_label.double_click_input()

    create_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Create a new tax rate"][
        0
    ]
    create_btn.invoke()

    name_edit = window.child_window(title="Name", control_type="Edit").wrapper_object()
    desc_edit = window.child_window(title="Description", control_type="Edit").wrapper_object()
    value_edit = window.child_window(title="Value", control_type="Edit").wrapper_object()
    _type_into(name_edit, vat_name)
    _type_into(desc_edit, vat_name)
    _type_into(value_edit, vat_percent)
    # VAT code (E-Invoice) already defaults to "S (Standard rate)" - leave it.

    save_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Save the current contents"][
        0
    ]
    try:
        save_btn.invoke()
    except Exception:
        save_btn.click_input()

    # Switching to the VATs list/editor drops the product editor's controls until its
    # tab is reactivated (same behaviour as the Order tab in create_debtor).
    product_tab = window.child_window(title_re=r"\*?New product", control_type="TabItem").wrapper_object()
    try:
        product_tab.set_focus()
    except Exception:
        pass
    product_tab.click_input()

    vat_combo = window.child_window(title="VAT", control_type="ComboBox").wrapper_object()
    vat_combo.expand()
    options = [i.window_text() for i in vat_combo.descendants(control_type="ListItem")]
    vat_combo.collapse()
    if vat_name not in options:
        raise ManualReviewRequired(f"created VAT rate {vat_name!r} did not appear in the product VAT dropdown")
    vat_combo.iface_value.SetValue(vat_name)


def create_product(window, sku: str, description: str, price_gross: str, vat_percent: str):
    """Open a New Product editor (Order stays open), resolve/create its VAT, fill, save."""
    new_product_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Create a new product"][
        0
    ]
    new_product_btn.invoke()

    item_no_spec = window.child_window(title="Item Number", control_type="Edit")
    item_no_spec.wait("exists", timeout=10)

    resolve_or_create_vat(window, vat_percent)

    item_no_edit = window.child_window(title="Item Number", control_type="Edit").wrapper_object()
    name_edit = window.child_window(title="Name", control_type="Edit").wrapper_object()
    desc_edit = window.child_window(title="Description", control_type="Edit").wrapper_object()
    scope = item_no_edit.parent()
    price_edit = _price_gross_edit(scope)

    _type_into(item_no_edit, sku)
    _type_into(name_edit, description)
    _type_into(desc_edit, description)
    _type_into(price_edit, price_gross)

    save_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Save the current contents"][
        0
    ]
    try:
        save_btn.invoke()
    except Exception:
        save_btn.click_input()

    order_tab = window.child_window(title_re=r"\*?New Order", control_type="TabItem").wrapper_object()
    try:
        order_tab.set_focus()
    except Exception:
        pass
    order_tab.click_input()


_CELL_POSITION_PROMPT = """This image shows a data grid's header row and its data rows \
from an accounting app. Find the data row whose "Item No." column exactly equals "{sku}".

Respond with ONLY a JSON object of this shape:
{{"row_y_fraction": <float 0.0-1.0>, "columns": {{"Qty.": <float>, "U.Price": <float>, "Discount": <float>}}}}

row_y_fraction = that row's vertical center as a fraction of the image height (0.0 = top, 1.0 = bottom).
For each listed column name, report the horizontal center of that column's cell in the \
matched row, as a fraction of the image width (0.0 = left edge, 1.0 = right edge), read \
from the header labels directly above."""


def _ask_vision_cell_positions(image, sku: str, client=None) -> dict:
    """Ask a vision-capable LLM for the target row/column positions - the Items grid
    exposes zero cell-level UIA elements even with a real line present (confirmed live)."""
    import anthropic

    client = client or anthropic.Anthropic()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=256,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                    {"type": "text", "text": _CELL_POSITION_PROMPT.format(sku=sku)},
                ],
            }
        ],
    )
    text = next((block.text for block in response.content if block.type == "text"), "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def set_line_values(window, sku: str, quantity: str, unit_price: str, discount_percent: str, client=None):
    """Set Qty/U.Price/Discount on the Order line matching sku.

    The Items grid (header and rows) has zero UIA cell elements even with a real line
    present (confirmed live), so cell positions come from a vision read of the same
    grid image, keyed to the header labels. Each cell edit is click + F2 + select-all +
    type + Enter - confirmed live against the real document model (Total Gross changed
    from $0.00 to the expected amount after a U.Price edit), not assumed.
    """
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    grid_area = items_label.parent().parent()
    total_gross = window.child_window(title="Total Gross", control_type="Edit").wrapper_object()
    before = total_gross.get_value()

    positions = _ask_vision_cell_positions(grid_area.capture_as_image(), sku, client=client)
    rect = grid_area.rectangle()
    row_y = int(positions["row_y_fraction"] * rect.height())

    from pywinauto.keyboard import send_keys

    for column, value in (("Qty.", quantity), ("U.Price", unit_price), ("Discount", discount_percent)):
        x = int(positions["columns"][column] * rect.width())
        grid_area.click_input(coords=(x, row_y))
        send_keys("{F2}")
        send_keys("^a")
        send_keys("{DELETE}")
        send_keys(value)
        send_keys("{ENTER}")

    after = total_gross.get_value()
    if after == before and Decimal(quantity) > 0 and Decimal(unit_price) > 0:
        raise ManualReviewRequired(f"setting line values for {sku!r} did not change Total Gross ({after})")


def resolve_or_create_product(window, line_item, client=None) -> str:
    """Select line_item.sku on the Order if it exists; otherwise create and select it,
    then set its Qty/U.Price/Discount. Mirrors resolve_or_create_debtor.

    line_item: an extraction.LineItem. Returns "selected_existing" or "created_and_selected".
    """
    outcome = resolve_product(window, line_item.sku, client=client)
    if outcome == "no_match":
        price_gross = (line_item.unit_net_price * (Decimal("1") + line_item.vat_percent / Decimal("100"))).quantize(
            Decimal("0.01")
        )
        create_product(window, line_item.sku, line_item.description, str(price_gross), str(line_item.vat_percent))
        outcome = resolve_product(window, line_item.sku, client=client)
        if outcome != "selected":
            raise ManualReviewRequired(f"newly created product {line_item.sku!r} was not found on re-search")
        result = "created_and_selected"
    else:
        result = "selected_existing"

    set_line_values(
        window,
        line_item.sku,
        str(line_item.quantity),
        str(line_item.unit_net_price),
        str(line_item.discount_percent),
        client=client,
    )
    return result


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
