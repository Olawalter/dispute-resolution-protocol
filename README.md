# Dispute Resolution Protocol

A reusable, on-chain dispute resolution primitive for GenLayer. Any escrow contract, DAO, marketplace, or gig-economy platform can call this contract as its resolution layer — instead of building adjudication logic themselves.

The contract uses GenLayer's AI-validator consensus to independently fetch live evidence from both parties, evaluate it against explicit resolution criteria, and produce a structured ruling that is permanently stored on-chain.

---

## Purpose

Deterministic EVM contracts cannot evaluate evidence. They can hold funds in escrow and enforce programmatic conditions, but the moment a dispute requires judgment — "did this deliverable meet the spec?" — deterministic logic breaks down. Courts, centralized arbitrators, and multisigs are the current fallbacks, all of which are slow, expensive, or trust-dependent.

This contract fills that gap. It is a primitive: it produces a **RulingRecord** that any caller (escrow, DAO, prediction market) can read and act on.

---

## What it does

**Dispute lifecycle:**

```
file_dispute()
  └─► OPEN

submit_evidence()  [claimant]
submit_evidence()  [respondent]
  └─► EVIDENCE_READY

request_ruling()   ← validator consensus happens here
  └─► RULED

finalize()
  └─► FINAL

file_appeal()      ← if either party contests the ruling
  └─► APPEALED ─► request_ruling() ─► RULED ─► FINAL
```

**Ruling structure:**
```json
{
  "decision":           "CLAIMANT_WINS | RESPONDENT_WINS | SPLIT | INVALID",
  "claimant_percentage": 75,
  "confidence":          82,
  "reason_code":         "BREACH_OF_CONTRACT",
  "evidence_quality":    "STRONG",
  "key_findings":        ["Delivery confirmed by merged PR", "No payment record found"],
  "summary":             "Claimant delivered on time; respondent failed to pay.",
  "round":               1,
  "ruled_at":            "2026-08-01T12:00:00Z"
}
```

---

## Consensus design

`request_ruling` is the only non-deterministic method. It runs inside `gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`.

### leader_fn

1. Fetches all evidence URLs from both parties — up to 4 per side, capped at 1 500 chars each — using `gl.nondet.web.render(url, mode="text")`.
2. Fetches shared context URLs (contracts, specs, reference docs) the same way.
3. Builds a structured prompt that includes both parties' statements, their live-fetched evidence, and the caller-supplied resolution criteria.
4. Calls `gl.nondet.exec_prompt(prompt, response_format="json")` and validates the response structure.

### validator_fn

Each validator **independently re-fetches every URL** and re-runs the LLM. It does not trust the leader's fetched content. It then checks three normalized fields:

| Field | Rule | Rationale |
|---|---|---|
| `decision` | Exact match | Direction of ruling must agree |
| `claimant_percentage` | `abs(leader − mine) ≤ 10` | Small rounding variance is acceptable |
| `confidence` | `abs(leader − mine) ≤ 20` | Evidence quality interpretation varies |

This is a deliberately loose equivalence: validators can reach slightly different confidence scores and award slightly different percentages, but they must agree on who wins.

### Why not `strict_eq`?

`strict_eq` would require validators to produce identical JSON — impossible when each validator fetches live web content and calls an LLM independently. The equivalence function above captures meaningful agreement without requiring byte-for-byte identity.

---

## Storage design

```python
class DisputeResolutionProtocol(gl.Contract):
    disputes:         TreeMap[str, str]   # dispute_id  → DisputeRecord  (JSON)
    evidence:         TreeMap[str, str]   # evidence_id → EvidenceRecord (JSON)
    rulings:          TreeMap[str, str]   # dispute_id  → RulingRecord   (JSON)
    appeals:          TreeMap[str, str]   # appeal_id   → AppealRecord   (JSON)
    next_dispute_id:  u64
    next_evidence_id: u64
    next_appeal_id:   u64
```

All records are stored as JSON strings inside `TreeMap[str, str]`. This is the correct GenLayer pattern for complex nested data: it avoids the typed-storage restrictions on nested dicts/lists while still using GenLayer's verified storage types at the top level.

Timestamps come from `gl.message_raw['datetime']` — the actual block timestamp, not a hardcoded value.

---

## How to use as a primitive

An escrow contract would:

1. **Deploy** this contract separately (or use a shared instance).
2. Call `file_dispute(...)` when a payer disputes a delivery.
3. Let both parties call `submit_evidence(...)`.
4. Call `request_ruling(dispute_id)` and wait for FINALIZED status.
5. Read `get_ruling(dispute_id)` — use `decision` and `claimant_percentage` to split the held funds.

```python
# Pseudocode — escrow integration
ruling = dispute_contract.get_ruling(dispute_id)
if ruling["decision"] == "CLAIMANT_WINS":
    release_to(claimant, full_amount)
elif ruling["decision"] == "RESPONDENT_WINS":
    release_to(respondent, full_amount)
elif ruling["decision"] == "SPLIT":
    pct = ruling["claimant_percentage"]
    release_to(claimant, full_amount * pct // 100)
    release_to(respondent, full_amount * (100 - pct) // 100)
```

---

## Access control

| Operation | Who may call |
|---|---|
| `file_dispute` | Anyone (becomes claimant) |
| `submit_evidence` | Claimant or respondent only |
| `request_ruling` | Anyone (but dispute must be in correct status) |
| `file_appeal` | Claimant or respondent only |
| `finalize` | Anyone (locks the ruling) |

---

## Appeal system

Five typed appeal grounds prevent frivolous re-reviews:

| Type | When to use |
|---|---|
| `new_evidence` | Evidence that was not available at the time of the original review |
| `procedural_error` | Incorrect party identified or wrong criteria applied |
| `bias_claim` | Validator result appears systematically skewed |
| `insufficient_evidence_reviewed` | Not all submitted URLs were considered |

An appeal resets the dispute to `APPEALED` status. The appealing party may attach new evidence URLs. A new call to `request_ruling()` triggers a fresh validator consensus round with `ruling_count` incremented (visible to the LLM, so it can consider that this is a re-evaluation).

---

## Running the tests

```bash
pip install pytest
pytest tests/direct/ -v
```

29 tests cover:
- State machine transitions (OPEN → EVIDENCE_READY → RULED → FINAL)
- Access control (third-party rejection, duplicate evidence guard)
- URL validation
- **Equivalence principle isolation**: tests that directly verify the validator rejects mismatched decisions and percentage deltas > 10
- Full appeal-and-re-review cycle
- View helpers and stats

---

## Deploy

```bash
npm install -g genlayer
genlayer network set studionet
genlayer deploy contracts/dispute_resolution_protocol.py
```

After deploy, inspect the schema:

```bash
genlayer schema <deployed-address>
```

---

## File structure

```
contracts/
  dispute_resolution_protocol.py   ← the primitive (single file, no dependencies)

tests/
  direct/
    test_dispute_resolution.py     ← 29 in-memory unit tests
```

---

## Use cases

| Application | How it uses this primitive |
|---|---|
| Freelance escrow | Release funds based on `decision` + `claimant_percentage` |
| DAO grant disbursement | Resolve disputes between grant recipients and committees |
| NFT royalty disputes | Adjudicate disputed royalty splits |
| Prediction market resolution | Adapt for YES/NO markets with `claimant_percentage` = 0 or 100 |
| Insurance claims | Evaluate claim evidence against policy terms |
| Content moderation | Resolve appeals against moderation decisions |
