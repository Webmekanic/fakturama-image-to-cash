import base64
import io
import json

from PIL import Image

from .exceptions import ManualReviewRequired

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

_COLUMN_POSITION_PROMPT = """This image stacks two bands cropped from a data grid: the \
header row on top, and exactly one data row below it (other rows have been removed).

Respond with ONLY a JSON object of this shape:
{"columns": {"Qty.": <float>, "U.Price": <float>, "Discount": <float>}}

For each listed column name, report the horizontal center of that column's cell in the \
data row (the bottom band), as a fraction of the image width (0.0 = left edge, 1.0 = \
right edge), using the header labels (the top band) to identify each column.

Do not include internal or system XML tags in your response."""


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

    for pos in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            return json.loads(text[pos:])
        except json.JSONDecodeError:
            continue

    raise ManualReviewRequired(f"vision response was not valid JSON: {text!r}")


def _ask_vision_match(image, value: str, column: str = "Company", client=None) -> dict:
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


def _ask_vision_debtor_match(image, debtor, client=None) -> dict:
    """Like _ask_vision_match, but requires Company/First Name/Name/ZIP/City to all
    match (per the spec's exact-match criteria for reusing an existing Debtor), not
    just Company."""
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


def _crop_row_with_header(
    image, header_top_fraction: float, row_y_fraction: float, header_height_fraction: float = 0.06, row_fraction: float = 0.06
):
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

    min_height = 300
    if combined.height < min_height:
        scale = min_height // combined.height + 1
        combined = combined.resize((combined.width, combined.height * scale), Image.LANCZOS)

    return combined


def _ask_vision_column_positions(image, client=None) -> dict:
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
