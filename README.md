# Change-Order Extraction Pipeline

Turns messy change-order documents into validated JSON. The inputs can be typed PDFs, scans, informal emails, emails with PDF attachments, multi-CO logs and OCR'd text. Every field in the output carries a confidence score, a verbatim evidence quote and flags. Each change order also gets a routing decision: **auto-accept**, or **send to a human**.

```bash
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -r requirements.txt
python samples/make_samples.py                      # build the 10-document test set + gold labels

echo OPENAI_API_KEY=sk-... > .env                    # or ANTHROPIC_API_KEY
python -m co_extract samples/docs --out out/        # auto: uses the LLM if a key is set, else rules
python -m co_extract some_co.pdf --mode llm         # force the LLM path
python eval/run_eval.py --mode rules --errors       # score against gold labels
python eval/run_eval.py --mode llm
pytest -q                                           # 29 tests; LLM and OCR paths are tested with a fake client
```

Settings come from environment variables or `.env`: `CO_PROVIDER` (`openai` or `anthropic`; defaults to whichever key is set), `CO_MODEL` (default `gpt-4.1-mini` for OpenAI, `claude-opus-5-5` for Anthropic) and `CO_REVIEW_THRESHOLD` (default `0.80`).

---

## Writeup

### The problem as I framed it

A change order is money. If a wrong total is written silently into job costing, the cost is far worse than a PM spending 30 seconds on a review. So the goal is not "extract everything." The goal is to **extract what we can, know which parts we can trust, and send the rest to a human with a reason attached.** The metric I optimize is **auto-accept precision**: of the fields written without review, how many are right. Second to that is coverage: how many fields we can auto-accept at all.

### Pipeline

```
ingest ──► OCR* ──► extract ──► normalize ──► derive ──► validate ──► score ──► route
 PDF/scan    vision,    LLM (primary)   money, dates,   fill subtotal/   arithmetic,    per-field     auto-accept
 email+att   regex (2nd      days, CO ids,   total from       required       confidence    or review
 text/OCR    opinion)        signs           lines, flagged   fields, sanity               (+ reasons)
```

1. **Ingest** ([ingest.py](co_extract/ingest.py)): PyMuPDF text layer with NFKC normalization, because real PDFs contain ligatures like `ﬃ` that silently break matching. Works **page by page**: a page with almost no text (a scan inside an otherwise typed PDF, a phone photo `.jpg/.png`, an image attachment) is rendered to an image and queued for OCR. `.eml` files are parsed and their PDF and image attachments pulled out, since change orders arrive by email.
   - **Vision OCR fallback** ([ocr.py](co_extract/ocr.py)), *only for the queued pages*, so typed PDFs cost nothing extra: the vision model transcribes the page verbatim (no corrections, `[illegible]` for unreadable parts), and the transcript is flattened to plain text in code because models still emit markdown tables. The transcript goes **back into the text** before extraction, so the regex second opinion and the evidence-grounding check work on scans too, instead of being switched off.
2. **Extract**
   - **LLM** ([llm.py](co_extract/llm.py)): OpenAI `gpt-4.1-mini` by default (Claude also supported), with strict JSON-schema structured outputs. The PDF goes in as a native file part, so scans are read visually and need no OCR step. For every field the model returns `value`, a **verbatim `evidence` quote** and an `uncertain_reason`. It is told *not* to compute missing values. Arithmetic is done by code, never by the model. It returns a list, so a CO log becomes several records, and the latest revision wins.
   - **Rules** ([rules.py](co_extract/rules.py)): a regex extractor tuned for precision that prefers to abstain rather than guess. It serves two purposes: a zero-cost offline fallback, and an *independent second opinion* for the LLM.
3. **Normalize** ([normalize.py](co_extract/normalize.py)): handles `(1,200.00)` / `1,200 CR` / `credit of $500` → negative, `$4.2k`, OCR digit swaps (`$8,9l5.OO` → 8915), `"no change to contract time"` → 0 days, and `CO #007` = `C.O. No. 7`. `PCO-21` stays distinct, because a potential CO is a different record.
4. **Derive and validate** ([validate.py](co_extract/validate.py)): checks that qty × unit price = amount, that line items sum to the subtotal, and that subtotal + markup + tax = total. Missing totals are computed but marked `source: "derived"` and always sent to review. Other checks: required fields present, plausible dates, a value-engineering CO with a positive total, and "approved" with no signer.
5. **Confidence** ([confidence.py](co_extract/confidence.py)): see below.
6. **Route**: a change order needs review if any error-level issue fired, any critical field (CO number, project, total) scores under the threshold, or the total was derived. `review_reasons` explains why in plain English.

### Confidence: computed from evidence, not asked from the model

A model's own "I'm 95% sure" is poorly calibrated, so I don't use it. Each field's score is a logistic combination of signals the pipeline can **check**:

| Signal | Why it matters |
|---|---|
| **Grounded**: the evidence quote exists in the source text | Catches hallucinations. Text matching is fuzzy, but **numbers must match exactly**: `$2,760` vs `$2,706` is a one-character difference that fuzzy matching would wave through. |
| **Value is in its evidence** | Catches a right quote with a wrongly parsed value (sign, digit). |
| **Regex agrees** | Two independent methods agreeing is strong evidence; disagreement is a red flag. |
| **Arithmetic reconciles** | The strongest signal. If lines → subtotal → total all add up, the money fields confirm each other independently of any model. |
| Model flagged uncertainty, a validation rule fired, scanned source, keyword-only classification | Penalties. |

The weights are hand-set priors. With real labelled data (PM corrections in the review queue) I would fit them with logistic regression and check calibration per document type. `eval/run_eval.py` already reports the reliability buckets needed for that.

### Results

The test set has 10 documents and 11 change orders, each built to hit a specific failure mode: a clean form, an informal email, a credit/deduct with parenthesized negatives, a **scanned PDF with no text layer**, **line items that don't reconcile**, **two COs in one PDF**, an email with a **PDF attachment**, **OCR noise** (`T0TAL: $8,9l5.OO`), a **phone photo (JPEG)**, and a **mixed PDF** whose typed cover page has no prices because the pricing page is a scan.

| Metric | Rules only (offline, $0) | LLM (`gpt-4.1-mini`) + rules |
|---|---|---|
| Field accuracy (where gold has a value) | 72.1% | **98.6%** |
| False-fill rate (value invented where gold is empty) | 0% | 0% |
| **Auto-accept precision** (confidence ≥ 0.8) | **100%** | **99.3%** |
| Auto-accept coverage | 60.0% | 99.3% |
| Total amount correct | 8/11 (the 3 image-only totals went to review) | **11/11** |
| Scan, phone photo, mixed PDF | not readable, all sent to review | read via vision OCR; evidence and arithmetic checked on the transcript |
| Arithmetic-mismatch CO | sent to review | sent to review |

Rules only: the scans, the photo, the mixed PDF, the email and the arithmetic mismatch go to review, and the 6 clean COs are auto-accepted. The rules miss what needs language understanding (a project name inside an email subject, a contractor shown only in a letterhead, reason codes) or vision. Those are exactly the cases the confidence layer sends to review instead of guessing.

With the LLM, both remaining errors are the `reason` category, which is partly a judgment call. CO 007 says "per RFI-042" and the model says `design_change` where I labelled `owner_request`. The phone photo's fire-marshal change was labelled `owner_request`, but at **0.62 confidence**, below the review threshold: the score flagged its own mistake. Raw output is in `eval/results_llm.json`.

**What the eval caught during development:** the first LLM run scored 95.7%. It showed the model (a) left `reason` empty instead of choosing, (b) put the project name in the `owner` field with 0.96 confidence, and (c) turned email prose into line items. A tighter prompt fixed (a), and (c) mostly. For (b), I didn't rely on the prompt alone: a code check now makes "party name equals project name" an error that forces review, because the model still does it on some runs.

**Honest caveat:** I wrote both the documents and the rules, so these numbers prove the *mechanism* (routing, calibration, failure detection) more than real-world accuracy. The next step is a held-out set of real change orders.

### Failure modes, and what the pipeline does about each

| Failure mode | Mitigation | Remaining risk |
|---|---|---|
| LLM invents or "fixes" a number | Verbatim evidence with exact number matching, regex cross-check, arithmetic | If the document itself is wrong *and* reconciles, nothing catches it |
| Document arithmetic doesn't reconcile (real typo on the form) | `subtotal_mismatch` / `total_mismatch` → review, with the amounts in the reason | Which number is the "true" one is a human decision; the pipeline doesn't pick |
| Sign of credits and deducts | Accounting parentheses, `CR` and "credit" are all parsed; a value-engineering CO with a positive total is flagged | Parentheses in prose ("markup ($577.50)") are ambiguous; the rules treat them as negative only at the end of a table row |
| Scans, photos, mixed PDFs | Page-level detection; vision OCR only on textless pages; transcript re-enters the normal pipeline so grounding, regex and arithmetic checks still apply; smaller confidence penalty (`ocr_source`) because OCR and extraction can share the same misreading; if OCR fails the CO goes to review instead of being dropped | A digit misread the same way by OCR and extraction, if the arithmetic still happens to reconcile; handwriting; stamps over numbers |
| Multiple COs, or revisions of one CO, in a document | LLM returns a list and keeps the latest revision; COs are paired by normalized number; duplicates warned | Revision history isn't modelled; R1 vs R2 needs the CO log |
| Proposed vs approved | "approved" requires a signature or explicit approval; approved with no signer → warning | A signature image with no typed name |
| Missing total ("see attached") | Derived from lines, marked `derived`, always reviewed | |
| Prompt injection inside a document | Structured output plus evidence grounding: an instruction in the PDF can't produce an ungrounded value without being flagged | Not adversarially tested |
| Same input, different answer (LLM non-determinism, even at temperature 0) | Code-level checks (arithmetic, party ≠ project, grounding) don't depend on the model behaving the same twice | Run-to-run variance should be tracked in production evals |
| LLM down, refusal or truncated output | Refusals and truncation are detected (and Claude gets server-side model fallback); in `auto` mode the pipeline falls back to rules and adds a warning instead of dropping the document | |

### What I would build next at Sledge

- **Tie each CO to the job.** Fuzzy-match project name and number against the company's job list (QuickBooks / Sledge jobs), and treat a match as another confidence signal. An extracted project that doesn't exist is a red flag.
- **Map line items to the company's cost codes** via retrieval over their code list and past coded COs, so a CO lands on the right budget line.
- **Use the review queue as training data.** Every PM correction becomes a labelled example, which feeds weight refitting, per-customer regex/prompt hints and regression evals.
- **Cost control.** Run the rules first. When they reconcile perfectly, skip the LLM or use a smaller model. Send only messy documents to the larger model, and batch the backlog.
- **Track a CO's lifecycle.** Link PCO → CO → revision → owner approval → invoice / pay app, and flag approved COs that never reached billing. That's revenue a contractor is leaving on the table.

### Layout

```
co_extract/   schema · ingest · ocr · rules · llm · normalize · validate · confidence · pipeline · cli
samples/      make_samples.py → docs/ (10 inputs) + gold/ (labels)
eval/         run_eval.py (accuracy, false-fill, calibration, auto-accept precision)
tests/        unit + end-to-end + LLM path with a fake client
```
