from decimal import Decimal

from fakturama_image_to_cash.extraction import Debtor, LineItem, OrderData, validate_totals


def _line(sku, qty, unit_price, discount, vat, source_total):
    return LineItem(
        sku=sku,
        description=sku,
        quantity=Decimal(qty),
        unit_net_price=Decimal(unit_price),
        vat_percent=Decimal(vat),
        discount_percent=Decimal(discount),
        source_total=Decimal(source_total),
    )


def _order(line_items, net_total, vat_total, gross_total):
    return OrderData(
        order_date="2026-07-14",
        external_reference="WEB-2026-0714-A17",
        debtor=Debtor(
            company="Northstar Office GmbH",
            contact_name="Marta Klein",
            alias="NORTHSTAR-BERLIN",
            billing_address="Friedrichstrasse 88, 10117 Berlin",
            delivery_address="Beusselstrasse 44, 10553 Berlin",
            payment_method="Bank Transfer",
        ),
        line_items=line_items,
        paid_status="PAID",
        payment_date="2026-07-18",
        source_net_total=Decimal(net_total),
        source_vat_total=Decimal(vat_total),
        source_gross_total=Decimal(gross_total),
    )


def test_matching_totals_produce_no_mismatches():
    # Covers discounted and non-discounted line items summing correctly in one pass.
    line_items = [
        _line("CHR-ERG-01", "2", "250.00", "10", "19", "450.00"),
        _line("MAT-DESK-02", "3", "40.00", "0", "19", "120.00"),
    ]
    order = _order(line_items, "570.00", "108.30", "678.30")

    assert validate_totals(order) == []


def test_order_level_total_mismatch_is_reported():
    line_items = [
        _line("CHR-ERG-01", "2", "250.00", "10", "19", "450.00"),
        _line("MAT-DESK-02", "3", "40.00", "0", "19", "120.00"),
    ]
    order = _order(line_items, net_total="999.00", vat_total="108.30", gross_total="678.30")

    mismatches = validate_totals(order)

    assert len(mismatches) == 1
    assert "net total mismatch" in mismatches[0]
