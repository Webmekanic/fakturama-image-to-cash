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


def _payment_method_combo(window, paid_cb):
    """The Invoice's payment-method combo has no accessible name; it sits immediately
    right of the "paid" checkbox on the same row - confirmed live. It is populated
    automatically from the Debtor's own Payment Method (set via
    resolve_or_create_payment_method), which is why apply_payment_status never needs
    to set it directly - only verify_final_records reads it back, to confirm spec
    5.2's "Invoice payment method equals the extracted Payment Method" as a fact
    rather than an assumption."""
    paid_rect = paid_cb.rectangle()
    candidates = [
        c
        for c in window.descendants(control_type="ComboBox")
        if abs(c.rectangle().top - paid_rect.top) < 15 and c.rectangle().left > paid_rect.right
    ]
    return min(candidates, key=lambda c: c.rectangle().left)


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

    That focus-race fix was not sufficient on its own: reproduced live on the new
    term-of-payment form, typing "Bank Transfer" into Name then Description left
    Name="Ba" and Description="nk TransferaBank Transfer" even with the focus poll in
    place. Win32 SendInput injects into the global input queue and Fakturama's own SWT
    event loop can still be draining a field's queued keystrokes when our code moves
    focus away - has_keyboard_focus() only reports the OS-level focus change, not
    whether the app has finished processing prior input. Waiting for this field's own
    get_value() to stop changing before returning closes that gap. This does not
    compare against the literal input text: numeric/currency fields reformat what was
    typed (confirmed live: typing "297.50" settles as "$297.50"), so a stability check
    is used instead of an exact-match check.
    """
    escaped = "".join(f"{{{c}}}" if c in _SEND_KEYS_SPECIAL else c for c in text)
    edit.set_focus()
    deadline = time.time() + 2.0
    while time.time() < deadline and not edit.has_keyboard_focus():
        time.sleep(0.02)
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


def _zip_city_edits(window):
    """The 'ZIP - City' row has two unlabeled Edit controls side by side: ZIP on the
    left, City on the right - confirmed live, no separate labels exist for either."""
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
    """Fill Street/ZIP/City/Country/E-Mail/Telephone on whichever address sub-tab
    (Main address, or an additional-address tab added via "+") is currently active.

    Confirmed live: only the active address sub-tab's fields exist in the UIA tree at
    any moment (switching tabs removes the inactive one's controls, the same behavior
    already documented for the Order vs. Debtor editor tabs elsewhere in this file), so
    no extra scoping beyond "make sure the right tab is already active" is needed.

    Country is a real named ComboBox, not a custom-painted grid - confirmed live, with
    251 options. It suffers the same ValuePattern/selected-text non-persistence bug as
    every other combo in this app (confirmed live: a freshly opened New Debtor editor's
    Country defaults to "United States" and nothing previously in this codebase ever
    changed it), so it is driven through the same proven _select_combo_option keyboard
    navigation as VAT and price mode, not a faster but unverified type-ahead shortcut.
    """
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
    """Open the active address tab's role picker and check Invoice/Delivery address.

    The unlabeled "address type" field has an adjacent button; clicking it opens a
    floating popup with real "Invoice address"/"Delivery address" CheckBox controls -
    confirmed live. Critically, this popup is transient and is dismissed by any
    window-activation change (confirmed live: calling SetForegroundWindow between
    opening it and clicking a checkbox closed it before the click could land) - so
    nothing here may re-foreground the app in between.

    The arrow button's click silently does nothing unless the adjacent Edit already
    has real keyboard focus - confirmed live via a direct before/after comparison:
    clicking the arrow cold (right after typing elsewhere) produced zero CheckBox
    controls even after polling for seconds, while calling set_focus() on the Edit
    immediately before the same click reliably opened the popup every time.

    On a freshly opened editor, this row also sits partially below the Documents/
    terms-of-payment panel divider (confirmed live: its measured rectangle extends
    ~19px past that boundary) - a click on a partially clipped control's reported
    rectangle can silently land on whatever is actually visible at that screen pixel
    (the panel below) instead of the control itself, so it is scrolled fully into
    view first via the same vertical-scrollbar "Line down" button, rather than
    guessing a fixed number of clicks.
    """
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

    # Scope everything below to the address-type row's own parent container, not the
    # whole window: with a New Order tab and a New Debtor tab both resident in the UIA
    # tree at once (confirmed live - inactive top-level tabs keep their controls, unlike
    # inactive Main-address-vs-additional-address sub-tabs within one editor, which do
    # not), a same-window-wide proximity search can coincidentally match some unrelated
    # control elsewhere that happens to share the same Y-coordinate, silently clicking
    # the wrong thing instead of the address-type arrow.
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

    # The very first click on this popup within a freshly opened editor reliably
    # produces zero CheckBox controls even after polling for a full second - confirmed
    # live, reproduced twice, on an editor that had never opened this popup before. An
    # immediate second click on the same still-focused arrow button then opens it
    # correctly every time (also confirmed live) - some part of Fakturama's own
    # popup Shell is evidently lazily constructed on first use.
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


_DEBTOR_MATCH_PROMPT = """This image is a cropped search-results table from an accounting \
app (columns typically include No., First Names, Names, Company, ZIP, City), filtered by \
company "{company}".

Respond with ONLY a JSON object of this shape:
{{"row_count": <int>, "exact_match_count": <int>, "exact_match_y_fraction": <float 0.0-1.0 or null>}}

row_count = number of rows that contain any visible text/data (excluding the header row).
Empty grid lines with no text in them do not count as rows - an empty results table is
row_count 0, even if faint row-separator lines extend down the pane.
exact_match_count = number of rows where ALL of the following match exactly (ignoring \
case and surrounding whitespace): Company = "{company}", First Names = "{first_name}", \
Names = "{last_name}", ZIP = "{zip_code}", City = "{city}". Treat an expected value of \
"" (empty) as matching any value in that column - only compare fields with a non-empty \
expected value.
exact_match_y_fraction = if exact_match_count is exactly 1, that row's vertical center \
as a fraction of the image height (0.0 = top, 1.0 = bottom); otherwise null."""


def _ask_vision_debtor_match(image, debtor, client=None) -> dict:
    """Like _ask_vision_match, but requires Company/First Name/Name/ZIP/City to all
    match (per the spec's exact-match criteria for reusing an existing Debtor), not
    just Company - reduces false-positive matches between same-named companies at
    different addresses or contacts."""
    import anthropic

    client = client or anthropic.Anthropic()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    image_data = base64.standard_b64encode(buf.getvalue()).decode("utf-8")

    first_name, _, last_name = debtor.contact_name.partition(" ")
    response = client.messages.create(
        model=VISION_MODEL,
        max_tokens=1024,
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_data}},
                    {
                        "type": "text",
                        "text": _DEBTOR_MATCH_PROMPT.format(
                            company=debtor.company,
                            first_name=first_name,
                            last_name=last_name,
                            zip_code=debtor.billing_address.zip_code,
                            city=debtor.billing_address.city,
                        ),
                    },
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


def resolve_debtor(window, debtor, client=None) -> str:
    """Search the Order's address selector for debtor.company and act on the result.

    A row is only treated as an exact match when Company, First Name, Name, ZIP, and
    City all agree with debtor (per the spec's exact-match criteria) - not Company
    alone, which could false-match a same-named company at a different address.

    Returns "selected" (exact match chosen and linked to the Order) or "no_match".
    Raises ManualReviewRequired if more than one row is an exact match.
    """
    dialog = _address_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(debtor.company, with_spaces=True)
    time.sleep(1.0)  # Fakturama rebuilds part of the dialog's widget tree after filtering,
    search_edit = dialog.descendants(control_type="Edit")[0]  # invalidating the old reference
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
        raise ManualReviewRequired(f"selecting {debtor.company!r} did not populate the Order's address")

    return "selected"


def resolve_or_create_payment_method(window, payment_method: str):
    """Ensure payment_method exists as a term of payment and select it on the open
    Debtor editor's Payment combo.

    Mirrors resolve_or_create_vat: the Debtor's own Payment ComboBox lists every term
    of payment by name via ExpandCollapse + ListItem children - confirmed live, same
    mechanism as the VAT combo, so no vision call is needed. Only "Pay Cash" exists by
    default (confirmed live).
    """
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
    # resource key as its accessible name - confirmed live, used here as-is since it's
    # still a stable, real selector. Option text carries a trailing space - confirmed
    # live (e.g. "Credit transfer ", not "Credit transfer").
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

    # Return to the Debtor editor and re-expand Payment - mirrors the already-proven
    # fix for the product editor's VAT combo caching stale options from before the new
    # row existed. Unlike the VAT case, the Debtor editor doesn't need to be closed and
    # reopened: it's a different tab entirely (not the tab that created the payment
    # method), so simply reactivating it and re-querying the combo is enough.
    debtor_tab = window.child_window(title_re=r"\*?New Debtor", control_type="TabItem").wrapper_object()
    try:
        debtor_tab.set_focus()
    except Exception:
        pass
    debtor_tab.click_input()

    payment_combo = window.child_window(title="Payment", control_type="ComboBox").wrapper_object()
    payment_combo.expand()
    options = [i.window_text() for i in payment_combo.descendants(control_type="ListItem")]
    payment_combo.collapse()
    if payment_method not in options:
        raise ManualReviewRequired(f"created payment method {payment_method!r} did not appear in the Payment dropdown")
    _select_combo_option(payment_combo, options, payment_method)


def create_debtor(window, debtor):
    """Open a New Debtor editor (Order stays open), fill it fully, and save.

    debtor: an extraction.Debtor. Fills Company/First/Last Name, the Main address
    (Street/ZIP/City/Country/E-Mail/Telephone) with the Invoice address role assigned,
    a second address tab with the Delivery address role when billing and delivery
    differ, Miscellaneous (Alias/Discount/Net-or-Gross), and the Payment Method.
    """
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

    # Naive split on the first space; falls back to the company name if contact_name is blank.
    first_name, _, last_name = debtor.contact_name.partition(" ")
    _type_into(company_edit, debtor.company)
    _type_into(first_name_edit, first_name or debtor.company)
    _type_into(last_name_edit, last_name)

    # Main address sub-tab is active by default on a freshly opened New Debtor editor.
    _fill_main_address(window, debtor.billing_address, debtor.email, debtor.telephone)

    same_address = debtor.billing_address == debtor.delivery_address
    _assign_address_role(window, invoice_address=True, delivery_address=same_address)

    if not same_address:
        # "+" adds a second address sub-tab (titled "additional address #N") with the
        # same field layout as Main address - confirmed live. Its own "address type"
        # popup assigns the Delivery address role independently of Main address's.
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

    resolve_or_create_payment_method(window, debtor.payment_method)

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
    outcome = resolve_debtor(window, debtor, client=client)
    if outcome == "selected":
        return "selected_existing"

    create_debtor(window, debtor)

    outcome = resolve_debtor(window, debtor, client=client)
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
    # The real preview text (confirmed live) is
    # "Company\r\nContact\r\nStreet\r\nDE-10115 Berlin\r\nCountry" - it prefixes ZIP
    # with a two-letter ISO country code we have no way to derive from the extracted
    # country name, so an exact reconstructed string can't be compared. Checking that
    # every real extracted field appears somewhere in the preview is robust to that
    # formatting detail while still verifying each individual piece of data landed.
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
