# Fakturama Image-to-Cash Automation

![Fakturama dashboard](screenshots/Fakturama-dashboard.png)

Turns a single order image into a saved, verified Order and linked Invoice inside
[Fakturama](https://www.fakturama.info/), resolving or creating the Debtor and Product
master records along the way, without hardcoded coordinates or a fixed UI layout.

See [`docs/design.md`](docs/design.md) for the original design document.

## Requirements

- **Windows**, with [Fakturama](https://www.fakturama.info/download/) installed at its
  default location (`C:\Program Files\Fakturama2\Fakturama.exe` - see
  `FAKTURAMA_PATH` in `src/fakturama_image_to_cash/uia/order_invoice.py` if yours differs).
- Python 3.14+ and [uv](https://docs.astral.sh/uv/).
- An Anthropic API key with access to `claude-opus-5` (vision-capable).

## Setup

```powershell
uv sync
```

Create a `.env` file in the project root

```
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_WORKSPACE_ID=wrkspc_...
```

**Start Fakturama and leave it running** before running the automation - it attaches to
the already-running application; it does not launch or manage the process itself.
Launch it from the Start Menu, or from PowerShell:

```powershell
Start-Process "C:\Program Files\Fakturama2\Fakturama.exe"
```

## Running

Run the test suite (extraction/parsing logic only, mocked LLM responses. No API key or
Fakturama needed):

```powershell
uv run pytest -v
```

Run the full workflow against the real, running Fakturama and a real order image:

```powershell
uv run --env-file .env fakturama-image-to-cash screenshots\01-source-order.png
```

If any step can't be confidently verified (an ambiguous match, a value that didn't
persist, totals that don't reconcile), the run stops and raises
`fakturama_image_to_cash.uia.ManualReviewRequired` with a description of what couldn't
be confirmed, rather than guessing or silently continuing.

## How It Works

```text
Order Image
    │
    ▼
Claude Vision Extraction
    │
    ▼
Validated OrderData
    │
    ▼
Open New Order
    │
    ├── Resolve/Create Debtor
    │      ├── Address (billing + delivery)
    │      ├── Invoice/Delivery Address Roles
    │      └── Payment Method
    │
    ├── Resolve/Create Products
    │      ├── VAT
    │      └── Line Values
    │
    ▼
Save Order
    │
    ▼
Create Linked Invoice
    │
    ▼
Apply Payment Status
    │
    ▼
Save Invoice
    │
    ▼
Final Verification
```

## Validation

The complete workflow has been executed successfully against a real Fakturama
installation using a real order image and real Claude vision calls, with no mocked UI
or vision interactions. The run completed from image extraction through Order
creation, linked Invoice creation, payment status, and final record verification.

The successful end-to-end path covered:

- Order extraction from the source image
- Existing Debtor resolution and new Debtor creation
- Billing and delivery address population
- Invoice/Delivery address role assignment
- Payment Method creation and selection
- Existing Product resolution and new Product creation
- VAT resolution/creation
- Multi-line item entry
- Order save and persistence
- Linked Invoice creation
- Paid status and payment date
- Payment value reconciliation against Invoice Total
- Final Order and Invoice verification

## Error Handling

- **Fail fast, never guess.** Any unverifiable step raises `ManualReviewRequired` and
  stops the run immediately.
- **Two failure layers.** `ExtractionError` catches bad data before Fakturama is
  touched; `ManualReviewRequired` covers everything after.
- **Ambiguous matches stop the run** rather than silently picking one.
- **Critical values are read back** (address roles, VAT, Country, Payment Method,
  totals) and verified after being set.
- **Errors are descriptive** - each one names the specific field or value expected vs.
  found.

## Known Limitations

- The search dialog has exhibited an intermittent close-without-match behavior during
  testing (not fully root-caused); the workflow detects the condition and stops safely
  rather than guessing.
- Address-role popup can no-op on its first click in a fresh editor; handled with a
  bounded retry.
- No Delivery/Correction/Dunning documents; single image per run, no batch mode.

## Written questions

### If you had 3 more hours, what would you do for this task?

1. Root-cause the two intermittent UI behaviors (search dialog closing early,
   address-role popup's first-click no-op) instead of the current safe workarounds.
2. Add a structured, per-step run report (screenshots plus what was
   resolved/created/flagged) instead of relying on console output.
3. Add batch processing to run over a directory of order images, with a
   pass/manual-review summary.
4. Add integration tests against a scripted UIA mock or recorded Fakturama session, so
   regressions don't need a live environment to catch.
5. Extend the address schema to capture additional name and district when the source
   image supplies them.
