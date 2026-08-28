import json

import pytest

from fakturama_image_to_cash.extraction import ExtractionError, extract_order, parse_order_payload


def _valid_payload():
    return {
        "order_date": "2026-07-14",
        "external_reference": "WEB-2026-0714-A17",
        "debtor": {
            "company": "Northstar Office GmbH",
            "contact_name": "Marta Klein",
            "alias": "NORTHSTAR-BERLIN",
            "billing_address": "Friedrichstrasse 88, 10117 Berlin",
            "delivery_address": "Beusselstrasse 44, 10553 Berlin",
            "payment_method": "Bank Transfer",
        },
        "line_items": [
            {
                "sku": "CHR-ERG-01",
                "description": "Ergonomic Desk Chair",
                "quantity": 2,
                "unit_net_price": 250.00,
                "vat_percent": 19,
                "discount_percent": 10,
                "source_total": 450.00,
            },
            {
                "sku": "MAT-DESK-02",
                "description": "Anti-Fatigue Desk Mat",
                "quantity": 3,
                "unit_net_price": 40.00,
                "vat_percent": 19,
                "discount_percent": 0,
                "source_total": 120.00,
            },
        ],
        "paid_status": "PAID",
        "payment_date": "2026-07-18",
        "source_net_total": 570.00,
        "source_vat_total": 108.30,
        "source_gross_total": 678.30,
    }


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, text):
        self._text = text

    def create(self, **kwargs):
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, text):
        self.messages = _FakeMessages(text)


# --- parse_order_payload: structural/content validation -------------------


def test_parse_order_payload_accepts_valid_response():
    order = parse_order_payload(_valid_payload())

    assert order.debtor.company == "Northstar Office GmbH"
    assert len(order.line_items) == 2


def test_parse_order_payload_rejects_missing_required_field():
    payload = _valid_payload()
    del payload["paid_status"]

    with pytest.raises(ExtractionError, match="missing required order fields"):
        parse_order_payload(payload)


def test_parse_order_payload_rejects_invalid_numeric_value():
    payload = _valid_payload()
    payload["line_items"][0]["unit_net_price"] = "not-a-number"

    with pytest.raises(ExtractionError, match="invalid numeric value"):
        parse_order_payload(payload)


def test_parse_order_payload_rejects_totals_mismatch():
    payload = _valid_payload()
    payload["source_net_total"] = 999.00

    with pytest.raises(ExtractionError, match="totals validation failed"):
        parse_order_payload(payload)


# --- extract_order: LLM call + response parsing ----------------------------


def test_extract_order_parses_valid_llm_response(tmp_path):
    image_path = tmp_path / "order.png"
    image_path.write_bytes(b"fake-image-bytes")
    client = _FakeClient(json.dumps(_valid_payload()))

    order = extract_order(str(image_path), client=client)

    assert order.external_reference == "WEB-2026-0714-A17"


def test_extract_order_handles_code_fenced_json(tmp_path):
    # Claude often wraps JSON in a markdown fence despite being told not to;
    # this is the realistic shape of a live response, not a hypothetical.
    image_path = tmp_path / "order.png"
    image_path.write_bytes(b"fake-image-bytes")
    fenced_text = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    client = _FakeClient(fenced_text)

    order = extract_order(str(image_path), client=client)

    assert order.debtor.company == "Northstar Office GmbH"


def test_extract_order_rejects_malformed_json(tmp_path):
    image_path = tmp_path / "order.png"
    image_path.write_bytes(b"fake-image-bytes")
    client = _FakeClient("this is not json")

    with pytest.raises(ExtractionError, match="not valid JSON"):
        extract_order(str(image_path), client=client)
