# Fakturama Image-to-Cash Automation
###  Software Design Document

## 1. Introduction

#### Purpose 
This document describes an automation system that transforms a single order image into a saved and verified Order and linked Invoice in Fakturama. It covers the architecture, data flow, UI control-discovery strategy, design decisions, and trade-offs.
#### Goal
Given an order image as the sole input, the system will:
1. Extract and validate order, debtor, payment, and line-item data.
2. Resolve or create the required Debtor, Payment Method, VAT, and Product records.
3. Create and save the Order.
4. Generate its linked Invoice.
5. Apply the extracted payment status.
6. Verify the resulting Order and Invoice.

#### Key constraint
The automation must not rely on hardcoded screen coordinates or a fixed UI layout. Fakturama controls are discovered at runtime using semantic UI properties exposed through Microsoft UI Automation (UIA).

## 2. System Architecture
The system is organized into five layers with clear responsibilities. Together, they extract and validate the source data, orchestrate the Order-first workflow, interact with Fakturama through UIA, and verify the resulting records.

<img width="2432" height="717" alt="image" src="https://github.com/user-attachments/assets/bdc09f70-3077-4aba-8da8-8884bf0395c8" />

The overview shows the system's primary flow from the order image to the verified Order and linked Invoice. The internal responsibilities are separated into the following layers:

| Layer | Responsibility |
|---|---|
| Extraction Service | Converts the order image into normalized and validated structured data. |
| Orchestrator | Controls the Order-first workflow, matching rules, state, and manual-review gates. |
| Fakturama Domain Layer | Encapsulates Fakturama-specific operations for Debtors, Products, VAT, Payment Methods, Orders, and Invoices. |
| UIA Control Layer | Uses Microsoft UI Automation (UIA) to discover and interact with Fakturama controls without relying on hardcoded coordinates. |
| Verification & Audit | Verifies persisted state after critical operations and records evidence for troubleshooting and review. |


## 3. Data Design: Extraction Schema & Fakturama Entities
The system separates extracted source data from Fakturama's persisted entities. The extraction service produces a normalized representation of the order, which the automation engine maps into Fakturama through its UI.

<img width="4108" height="3790" alt="image" src="https://github.com/user-attachments/assets/9c4267e0-831f-412b-9855-19de895a1ac9" />

* Extraction schema (left): A normalized order payload containing the order header, debtor, payment information, line items, and source totals. Source totals are retained for validation and are not written directly into Fakturama.
* Fakturama entities (right): The target entities used to build the transaction. Dependencies are resolved before their consumers: VAT → Product → Order Line and Payment Method → Debtor → Order, followed by Order → Invoice through the follow-up document relationship.
* Source of truth: Fakturama's own selectors are used to determine whether Debtors, Products, VAT rates, and Payment Methods already exist. The automation does not query or modify the Fakturama database directly.

## 4. Control-Discovery and Grounding Strategy

<img width="702" height="2523" alt="image" src="https://github.com/user-attachments/assets/854c88e5-2ec6-4625-ae2b-99a66ac038bd" />


* Primary path: Find the control via UIA → confirm it is uniquely identified → wait until it is ready → act.
* Fallback path: If the control is ambiguous or cannot be uniquely identified → capture a screenshot → use a vision-capable LLM to identify the target location → click → log the fallback usage.
* Both paths converge on a single re-query step before the flow continues, ensuring that every interaction — whether performed through UIA or the vision fallback — is verified by checking the resulting UI state.

## 5. Execution Flow (Order-First)
The workflow transforms a single order image into a verified Order and linked Invoice by resolving existing records, creating missing records when safe, and stopping for manual review when matches are ambiguous.

<img width="1998" height="3097" alt="image" src="https://github.com/user-attachments/assets/597b1d3f-2f6b-4825-909f-b1b3abea503c" />

* The New Order tab stays open for the entire run; Debtor and Product resolution happen as nested detours that always return to it.
* Every resolve step branches three ways: select (exact match) → create (no match) → stop for manual review (ambiguous/conflicting match).
* Product resolution repeats once per line item, in source order, before the Order is completed and its Invoice generated as a follow-up document.

## 6. Verification Strategy
Every create, select, or save action is verified by re-reading the resulting UI or document state before the workflow continues. If the observed state does not match what was expected, the automation stops for manual review rather than guessing or silently continuing.

<img width="1257" height="1524" alt="image" src="https://github.com/user-attachments/assets/b6966293-d786-4757-9a56-a052bab6992b" />

- Verification is a gate after every create/select/save — not a final step.
- Four hard-stop conditions: conflicting matches, a newly created record missing on re-search, an unavailable required Payment Method, or totals that disagree with the source beyond tolerance.
- Success requires the Order and linked Invoice to be present and consistent with the source, including totals, references, and payment status.
  
## 7. Key Trade-offs
| Decision | Trade-off accepted |
|---|---|
| UIA-first, vision-LLM fallback only | More reliable/fast on the common path; two grounding mechanisms to maintain and log |
| Strict exact-match + stop-on-ambiguity | Fewer fully-autonomous runs, in exchange for never silently corrupting master data |
| Single continuous Order-first session | Matches required UX; one long-lived session to keep consistent if a step fails mid-flow |
| OCR + LLM hybrid extraction | More setup than LLM-only, but materially better accuracy on tabular numeric data |
| Recompute-and-compare totals pre-UI | Rejects a few legitimately-rounded documents; cost of bad data in Fakturama is far higher |

## 8. Assumptions & Constraints

- One order per image, broadly similar layout to the reference sample; Windows desktop Fakturama, UIA-accessible.
- No Delivery, Correction, or Dunning documents — the flow ends after Invoice verification.
- No hardcoded coordinates or fixed layout assumptions (explicit task requirement).
