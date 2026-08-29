import base64
import io
import json
import time
from datetime import datetime

from pywinauto import Application

FAKTURAMA_PATH = r"C:\Program Files\Fakturama2\Fakturama.exe"
MAIN_WINDOW_CLASS = "SWT_Window0"
NEW_ORDER_BUTTON_NAME = "Create: New Order"
VISION_MODEL = "claude-opus-5"

_MATCH_PROMPT = """This image is a cropped search-results table from an accounting app, \
filtered by company name "{company}".

Respond with ONLY a JSON object of this shape:
{{"row_count": <int>, "exact_match_count": <int>, "exact_match_y_fraction": <float 0.0-1.0 or null>}}

row_count = number of data rows visible (excluding the header row).
exact_match_count = number of rows whose Company column exactly equals "{company}" \
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


def _type_into(edit, text: str):
    """Type text via real keystrokes. ValuePattern.SetValue does not reliably persist
    in this app (confirmed on the Debtor Company field), so avoid it here."""
    edit.set_focus()
    edit.type_keys("^a{DELETE}", with_spaces=True)
    edit.type_keys(text, with_spaces=True)


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


def _ask_vision_match(image, company_name: str, client=None) -> dict:
    """Ask a vision-capable LLM to read the results grid, since it exposes no rows via UIA."""
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
                    {"type": "text", "text": _MATCH_PROMPT.format(company=company_name)},
                ],
            }
        ],
    )
    text = next((block.text for block in response.content if block.type == "text"), "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text)


def _address_preview_edit(pane):
    """The address preview has no name; it is the sole unnamed Edit in this pane
    (Cust.Ref. and Consultant are the only named Edits here)."""
    return [e for e in pane.descendants(control_type="Edit") if not e.window_text()][0]


def resolve_debtor(window, company_name: str, client=None) -> str:
    """Search the Order's address selector for company_name and act on the result.

    Returns "selected" (exact match chosen and linked to the Order) or "no_match".
    Raises ManualReviewRequired if more than one row is an exact match.
    """
    dialog = _address_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(company_name, with_spaces=True)
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
