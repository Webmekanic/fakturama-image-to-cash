from dataclasses import dataclass
from decimal import Decimal

TOTALS_TOLERANCE = Decimal("0.01")


@dataclass
class LineItem:
    sku: str
    description: str
    quantity: Decimal
    unit_net_price: Decimal
    vat_percent: Decimal
    discount_percent: Decimal
    source_total: Decimal

    def computed_net_total(self) -> Decimal:
        return (
            self.quantity
            * self.unit_net_price
            * (Decimal("1") - self.discount_percent / Decimal("100"))
        )

    def computed_vat_amount(self) -> Decimal:
        return self.computed_net_total() * self.vat_percent / Decimal("100")


@dataclass
class Debtor:
    company: str
    contact_name: str
    alias: str
    billing_address: str
    delivery_address: str
    payment_method: str


@dataclass
class OrderData:
    order_date: str
    external_reference: str
    debtor: Debtor
    line_items: list[LineItem]
    paid_status: str
    payment_date: str | None
    source_net_total: Decimal
    source_vat_total: Decimal
    source_gross_total: Decimal


def validate_totals(order: OrderData, tolerance: Decimal = TOTALS_TOLERANCE) -> list[str]:
    mismatches = []

    for item in order.line_items:
        if abs(item.computed_net_total() - item.source_total) > tolerance:
            mismatches.append(
                f"line item {item.sku} total mismatch: "
                f"computed {item.computed_net_total()} vs source {item.source_total}"
            )

    computed_net_total = sum(
        (item.computed_net_total() for item in order.line_items), Decimal("0")
    )
    computed_vat_total = sum(
        (item.computed_vat_amount() for item in order.line_items), Decimal("0")
    )
    computed_gross_total = computed_net_total + computed_vat_total

    if abs(computed_net_total - order.source_net_total) > tolerance:
        mismatches.append(
            f"net total mismatch: computed {computed_net_total} vs source {order.source_net_total}"
        )
    if abs(computed_vat_total - order.source_vat_total) > tolerance:
        mismatches.append(
            f"vat total mismatch: computed {computed_vat_total} vs source {order.source_vat_total}"
        )
    if abs(computed_gross_total - order.source_gross_total) > tolerance:
        mismatches.append(
            f"gross total mismatch: computed {computed_gross_total} vs source {order.source_gross_total}"
        )

    return mismatches
