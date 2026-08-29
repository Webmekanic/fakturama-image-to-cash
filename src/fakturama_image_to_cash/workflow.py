"""Orchestrates the full Order-image -> verified linked Invoice workflow.

Lives outside uia.py deliberately: it depends on both extraction.py and uia.py to
sequence one continuous run, so it doesn't belong inside either module without
blurring their existing single-responsibility boundaries (uia.py = Fakturama UI
Automation only; extraction.py = image parsing only).
"""

from . import uia
from .extraction import extract_order


def run_workflow(image_path: str, client=None):
    """Extract order_data from image_path and drive it through Fakturama end to end.

    Returns (order_title, invoice_title) - the saved document numbers - once every
    step has completed and verified without raising ManualReviewRequired.
    """
    order_data = extract_order(image_path, client=client)

    window = uia.connect_main_window()
    uia.open_new_order(window)
    uia.set_order_header(window, order_data.order_date, order_data.external_reference)
    uia.resolve_or_create_debtor(window, order_data.debtor, client=client)
    for line_item in order_data.line_items:
        uia.resolve_or_create_product(window, line_item, client=client)

    order_title = uia.save_order(window)
    uia.create_linked_invoice(window)
    uia.apply_payment_status(window, order_data.paid_status, order_data.payment_date)
    invoice_title = uia.save_invoice(window)

    uia.verify_final_records(window, order_data, order_title, invoice_title)
    return order_title, invoice_title
