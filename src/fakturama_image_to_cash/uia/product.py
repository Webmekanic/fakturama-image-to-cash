import time
from decimal import Decimal

from pywinauto.keyboard import send_keys

from .exceptions import ManualReviewRequired
from .ui_helpers import _results_pane, _select_combo_option, _type_into, _wait_for_grid_to_settle
from .vision import _ask_vision_column_positions, _ask_vision_match, _crop_row_with_header


def _price_mode_combo(window):
    """The Net/Gross price-mode selector has no associated label; it is the only
    ComboBox in the header row above the Cust.Ref./VAT row."""
    candidates = [combo for combo in window.descendants(control_type="ComboBox") if combo.rectangle().top < 250]
    return candidates[0]


def _product_dialog(window, timeout: int = 10):
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
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    grid_area = items_label.parent().parent()
    _wait_for_grid_to_settle(grid_area)
    match = _ask_vision_match(grid_area.capture_as_image(), sku, column="Item No.", client=client)
    if match["exact_match_count"] != 1:
        raise ManualReviewRequired(f"product {sku!r} not found on the Order line after selection: {match}")


def resolve_product(window, sku: str, client=None) -> str:
    dialog = _product_dialog(window)
    search_edit = dialog.descendants(control_type="Edit")[0]
    search_edit.set_focus()
    search_edit.type_keys(sku, with_spaces=True)
    time.sleep(1.0)

    if not window.child_window(title="Select a product", control_type="Window").exists(timeout=1):
        _verify_sku_on_order(window, sku, client=client)
        return "selected"

    search_edit = dialog.descendants(control_type="Edit")[0]
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
    """Price (gross) sits one row above cost price (net); a tight row tolerance is
    needed so the two adjacent unlabeled Edits aren't confused."""
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

    Unlike the custom-painted grids, this VAT ComboBox is a real SWT Combo whose
    options are readable via ExpandCollapse + ListItem children, so no vision call is
    needed here. Only "Free of Tax" exists by default.
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
    # Closing and reopening it gives a fresh combo - safe here because no product
    # fields have been typed into it yet at this point.
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


def set_line_values(window, sku: str, quantity: str, unit_price: str, discount_percent: str, client=None):
    """Set Qty/U.Price/Discount on the Order line matching sku.

    The Items grid has zero UIA cell elements even with a real line present, so cell
    positions come from vision reads of the grid image. Row-finding and column-reading
    are two separate calls: asking one call to do both from the full multi-row grid
    produced inconsistent column positions for a row other than the first (confirmed
    live on a real two-line order) - locating the row first, then reading columns from
    just that row's own header+row crop, fixed it. Each cell edit is click + F2 +
    select-all + type + Enter.
    """
    items_label = [t for t in window.descendants(control_type="Text") if t.window_text() == "Items"][0]
    grid_area = items_label.parent().parent()
    grid_rect = grid_area.rectangle()
    # "Total" (the VAT-inclusive grand total) is always present regardless of price
    # mode; the field above it is labeled "Total Gross" in Gross mode but "Total Net"
    # in Net mode.
    total_field = window.child_window(title="Total", control_type="Edit").wrapper_object()
    before = total_field.get_value()

    # grid_area spans from well above the Items table down through the grid - the
    # "Items" label's own measured position is the only UIA-grounded anchor for where
    # the real column-header row starts.
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

    for column, value in (("Qty.", quantity), ("U.Price", unit_price), ("Discount", discount_percent)):
        x = int(columns[column] * rect.width())
        grid_area.click_input(coords=(x, row_y))
        send_keys("{F2}")
        send_keys("^a")
        send_keys("{DELETE}")
        send_keys(value)
        send_keys("{ENTER}")
        # Editing a second line's cells right after the first with no wait
        # intermittently only applies Qty and silently drops U.Price/Discount - the
        # next click races ahead of Fakturama committing the previous cell's edit.
        # Polling until the grid stops visually changing is a real completion signal.
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
