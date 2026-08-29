import base64
import io
import json
import re
import time
from datetime import datetime
from decimal import Decimal

from PIL import Image
from pywinauto import Application

FAKTURAMA_PATH = r"C:\Program Files\Fakturama2\Fakturama.exe"
MAIN_WINDOW_CLASS = "SWT_Window0"
NEW_ORDER_BUTTON_NAME = "Create: New Order"
VISION_MODEL = "claude-opus-5"

_MATCH_PROMPT = """This image is a cropped search-results table from an accounting app, \
filtered by {column} "{value}".

Respond with ONLY a JSON object of this shape:
{{"row_count": <int>, "exact_match_count": <int>, "exact_match_y_fraction": <float 0.0-1.0 or null>}}

row_count = number of rows that contain any visible text/data (excluding the header row).
Empty grid lines with no text in them do not count as rows - an empty results table is
row_count 0, even if faint row-separator lines extend down the pane.
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

    # SetValue on this combo suffers the same non-persistence bug confirmed on the
    # product VAT combo (see _select_combo_option): it reads back correctly right up
    # until save, then the saved Order silently reverts to "Gross" - which then treats
    # every net unit price as already including VAT, silently dropping VAT off every
    # line's total. Real keyboard navigation is required, not ValuePattern.SetValue.
    price_mode_combo = _price_mode_combo(window)
    price_mode_combo.expand()
    price_mode_options = [i.window_text() for i in price_mode_combo.descendants(control_type="ListItem")]
    price_mode_combo.collapse()
    _select_combo_option(price_mode_combo, price_mode_options, "Net")


_SEND_KEYS_SPECIAL = "+^%~(){}"


def _type_into(edit, text: str):
    """Type text via real keystrokes. ValuePattern.SetValue does not reliably persist
    in this app (confirmed on the Debtor Company field), so avoid it here.

    send_keys treats +^%~(){} as modifiers/groups, so literal occurrences (e.g. "VAT 19%")
    must be escaped or they get silently swallowed.

    set_focus() is asynchronous - confirmed live that has_keyboard_focus() reports
    False on *both* the old and new control immediately after calling it. Typing right
    away (as this used to) sent keystrokes during that in-between state: confirmed live
    that back-to-back _type_into calls on Name then Description corrupted Name into
    "VAT 19%aVAT 19%" - part of the second call's keystrokes landed on the first
    field before focus had actually finished moving. Polling for the real signal
    (has_keyboard_focus) before typing, rather than a fixed delay, fixed it.
    """
    escaped = "".join(f"{{{c}}}" if c in _SEND_KEYS_SPECIAL else c for c in text)
    edit.set_focus()
    deadline = time.time() + 2.0
    while time.time() < deadline and not edit.has_keyboard_focus():
        time.sleep(0.02)
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


def _parse_vision_json(text: str) -> dict:
    """Parse a vision response's JSON, tolerating a markdown fence, and failing with a
    clear ManualReviewRequired instead of leaking a raw JSONDecodeError up the stack."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # With thinking disabled, the model occasionally writes its reasoning as visible
    # prose before the JSON despite being asked not to (confirmed live). Try each "{"
    # from the end backward: the innermost nested brace (e.g. the "columns" object)
    # parses first but leaves trailing "}" as extra data and fails, so the next one
    # left of it - the true outer brace - is what actually succeeds.
    for pos in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            return json.loads(text[pos:])
        except json.JSONDecodeError:
            continue

    raise ManualReviewRequired(f"vision response was not valid JSON: {text!r}")


def _ask_vision_match(image, value: str, column: str = "Company", client=None) -> dict:
    """Ask a vision-capable LLM to read a results grid, since it exposes no rows via UIA."""
    import anthropic

    client = client or anthropic.Anthropic()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=1024,
        output_config={"effort": "low"},
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
    text = next((block.text for block in response.content if block.type == "text"), "")
    return _parse_vision_json(text)


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


def _verify_sku_on_order(window, sku: str, client=None):
    """Confirm the Items grid actually contains sku, rather than trusting that the
    product dialog closing means a correct auto-select happened.

    Necessary because "Select a product" can also close for reasons unrelated to a
    real match: confirmed live, repeatedly, across fresh Fakturama restarts, for
    several search terms with no product in the catalog (not limited to one specific
    character - the exact trigger was not fully isolated). resolve_product cannot
    otherwise distinguish that from a genuine auto-select, so the outcome is checked
    directly instead of inferred from why the dialog closed.
    """
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    grid_area = items_label.parent().parent()
    _wait_for_grid_to_settle(grid_area)
    match = _ask_vision_match(grid_area.capture_as_image(), sku, column="Item No.", client=client)
    if match["exact_match_count"] != 1:
        raise ManualReviewRequired(f"product {sku!r} not found on the Order line after selection: {match}")


def resolve_product(window, sku: str, client=None) -> str:
    """Search the Order's product selector for sku and act on the result.

    "Select a product" auto-selects and closes itself the instant the search narrows
    to one exact match - confirmed live (Total Gross rose by exactly the matched
    product's price, no OK click involved). But it can also close without a real
    match (see _verify_sku_on_order), so the dialog closing alone is never trusted as
    proof of a correct selection - the Items grid is checked afterward instead.
    Dialog still open -> no match or ambiguous, same as resolve_debtor.
    """
    dialog = _product_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(sku, with_spaces=True)
    time.sleep(1.0)  # let the dialog either auto-close or finish laying out results

    if not window.child_window(title="Select a product", control_type="Window").exists(timeout=1):
        _verify_sku_on_order(window, sku, client=client)
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


def _select_combo_option(combo, options: list[str], target: str):
    """Select target in combo via real keyboard navigation, not ValuePattern.SetValue.

    Confirmed live: SetValue (and even a real ListItem.select()) changes what
    selected_text() reports and survives right up until Save, but the saved product's
    VAT silently reverts to the default ("Free of Tax") - the same class of bug already
    documented for Edit fields, but for a ComboBox. Only keyboard navigation (focus,
    reset to the top, arrow down to the target index, Enter) actually persisted the
    selection to the saved document, confirmed by reopening the saved product.
    """
    from pywinauto.keyboard import send_keys

    index = options.index(target)
    combo.set_focus()
    combo.click_input()
    send_keys("{UP}" * len(options))
    send_keys("{DOWN}" * index)
    send_keys("{ENTER}")


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
        _select_combo_option(vat_combo, options, vat_name)
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

    # The still-open product editor's VAT combo caches its options at open time and
    # does not pick up a VAT rate created afterward, even after switching back to it
    # and waiting (confirmed live: the same stale options list came back every time).
    # Closing and reopening it gives a fresh combo populated from the database - safe
    # here because no product fields have been typed into it yet at this point.
    from pywinauto.keyboard import send_keys

    product_tab = window.child_window(title_re=r"\*?New product", control_type="TabItem").wrapper_object()
    try:
        product_tab.set_focus()
    except Exception:
        pass
    product_tab.click_input()
    send_keys("^w")

    new_product_btn = [b for b in window.descendants(control_type="Button") if b.window_text() == "Create a new product"][
        0
    ]
    new_product_btn.invoke()
    window.child_window(title="Item Number", control_type="Edit").wait("exists", timeout=10)

    vat_combo = window.child_window(title="VAT", control_type="ComboBox").wrapper_object()
    vat_combo.expand()
    options = [i.window_text() for i in vat_combo.descendants(control_type="ListItem")]
    vat_combo.collapse()
    if vat_name not in options:
        raise ManualReviewRequired(f"created VAT rate {vat_name!r} did not appear in the product VAT dropdown")
    _select_combo_option(vat_combo, options, vat_name)


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


_COLUMN_POSITION_PROMPT = """This image stacks two bands cropped from a data grid: the \
header row on top, and exactly one data row below it (other rows have been removed).

Respond with ONLY a JSON object of this shape:
{"columns": {"Qty.": <float>, "U.Price": <float>, "Discount": <float>}}

For each listed column name, report the horizontal center of that column's cell in the \
data row (the bottom band), as a fraction of the image width (0.0 = left edge, 1.0 = \
right edge), using the header labels (the top band) to identify each column.

Do not include internal or system XML tags in your response."""


def _crop_row_with_header(
    image, header_top_fraction: float, row_y_fraction: float, header_height_fraction: float = 0.06, row_fraction: float = 0.06
):
    """Composite the grid's header band and one target row's band into a single small
    image, so the column-position call sees only what it needs - not the other rows
    that (confirmed live, on a real two-line order) caused it to misread column
    positions when asked to also locate the row from the full multi-row grid.

    header_top_fraction must come from real UIA geometry (see set_line_values), not a
    guessed constant: grid_area spans from well above the Items table (No./Date/common
    data/Addresses) down through the grid, so a fixed "top of image" assumption grabbed
    the Order's own header fields instead of the Items table's header row - confirmed
    live by inspecting the actual composited crop.
    """
    width, height = image.size
    header_top = max(0, int(height * header_top_fraction))
    header_bottom = min(height, header_top + max(1, int(height * header_height_fraction)))
    header_band = image.crop((0, header_top, width, header_bottom))
    row_top = max(0, int(height * (row_y_fraction - row_fraction / 2)))
    row_bottom = min(height, int(height * (row_y_fraction + row_fraction / 2)))
    row_band = image.crop((0, row_top, width, max(row_top + 1, row_bottom)))
    combined = Image.new("RGB", (width, header_band.height + row_band.height), "white")
    combined.paste(header_band, (0, 0))
    combined.paste(row_band, (0, header_band.height))

    # The raw composite is only ~35-45px tall - confirmed live that reading column
    # X-positions from it is noisy (the same image, asked twice, gave different answers
    # and once picked the wrong column entirely). Upscaling to a minimum height made
    # repeated reads converge tightly (confirmed live: same image, 3 calls, <1% spread).
    min_height = 300
    if combined.height < min_height:
        scale = min_height // combined.height + 1
        combined = combined.resize((combined.width, combined.height * scale), Image.LANCZOS)

    return combined


def _ask_vision_column_positions(image, client=None) -> dict:
    """Ask a vision-capable LLM for column X positions from a header+row composite -
    a separate, simpler call from row-finding (see set_line_values)."""
    import anthropic

    client = client or anthropic.Anthropic()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=2048,
        thinking={"type": "disabled"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                    {"type": "text", "text": _COLUMN_POSITION_PROMPT},
                ],
            }
        ],
    )
    text = next((block.text for block in response.content if block.type == "text"), "")
    return _parse_vision_json(text)


def set_line_values(window, sku: str, quantity: str, unit_price: str, discount_percent: str, client=None):
    """Set Qty/U.Price/Discount on the Order line matching sku.

    The Items grid (header and rows) has zero UIA cell elements even with a real line
    present (confirmed live), so cell positions come from vision reads of the same
    grid image. Row-finding and column-reading are two separate calls: asking one call
    to do both from the full multi-row grid produced inconsistent column positions for
    a row other than the first (confirmed live on a real two-line order) - locating the
    row first (reusing the already-proven _ask_vision_match), then reading columns from
    just that row's own header+row crop, fixed it. Each cell edit is click + F2 +
    select-all + type + Enter - confirmed live against the real document model (Total
    Gross changed from $0.00 to the expected amount after a U.Price edit), not assumed.
    """
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    grid_area = items_label.parent().parent()
    grid_rect = grid_area.rectangle()
    # "Total" (the VAT-inclusive grand total) is always present regardless of price
    # mode; the field above it is labeled "Total Gross" in Gross mode but "Total Net"
    # in Net mode - confirmed live this broke a hardcoded "Total Gross" lookup the
    # instant the Order price-mode fix correctly kept the Order in Net mode.
    total_field = window.child_window(title="Total", control_type="Edit").wrapper_object()
    before = total_field.get_value()

    # grid_area spans from well above the Items table (No./Date/common data/Addresses)
    # down through the grid - the "Items" label's own measured position is the only
    # UIA-grounded anchor for where the real column-header row starts (confirmed live:
    # the header row sits on the same line as this label, not at the top of grid_area).
    header_top_fraction = (items_label.rectangle().top - grid_rect.top) / grid_rect.height()

    grid_image = grid_area.capture_as_image()
    row_match = _ask_vision_match(grid_image, sku, column="Item No.", client=client)
    if row_match.get("exact_match_count") != 1 or row_match.get("exact_match_y_fraction") is None:
        raise ManualReviewRequired(f"could not confidently locate {sku!r}'s row in the Items grid: {row_match}")
    row_y_fraction = row_match["exact_match_y_fraction"]

    row_image = _crop_row_with_header(grid_image, header_top_fraction, row_y_fraction)
    positions = _ask_vision_column_positions(row_image, client=client)
    columns = positions.get("columns", {})
    if not {"Qty.", "U.Price", "Discount"}.issubset(columns):
        raise ManualReviewRequired(f"could not confidently determine column positions for {sku!r}: {positions}")

    rect = grid_area.rectangle()
    row_y = int(row_y_fraction * rect.height())

    from pywinauto.keyboard import send_keys

    for column, value in (("Qty.", quantity), ("U.Price", unit_price), ("Discount", discount_percent)):
        x = int(columns[column] * rect.width())
        grid_area.click_input(coords=(x, row_y))
        send_keys("{F2}")
        send_keys("^a")
        send_keys("{DELETE}")
        send_keys(value)
        send_keys("{ENTER}")
        # Confirmed live: with no wait here, editing a second line's cells right after
        # the first (click -> F2 -> type -> Enter -> next click, back to back with no
        # gap) intermittently only applies Qty and silently drops U.Price/Discount -
        # the next click races ahead of Fakturama committing the previous cell's edit.
        # Polling until the grid stops visually changing (already used for dialogs
        # rendering search results) is a real completion signal, not a fixed delay.
        _wait_for_grid_to_settle(grid_area, timeout=2.0, poll_interval=0.1)

    after = total_field.get_value()
    if after == before and Decimal(quantity) > 0 and Decimal(unit_price) > 0:
        raise ManualReviewRequired(f"setting line values for {sku!r} did not change Total ({after})")


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


_PAID_STATUS_VALUES = {"paid"}
_UNPAID_STATUS_VALUES = {"", "unpaid", "open", "not paid"}


def _format_display_date(iso_date: str) -> str:
    """Fakturama displays dates like "Jul 18, 2026" - day is not zero-padded."""
    dt = datetime.strptime(iso_date, "%Y-%m-%d")
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}"


def _amounts_equal(a: str, b: str) -> bool:
    """Compare displayed amounts numerically (e.g. "$11.90" == "11.9") - a plain
    string compare after stripping currency symbols still fails on trailing zeros."""
    strip = lambda text: re.sub(r"[^0-9.]", "", text)
    return Decimal(strip(a)) == Decimal(strip(b))


def apply_payment_status(window, paid_status: str, payment_date: str | None = None):
    """Apply the extracted payment status to the open Invoice.

    "PAID" checks the paid checkbox (only if not already checked - safe to call on an
    already-paid Invoice), verifies Value matches Total, and sets the payment date via
    the same segmented-picker technique proven for the Order's own Date field. A
    recognized not-paid value leaves the checkbox alone. Anything else stops for review
    rather than guessing.
    """
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
    # Re-query fresh rather than trust the same reference used for the toggle - the
    # same defensive pattern already needed elsewhere (e.g. search_edit after a
    # dialog's widget tree rebuilds) for a wrapper object that may have gone stale.
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


def verify_final_records(window, order_data, order_title: str, invoice_title: str):
    """Verify the saved Order and Invoice against the originally extracted data.

    Reads only directly-readable editor fields - no vision. Milestone 6 already
    verified line items indirectly via Total Gross, and Milestone 7 already verified
    the Invoice inherited the Order's totals, so the custom-painted Items grid is not
    re-parsed here. Navigating Order -> Invoice also re-confirms payment persistence,
    the same "switch away and back" check used to validate it live during investigation.
    Collects every mismatch before raising, rather than stopping at the first one.
    """
    mismatches = []
    expected_address = "\r\n".join(
        [order_data.debtor.company, order_data.debtor.contact_name, order_data.debtor.billing_address]
    )
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
    if order_address != expected_address:
        mismatches.append(f"Order address {order_address!r} != expected {expected_address!r}")
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
    if invoice_address != expected_address:
        mismatches.append(f"Invoice address {invoice_address!r} != expected {expected_address!r}")
    if not _amounts_equal(invoice_total, expected_total):
        mismatches.append(f"Invoice Total {invoice_total!r} != expected {expected_total!r}")

    paid_cb = window.child_window(title="paid", control_type="CheckBox").wrapper_object()
    is_paid = paid_cb.get_toggle_state() == 1
    expects_paid = order_data.paid_status.strip().lower() in _PAID_STATUS_VALUES

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
