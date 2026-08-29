# Fakturama Image-to-Cash Automation

Turns a single order image into a saved, verified Order and linked Invoice inside
[Fakturama](https://www.fakturama.info/), resolving or creating the Debtor and Product
master records along the way, without hardcoded coordinates or a fixed UI layout.

See [`docs/design.md`](docs/design.md) for the original design document.

## Requirements

- **Windows**, with [Fakturama](https://www.fakturama.info/download/) installed at its
  default location (`C:\Program Files\Fakturama2\Fakturama.exe` - see
  `FAKTURAMA_PATH` in `src/fakturama_image_to_cash/uia.py` if yours differs).
- Python 3.14+ and [uv](https://docs.astral.sh/uv/).
- An Anthropic API key with access to `claude-opus-5` (vision-capable).

## Setup

```powershell
uv sync
```

Set your API key for the current session:

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

If your key is **identity-linked** (tied to a personal login rather than a workspace -
the API will reject requests with `anthropic-workspace-id is required...` if so), also
set:

```powershell
$env:ANTHROPIC_WORKSPACE_ID = "wrkspc_..."
```

A plain workspace-scoped API key (Console -> Settings -> API Keys -> Create Key, with a
specific workspace selected) doesn't need this second variable at all.

**Start Fakturama and leave it running** before running the automation - it attaches to
the already-running application; it does not launch or manage the process itself.

## Running

Run the test suite (extraction/parsing logic only, mocked LLM responses - no API key or
Fakturama needed):

```powershell
uv run pytest -v
```

Run the full workflow against the real, running Fakturama and a real order image:

```powershell
uv run fakturama-image-to-cash path\to\order-image.png
```

With no argument, it defaults to the bundled sample at `screenshots/01-source-order.png`.
On success it prints the saved Order and Invoice numbers, e.g.:

```
Order saved as PO000001, linked Invoice saved as INV000001
```

If any step can't be confidently verified (an ambiguous match, a value that didn't
persist, totals that don't reconcile), the run stops and raises
`fakturama_image_to_cash.uia.ManualReviewRequired` with a description of what couldn't
be confirmed, rather than guessing or silently continuing.

## Project layout

| File | Responsibility |
|---|---|
| `src/fakturama_image_to_cash/extraction.py` | Order image -> validated `OrderData` (one vision call to Claude Opus 5, schema + totals-recomputation validation) |
| `src/fakturama_image_to_cash/uia.py` | Every Fakturama interaction: UI Automation control discovery, the debtor/product/VAT/payment resolution and creation flows, and the vision fallback for the two custom-painted grids |
| `src/fakturama_image_to_cash/workflow.py` | Orchestrates the full Order-first sequence: extract -> New Order -> Debtor -> Products -> save Order -> linked Invoice -> payment status -> save Invoice -> final verification |
| `src/fakturama_image_to_cash/__init__.py` | CLI entry point (`fakturama-image-to-cash`) |

## Implementation approach

**Control discovery.** Every Fakturama control is found by UI Automation role and
accessible name - `window.child_window(title=..., control_type=...)` - re-queried at
each step rather than cached, and never by screen coordinate. Two real, confirmed-live
exceptions to "everything has a stable name":

- A handful of icon-only buttons (the existing-contact/existing-product selectors
  beside Addresses/Items) expose no accessible name. These are found by their *type and
  position relative to a named neighbor* (e.g. "the topmost `Image` control beside the
  `Addresses` label"), not a hardcoded coordinate - resolved fresh from the live tree on
  every call, so it tracks whatever the current layout actually is.
- `ComboBox.SetValue()` (WinAPI `ValuePattern`) does **not** persist to Fakturama's
  saved document - confirmed live, repeatedly: it updates what UI Automation reports as
  selected, right up until the moment of save, and the saved record then silently
  reverts to the default. Every combo selection (VAT rate, Net/Gross price mode) is
  instead driven by real keyboard navigation (focus, reset to the top, arrow down to
  the target index, Enter). The same non-persistence issue affects `Edit` fields and
  date pickers, which are typed into via real keystrokes for the same reason.

**Vision fallback.** Two Fakturama panes are custom-painted controls that expose *zero*
child elements through UI Automation, confirmed live by walking the tree - the
debtor/product search-results grids, and the Order's line-items grid. For these, a
screenshot is sent to Claude Opus 5 asking for exact-match counts and row/column
positions as fractions of the image, which are then converted to click coordinates
against the *live-queried* control rectangle at the moment of the click - so the vision
call supplies *what* to click, never a screen coordinate to remember. Every such read is
validated before use (`exact_match_count` must be exactly 1, or the run stops) and every
resulting edit is confirmed by re-reading the actual document total afterward.

**Verification, not assumption.** Every create/select/save step re-queries Fakturama's
own UI state afterward to confirm the action actually took effect - a newly-created
Debtor or Product is re-searched and re-selected from the Order before proceeding; line
edits are confirmed by checking the Order's total actually changed; the final Order and
Invoice are read back and compared field-by-field against the originally extracted data
(`verify_final_records`), collecting every mismatch rather than stopping at the first.

## What was tested vs. validated against real Fakturama

- **Unit-tested** (`uv run pytest -v`, 9 tests, no credentials or Fakturama needed):
  the extraction schema, totals-recomputation cross-check, and JSON-response parsing
  in `extraction.py`, using mocked LLM responses.
- **Validated against the real, installed Fakturama application** (not mocks): every
  UIA interaction - Debtor resolution and creation, Product and VAT resolution and
  creation, line-value editing, Order save, linked Invoice creation via the Order's own
  follow-up action, payment-status application, and final verification - plus the full
  workflow end-to-end, with real image extraction and real vision calls throughout, run
  successfully starting from a freshly-installed, empty Fakturama database (no
  pre-existing Debtor or Products for the sample order to coincidentally match against).

## Known limitations / what's incomplete

- The product/debtor search dialog occasionally closes without a genuine match, for a
  reason that was investigated but not fully root-caused (ruled out: the letter "N",
  search-text length). A post-close verification step (re-checking the Order's Items
  grid via vision before trusting a selection) turns this into a safe stop
  (`ManualReviewRequired`) instead of silently linking the wrong or no product, but the
  underlying Fakturama-side behavior itself isn't fixed.
- Payment **method** (Bank Transfer / Credit Card / etc.) is not selected or created on
  the Invoice - only payment **status** (paid/unpaid, date, value) is applied, per the
  milestone scope this was built against.
- No Delivery, Correction, or Dunning documents (explicitly out of scope).
- Single order image per run; no batch processing.
- A stray malformed product record was observed once during testing (a truncated Item
  Number from an interrupted save) but was never reproduced on demand, so it was not
  root-caused or fixed.
- `uia.py` has grown to nearly 1,000 lines across the milestones it was built in. A
  structural split by responsibility (connection, order, debtor, product, vision,
  shared helpers) was considered and deliberately deferred, to keep the commit history
  focused on one functional capability at a time rather than mixing in a large
  mechanical refactor.

## Written questions

### If you had 3 more hours, what would you do for this task?

1. **Root-cause the intermittent search-dialog closure.** This is the biggest open
   question mark: the product/debtor search dialog sometimes closes without a real
   match, for a reason I narrowed down but didn't pin to a single trigger. I'd
   instrument Fakturama's own log output (or attach a UI Automation event listener
   rather than polling) while reproducing it to get a definitive cause instead of a
   verified-safe workaround.
2. **Payment method selection/creation on the Invoice**, mirroring the already-built
   VAT resolve-or-create pattern - the Payment Method combo needs the same real
   keyboard-navigation selection technique already proven for VAT and price mode.
3. **Split `uia.py`** along the lines discussed during the build (connection/order/
   debtor/product/vision/common), now that its shape is stable, so the next person
   reading it isn't scrolling one file.
4. **Batch mode** - run the workflow over a directory of order images in one pass,
   collecting a summary of which completed and which stopped for manual review.
5. **A tighter feedback loop for the vision-based grid reads** - e.g., cropping to a
   smaller region before asking for column positions consistently (already done for
   the line-item grid) and adding the same technique anywhere else a full-grid
   screenshot is still sent in one shot.
