"""Fakturama UI Automation: resolve/create Debtors and Products, drive the Order and
linked Invoice lifecycle, and verify the saved result - all via UI Automation, with a
vision-LLM fallback for the two custom-painted grids UIA can't read directly.

Split into focused modules by responsibility; re-exported here so existing callers
(`from fakturama_image_to_cash import uia`, `uia.<name>`) keep working unchanged.
"""

from .debtor import (
    create_debtor,
    resolve_debtor,
    resolve_or_create_debtor,
    resolve_or_create_payment_method,
)
from .exceptions import ManualReviewRequired
from .order_invoice import (
    FAKTURAMA_PATH,
    MAIN_WINDOW_CLASS,
    NEW_ORDER_BUTTON_NAME,
    apply_payment_status,
    connect_main_window,
    create_linked_invoice,
    open_new_order,
    save_invoice,
    save_order,
    set_order_header,
)
from .product import (
    create_product,
    resolve_or_create_product,
    resolve_or_create_vat,
    resolve_product,
    set_line_values,
)
from .verification import verify_final_records
from .vision import VISION_MODEL

__all__ = [
    "ManualReviewRequired",
    "FAKTURAMA_PATH",
    "MAIN_WINDOW_CLASS",
    "NEW_ORDER_BUTTON_NAME",
    "VISION_MODEL",
    "connect_main_window",
    "open_new_order",
    "set_order_header",
    "resolve_debtor",
    "create_debtor",
    "resolve_or_create_debtor",
    "resolve_or_create_payment_method",
    "resolve_product",
    "resolve_or_create_vat",
    "create_product",
    "set_line_values",
    "resolve_or_create_product",
    "save_order",
    "save_invoice",
    "create_linked_invoice",
    "apply_payment_status",
    "verify_final_records",
]
