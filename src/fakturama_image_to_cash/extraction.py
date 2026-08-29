import base64
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

TOTALS_TOLERANCE = Decimal("0.01")
EXTRACTION_MODEL = "claude-opus-5"

_MEDIA_TYPES = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

_EXTRACTION_PROMPT = """You are extracting structured data from a sales order image.

Respond with ONLY a single JSON object (no markdown fences, no commentary) with exactly \
this shape:

{
  "order_date": "YYYY-MM-DD",
  "external_reference": string,
  "debtor": {
    "company": string,
    "contact_name": string,
    "alias": string,
    "email": string,
    "telephone": string,
    "billing_address": {"street": string, "zip_code": string, "city": string, "country": string},
    "delivery_address": {"street": string, "zip_code": string, "city": string, "country": string},
    "payment_method": string
  },
  "line_items": [
    {
      "sku": string,
      "description": string,
      "quantity": number,
      "unit_net_price": number,
      "vat_percent": number,
      "discount_percent": number,
      "source_total": number
    }
  ],
  "paid_status": string,
  "payment_date": "YYYY-MM-DD" or null,
  "source_net_total": number,
  "source_vat_total": number,
  "source_gross_total": number
}

Use the exact values shown in the image. Split each address into its street, zip_code, \
city, and country components. If billing and delivery addresses are identical, repeat \
the same address object in both fields. If a field is not present in the image, use an \
empty string (or null for payment_date)."""


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
class Address:
    street: str
    zip_code: str
    city: str
    country: str


@dataclass
class Debtor:
    company: str
    contact_name: str
    alias: str
    email: str
    telephone: str
    billing_address: Address
    delivery_address: Address
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


class ExtractionError(Exception):
    """Raised when extracted order data is malformed, incomplete, or fails totals validation."""


_REQUIRED_ORDER_FIELDS = {
    "order_date",
    "external_reference",
    "debtor",
    "line_items",
    "paid_status",
    "payment_date",
    "source_net_total",
    "source_vat_total",
    "source_gross_total",
}
_REQUIRED_DEBTOR_FIELDS = {
    "company",
    "contact_name",
    "alias",
    "email",
    "telephone",
    "billing_address",
    "delivery_address",
    "payment_method",
}
_REQUIRED_ADDRESS_FIELDS = {"street", "zip_code", "city", "country"}
_REQUIRED_LINE_ITEM_FIELDS = {
    "sku",
    "description",
    "quantity",
    "unit_net_price",
    "vat_percent",
    "discount_percent",
    "source_total",
}


def _require_nonempty(value: str, field_name: str) -> str:
    if not value.strip():
        raise ExtractionError(f"{field_name} must not be empty")
    return value


def _to_decimal(value, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ExtractionError(f"invalid numeric value for {field_name}: {value!r}") from None


def _parse_address(payload: dict, field_name: str) -> Address:
    if not isinstance(payload, dict):
        raise ExtractionError(f"{field_name} must be a JSON object")
    missing = _REQUIRED_ADDRESS_FIELDS - payload.keys()
    if missing:
        raise ExtractionError(f"{field_name} missing required fields: {sorted(missing)}")
    return Address(
        street=_require_nonempty(str(payload["street"]), f"{field_name}.street"),
        zip_code=str(payload["zip_code"]),
        city=str(payload["city"]),
        country=str(payload["country"]),
    )


def _parse_line_item(item: dict, index: int) -> LineItem:
    if not isinstance(item, dict):
        raise ExtractionError(f"line_items[{index}] must be a JSON object")
    missing = _REQUIRED_LINE_ITEM_FIELDS - item.keys()
    if missing:
        raise ExtractionError(f"line_items[{index}] missing required fields: {sorted(missing)}")

    quantity = _to_decimal(item["quantity"], f"line_items[{index}].quantity")
    if quantity <= 0:
        raise ExtractionError(f"line_items[{index}].quantity must be greater than 0, got {quantity}")

    unit_net_price = _to_decimal(item["unit_net_price"], f"line_items[{index}].unit_net_price")
    if unit_net_price < 0:
        raise ExtractionError(f"line_items[{index}].unit_net_price must not be negative, got {unit_net_price}")

    vat_percent = _to_decimal(item["vat_percent"], f"line_items[{index}].vat_percent")
    if not (0 <= vat_percent <= 100):
        raise ExtractionError(f"line_items[{index}].vat_percent must be between 0 and 100, got {vat_percent}")

    discount_percent = _to_decimal(item["discount_percent"], f"line_items[{index}].discount_percent")
    if not (0 <= discount_percent <= 100):
        raise ExtractionError(
            f"line_items[{index}].discount_percent must be between 0 and 100, got {discount_percent}"
        )

    source_total = _to_decimal(item["source_total"], f"line_items[{index}].source_total")
    if source_total < 0:
        raise ExtractionError(f"line_items[{index}].source_total must not be negative, got {source_total}")

    return LineItem(
        sku=_require_nonempty(str(item["sku"]), f"line_items[{index}].sku"),
        description=_require_nonempty(str(item["description"]), f"line_items[{index}].description"),
        quantity=quantity,
        unit_net_price=unit_net_price,
        vat_percent=vat_percent,
        discount_percent=discount_percent,
        source_total=source_total,
    )


def parse_order_payload(payload: dict) -> OrderData:
    if not isinstance(payload, dict):
        raise ExtractionError("extracted payload must be a JSON object")

    missing = _REQUIRED_ORDER_FIELDS - payload.keys()
    if missing:
        raise ExtractionError(f"missing required order fields: {sorted(missing)}")

    debtor_payload = payload["debtor"]
    if not isinstance(debtor_payload, dict):
        raise ExtractionError("debtor must be a JSON object")
    missing_debtor = _REQUIRED_DEBTOR_FIELDS - debtor_payload.keys()
    if missing_debtor:
        raise ExtractionError(f"missing required debtor fields: {sorted(missing_debtor)}")

    debtor = Debtor(
        company=_require_nonempty(str(debtor_payload["company"]), "debtor.company"),
        contact_name=str(debtor_payload["contact_name"]),
        alias=str(debtor_payload["alias"]),
        email=str(debtor_payload["email"]),
        telephone=str(debtor_payload["telephone"]),
        billing_address=_parse_address(debtor_payload["billing_address"], "debtor.billing_address"),
        delivery_address=_parse_address(debtor_payload["delivery_address"], "debtor.delivery_address"),
        payment_method=_require_nonempty(str(debtor_payload["payment_method"]), "debtor.payment_method"),
    )

    line_items_payload = payload["line_items"]
    if not isinstance(line_items_payload, list) or not line_items_payload:
        raise ExtractionError("line_items must be a non-empty JSON array")
    line_items = [_parse_line_item(item, index) for index, item in enumerate(line_items_payload)]

    source_net_total = _to_decimal(payload["source_net_total"], "source_net_total")
    source_vat_total = _to_decimal(payload["source_vat_total"], "source_vat_total")
    source_gross_total = _to_decimal(payload["source_gross_total"], "source_gross_total")

    order = OrderData(
        order_date=_require_nonempty(str(payload["order_date"]), "order_date"),
        external_reference=_require_nonempty(str(payload["external_reference"]), "external_reference"),
        debtor=debtor,
        line_items=line_items,
        paid_status=_require_nonempty(str(payload["paid_status"]), "paid_status"),
        payment_date=str(payload["payment_date"]) if payload["payment_date"] else None,
        source_net_total=source_net_total,
        source_vat_total=source_vat_total,
        source_gross_total=source_gross_total,
    )

    mismatches = validate_totals(order)
    if mismatches:
        raise ExtractionError(f"totals validation failed: {mismatches}")

    return order


def _parse_json_response(raw_text: str) -> dict:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"LLM response was not valid JSON: {exc}") from exc


def extract_order(image_path: str, client=None) -> OrderData:
    """Extract and validate order data from an order image using a vision-capable LLM."""
    import anthropic

    extension = image_path.rsplit(".", 1)[-1].lower()
    if extension not in _MEDIA_TYPES:
        raise ExtractionError(f"unsupported image type: .{extension}")
    media_type = _MEDIA_TYPES[extension]

    client = client or anthropic.Anthropic()

    with open(image_path, "rb") as f:
        image_data = base64.standard_b64encode(f.read()).decode("utf-8")

    response = client.messages.create(
        model=EXTRACTION_MODEL,
        max_tokens=4096,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": media_type, "data": image_data},
                    },
                    {"type": "text", "text": _EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    raw_text = next((block.text for block in response.content if block.type == "text"), "")
    payload = _parse_json_response(raw_text)
    return parse_order_payload(payload)
