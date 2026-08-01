# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
"""
Dispute Resolution Protocol — GenLayer Intelligent Contract

A reusable, on-chain dispute resolution primitive for resolving adversarial
disputes through multi-source evidence fetching and AI-validator consensus.

Any escrow contract, DAO, marketplace, or gig-economy platform can integrate
this contract as its resolution layer: call request_ruling() and use the
resulting verdict to release funds, settle bets, or enforce governance outcomes.

Consensus design
----------------
Validators do not need to produce identical rulings. The equivalence check
compares three normalized fields:

  1. decision     — exact match (CLAIMANT_WINS | RESPONDENT_WINS | SPLIT | INVALID)
  2. claimant_%   — within ±10 percentage points (handles rounding variance)
  3. confidence   — within ±20 points (handles evidence quality variance)

Validators also independently re-fetch every evidence URL so each one reads
live content, not a snapshot held by the leader.
"""
from genlayer import *
import json

# ---------------------------------------------------------------------------
# Valid vocabulary constants
# ---------------------------------------------------------------------------
DECISIONS = frozenset({"CLAIMANT_WINS", "RESPONDENT_WINS", "SPLIT", "INVALID"})
REASON_CODES = frozenset({
    "BREACH_OF_CONTRACT",
    "MISREPRESENTATION",
    "QUALITY_DISPUTE",
    "FRAUD",
    "INSUFFICIENT_EVIDENCE",
    "OTHER",
})
APPEAL_TYPES = frozenset({
    "new_evidence",
    "procedural_error",
    "bias_claim",
    "insufficient_evidence_reviewed",
})
STATUS_OPEN          = "OPEN"
STATUS_EVIDENCE_READY = "EVIDENCE_READY"
STATUS_IN_REVIEW     = "IN_REVIEW"
STATUS_RULED         = "RULED"
STATUS_APPEALED      = "APPEALED"
STATUS_FINAL         = "FINAL"


class DisputeResolutionProtocol(gl.Contract):
    """
    Reusable dispute resolution primitive.

    Storage layout (all values stored as JSON strings):
      disputes[dispute_id]   — DisputeRecord
      evidence[evidence_id]  — EvidenceRecord
      rulings[dispute_id]    — RulingRecord (set by request_ruling)
      appeals[appeal_id]     — AppealRecord
      counters               — auto-incrementing IDs
    """
    disputes:        TreeMap[str, str]
    evidence:        TreeMap[str, str]
    rulings:         TreeMap[str, str]
    appeals:         TreeMap[str, str]
    next_dispute_id: u64
    next_evidence_id: u64
    next_appeal_id:  u64

    def __init__(self) -> None:
        self.next_dispute_id  = 1
        self.next_evidence_id = 1
        self.next_appeal_id   = 1

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _require_dispute(self, dispute_id: int) -> dict:
        key = str(dispute_id)
        if key not in self.disputes:
            raise Exception(f"Dispute {dispute_id} not found")
        return json.loads(self.disputes[key])

    def _save_dispute(self, dispute: dict) -> None:
        self.disputes[str(dispute["dispute_id"])] = json.dumps(dispute)

    def _evidence_for(self, dispute_id: int) -> list:
        return [
            json.loads(self.evidence[k])
            for k in self.evidence
            if json.loads(self.evidence[k])["dispute_id"] == dispute_id
        ]

    # -----------------------------------------------------------------------
    # Write — file a dispute
    # -----------------------------------------------------------------------

    @gl.public.write
    def file_dispute(
        self,
        title: str,
        description: str,
        resolution_criteria: str,
        respondent: str,
        claimant_evidence_urls: list,
        context_urls: list,
    ) -> int:
        """
        File a new dispute. Caller is the claimant.

        Parameters
        ----------
        title                  : short human-readable label
        description            : full description of the dispute
        resolution_criteria    : explicit, objective criteria validators must apply
        respondent             : wallet address of the respondent party
        claimant_evidence_urls : URLs the claimant wants validators to evaluate
        context_urls           : shared background docs (contracts, specs, etc.)
        """
        if not title or not title.strip():
            raise Exception("Title is required")
        if not description or not description.strip():
            raise Exception("Description is required")
        if not resolution_criteria or not resolution_criteria.strip():
            raise Exception("Resolution criteria are required")
        if not respondent or not respondent.strip():
            raise Exception("Respondent address is required")
        for url in claimant_evidence_urls + context_urls:
            if not url.startswith("http://") and not url.startswith("https://"):
                raise Exception(f"Invalid URL: {url}")

        dispute_id = int(self.next_dispute_id)
        self.next_dispute_id = dispute_id + 1
        claimant = str(gl.message.sender_address)

        dispute = {
            "dispute_id":           dispute_id,
            "title":                title,
            "description":          description,
            "resolution_criteria":  resolution_criteria,
            "claimant":             claimant,
            "respondent":           respondent,
            "claimant_evidence_urls": claimant_evidence_urls,
            "context_urls":         context_urls,
            "status":               STATUS_OPEN,
            "filed_at":             gl.message_raw["datetime"],
            "ruling_count":         0,
        }
        self._save_dispute(dispute)
        return dispute_id

    # -----------------------------------------------------------------------
    # Write — submit evidence
    # -----------------------------------------------------------------------

    @gl.public.write
    def submit_evidence(
        self,
        dispute_id: int,
        statement: str,
        evidence_urls: list,
    ) -> int:
        """
        Submit a statement and evidence URLs for a dispute.
        Either the claimant or the respondent may call this.
        Once both parties have submitted, status advances to EVIDENCE_READY.
        """
        dispute = self._require_dispute(dispute_id)
        caller = str(gl.message.sender_address)

        if dispute["status"] not in (STATUS_OPEN, STATUS_EVIDENCE_READY):
            raise Exception(f"Cannot submit evidence in status {dispute['status']}")
        if caller not in (dispute["claimant"], dispute["respondent"]):
            raise Exception("Only claimant or respondent may submit evidence")
        if not statement or not statement.strip():
            raise Exception("Statement is required")
        for url in evidence_urls:
            if not url.startswith("http://") and not url.startswith("https://"):
                raise Exception(f"Invalid URL: {url}")

        # Prevent duplicate submissions from same party
        existing = self._evidence_for(dispute_id)
        for ev in existing:
            if ev["submitted_by"] == caller:
                raise Exception("You have already submitted evidence for this dispute")

        evidence_id = int(self.next_evidence_id)
        self.next_evidence_id = evidence_id + 1
        party = "claimant" if caller == dispute["claimant"] else "respondent"

        self.evidence[str(evidence_id)] = json.dumps({
            "evidence_id":   evidence_id,
            "dispute_id":    dispute_id,
            "submitted_by":  caller,
            "party":         party,
            "statement":     statement,
            "evidence_urls": evidence_urls,
            "submitted_at":  gl.message_raw["datetime"],
        })

        # Advance status when both parties have submitted
        submissions = self._evidence_for(dispute_id)
        parties_submitted = {ev["party"] for ev in submissions}
        if "claimant" in parties_submitted and "respondent" in parties_submitted:
            dispute["status"] = STATUS_EVIDENCE_READY
            self._save_dispute(dispute)

        return evidence_id

    # -----------------------------------------------------------------------
    # Write — request ruling  (non-deterministic, validator consensus)
    # -----------------------------------------------------------------------

    @gl.public.write
    def request_ruling(self, dispute_id: int) -> None:
        """
        Trigger validator consensus to produce a ruling.

        Consensus flow
        --------------
        leader_fn   : fetches all evidence URLs from both parties + shared context
                      URLs, then calls the LLM with the full evidence corpus.
        validator_fn: independently re-fetches the same URLs and re-runs the
                      LLM, then verifies the leader's result against its own on
                      three normalized fields.

        Equivalence principle
        ---------------------
          decision         — exact match
          claimant_%       — within ±10 percentage points
          confidence       — within ±20 points

        A unanimous ruling (confidence = 100 from all validators) is the most
        trustworthy; a low-confidence ruling signals borderline evidence.
        """
        dispute = self._require_dispute(dispute_id)
        if dispute["status"] not in (STATUS_EVIDENCE_READY, STATUS_APPEALED):
            raise Exception(
                f"Dispute must be in EVIDENCE_READY or APPEALED status, got {dispute['status']}"
            )

        dispute["status"] = STATUS_IN_REVIEW
        self._save_dispute(dispute)

        # Collect all evidence submissions
        submissions = self._evidence_for(dispute_id)
        claimant_ev = next((ev for ev in submissions if ev["party"] == "claimant"), None)
        respondent_ev = next((ev for ev in submissions if ev["party"] == "respondent"), None)

        # Capture into locals for closure access
        title                = dispute["title"]
        description          = dispute["description"]
        resolution_criteria  = dispute["resolution_criteria"]
        claimant_statement   = claimant_ev["statement"]  if claimant_ev  else "(none submitted)"
        respondent_statement = respondent_ev["statement"] if respondent_ev else "(none submitted)"
        claimant_urls        = (claimant_ev["evidence_urls"] if claimant_ev else []) + dispute["claimant_evidence_urls"]
        respondent_urls      = respondent_ev["evidence_urls"] if respondent_ev else []
        context_urls         = dispute["context_urls"]
        ruling_count         = dispute["ruling_count"]

        MAX_CHARS = 1500
        MAX_URLS  = 4

        def _fetch_block(urls: list, label: str) -> str:
            sections = []
            for url in urls[:MAX_URLS]:
                try:
                    raw = gl.nondet.web.render(url, mode="text").strip()
                    body = raw[:MAX_CHARS] + "\n... [truncated]" if len(raw) > MAX_CHARS else raw
                    sections.append(f"URL: {url}\n{body}")
                except Exception as err:
                    sections.append(f"URL: {url}\n[FETCH FAILED: {err}]")
            return f"--- {label} EVIDENCE ---\n" + "\n\n".join(sections) if sections else f"--- {label}: no URLs ---"

        def leader_fn() -> dict:
            claimant_block  = _fetch_block(claimant_urls,  "CLAIMANT")
            respondent_block = _fetch_block(respondent_urls, "RESPONDENT")
            context_block   = _fetch_block(context_urls,   "SHARED CONTEXT")

            prompt = f"""You are a neutral arbitrator resolving a dispute on a decentralized platform.

DISPUTE TITLE: {title}

DESCRIPTION:
{description}

RESOLUTION CRITERIA (apply these exactly):
{resolution_criteria}

CLAIMANT STATEMENT:
{claimant_statement}

RESPONDENT STATEMENT:
{respondent_statement}

LIVE EVIDENCE (fetched by this validator):
{claimant_block}

{respondent_block}

{context_block}

PREVIOUS RULING ROUNDS: {ruling_count} (if > 0 this is a re-evaluation after appeal)

ARBITRATION TASK:
Evaluate the evidence above against the resolution criteria and produce a ruling.

Rulings on evidence quality:
- If most URLs failed to fetch and you have little to evaluate: use INVALID
- If there IS evidence but it is clearly one-sided or insufficient: use INSUFFICIENT_EVIDENCE reason_code
- If evidence supports the claimant's position: lean CLAIMANT_WINS
- If evidence supports the respondent's position: lean RESPONDENT_WINS
- If evidence partially supports both: use SPLIT

Return a JSON object with EXACTLY these keys:
- decision: one of CLAIMANT_WINS, RESPONDENT_WINS, SPLIT, INVALID
- claimant_percentage: integer 0-100 (share of claim awarded to claimant; 100 for CLAIMANT_WINS, 0 for RESPONDENT_WINS, 0-100 for SPLIT, null for INVALID)
- confidence: integer 0-100 (your confidence in this ruling based on evidence quality)
- reason_code: one of BREACH_OF_CONTRACT, MISREPRESENTATION, QUALITY_DISPUTE, FRAUD, INSUFFICIENT_EVIDENCE, OTHER
- evidence_quality: one of STRONG, MODERATE, WEAK, NONE
- key_findings: array of 2-4 short strings summarizing the deciding evidence points
- summary: one sentence explaining the ruling"""

            raw = gl.nondet.exec_prompt(prompt, response_format="json")
            if not isinstance(raw, dict):
                raise Exception("LLM_ERROR: expected JSON object from exec_prompt")
            decision = raw.get("decision", "")
            if decision not in DECISIONS:
                raise Exception(f"LLM_ERROR: invalid decision '{decision}'")
            pct = raw.get("claimant_percentage")
            if decision != "INVALID" and not isinstance(pct, (int, float)):
                raw["claimant_percentage"] = 50 if decision == "SPLIT" else (100 if decision == "CLAIMANT_WINS" else 0)
            return raw

        def validator_fn(leader_result) -> bool:
            """
            Equivalence check: validators independently re-fetch all evidence
            and re-run the LLM, then compare three normalized fields.

            Passing conditions (all must hold):
              1. decision          — exact string match
              2. claimant_%        — abs(leader - mine) <= 10
              3. confidence        — abs(leader - mine) <= 20

            Any exception during independent evaluation returns False so a
            transient fetch or LLM error on the validator side registers as
            disagreement rather than crashing the consensus round.
            """
            try:
                if not isinstance(leader_result, gl.vm.Return):
                    return False
                leader = leader_result.calldata
                if not isinstance(leader, dict):
                    return False
                if leader.get("decision") not in DECISIONS:
                    return False

                my_result = leader_fn()
                if not isinstance(my_result, dict):
                    return False

                # 1. Direction of ruling must agree exactly
                if leader.get("decision") != my_result.get("decision"):
                    return False

                # 2. Award percentage must be within ±10 points
                leader_pct = int(leader.get("claimant_percentage") or 0)
                my_pct     = int(my_result.get("claimant_percentage") or 0)
                if abs(leader_pct - my_pct) > 10:
                    return False

                # 3. Confidence must be within ±20 points
                leader_conf = int(leader.get("confidence") or 0)
                my_conf     = int(my_result.get("confidence") or 0)
                if abs(leader_conf - my_conf) > 20:
                    return False

                return True
            except Exception:
                return False

        result = gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

        # Persist the ruling
        dispute = self._require_dispute(dispute_id)
        dispute["status"]       = STATUS_RULED
        dispute["ruling_count"] = ruling_count + 1
        self._save_dispute(dispute)

        self.rulings[str(dispute_id)] = json.dumps({
            "dispute_id":         dispute_id,
            "decision":           result.get("decision"),
            "claimant_percentage": result.get("claimant_percentage"),
            "confidence":         result.get("confidence"),
            "reason_code":        result.get("reason_code", "OTHER"),
            "evidence_quality":   result.get("evidence_quality", "MODERATE"),
            "key_findings":       result.get("key_findings", []),
            "summary":            result.get("summary", ""),
            "round":              ruling_count + 1,
            "ruled_at":           gl.message_raw["datetime"],
        })

    # -----------------------------------------------------------------------
    # Write — file appeal
    # -----------------------------------------------------------------------

    @gl.public.write
    def file_appeal(
        self,
        dispute_id: int,
        appeal_type: str,
        grounds: str,
        new_evidence_urls: list,
    ) -> int:
        """
        Appeal a ruling. Only claimant or respondent may appeal.
        Dispute re-enters the evidence phase so the appealing party can add
        new evidence, then must call request_ruling() again.
        """
        dispute = self._require_dispute(dispute_id)
        caller = str(gl.message.sender_address)

        if dispute["status"] != STATUS_RULED:
            raise Exception("Can only appeal a RULED dispute")
        if caller not in (dispute["claimant"], dispute["respondent"]):
            raise Exception("Only claimant or respondent may appeal")
        if appeal_type not in APPEAL_TYPES:
            raise Exception(f"Invalid appeal type. Must be one of: {', '.join(APPEAL_TYPES)}")
        if not grounds or not grounds.strip():
            raise Exception("Appeal grounds are required")

        appeal_id = int(self.next_appeal_id)
        self.next_appeal_id = appeal_id + 1

        self.appeals[str(appeal_id)] = json.dumps({
            "appeal_id":       appeal_id,
            "dispute_id":      dispute_id,
            "appealed_by":     caller,
            "appeal_type":     appeal_type,
            "grounds":         grounds,
            "new_evidence_urls": new_evidence_urls,
            "filed_at":        gl.message_raw["datetime"],
        })

        # If new evidence URLs are provided, register them as a new evidence submission
        if new_evidence_urls:
            for url in new_evidence_urls:
                if not url.startswith("http://") and not url.startswith("https://"):
                    raise Exception(f"Invalid URL: {url}")
            party = "claimant" if caller == dispute["claimant"] else "respondent"
            evidence_id = int(self.next_evidence_id)
            self.next_evidence_id = evidence_id + 1
            self.evidence[str(evidence_id)] = json.dumps({
                "evidence_id":   evidence_id,
                "dispute_id":    dispute_id,
                "submitted_by":  caller,
                "party":         party,
                "statement":     f"[APPEAL ROUND {dispute['ruling_count'] + 1}] {grounds}",
                "evidence_urls": new_evidence_urls,
                "submitted_at":  gl.message_raw["datetime"],
            })

        dispute["status"] = STATUS_APPEALED
        self._save_dispute(dispute)
        return appeal_id

    # -----------------------------------------------------------------------
    # Write — finalize
    # -----------------------------------------------------------------------

    @gl.public.write
    def finalize(self, dispute_id: int) -> None:
        """
        Lock the ruling permanently. Once FINAL, no further appeals are possible.
        Either party may call this after a ruling has been issued.
        """
        dispute = self._require_dispute(dispute_id)
        if dispute["status"] != STATUS_RULED:
            raise Exception("Dispute must be in RULED status to finalize")
        dispute["status"] = STATUS_FINAL
        self._save_dispute(dispute)

    # -----------------------------------------------------------------------
    # View methods
    # -----------------------------------------------------------------------

    @gl.public.view
    def get_dispute(self, dispute_id: int) -> dict:
        return self._require_dispute(dispute_id)

    @gl.public.view
    def get_ruling(self, dispute_id: int) -> dict:
        key = str(dispute_id)
        if key not in self.rulings:
            raise Exception(f"No ruling yet for dispute {dispute_id}")
        return json.loads(self.rulings[key])

    @gl.public.view
    def get_evidence(self, dispute_id: int) -> list:
        return self._evidence_for(dispute_id)

    @gl.public.view
    def get_appeal(self, appeal_id: int) -> dict:
        key = str(appeal_id)
        if key not in self.appeals:
            raise Exception(f"Appeal {appeal_id} not found")
        return json.loads(self.appeals[key])

    @gl.public.view
    def get_all_disputes(self) -> list:
        return [json.loads(self.disputes[k]) for k in self.disputes]

    @gl.public.view
    def get_disputes_by_party(self, address: str) -> list:
        return [
            json.loads(self.disputes[k])
            for k in self.disputes
            if json.loads(self.disputes[k])["claimant"] == address
            or json.loads(self.disputes[k])["respondent"] == address
        ]

    @gl.public.view
    def get_all_rulings(self) -> list:
        return [json.loads(self.rulings[k]) for k in self.rulings]

    @gl.public.view
    def get_stats(self) -> dict:
        disputes = [json.loads(self.disputes[k]) for k in self.disputes]
        rulings  = [json.loads(self.rulings[k])  for k in self.rulings]
        return {
            "total_disputes":  len(disputes),
            "open":            sum(1 for d in disputes if d["status"] == STATUS_OPEN),
            "in_review":       sum(1 for d in disputes if d["status"] == STATUS_IN_REVIEW),
            "ruled":           sum(1 for d in disputes if d["status"] == STATUS_RULED),
            "final":           sum(1 for d in disputes if d["status"] == STATUS_FINAL),
            "appealed":        sum(1 for d in disputes if d["status"] == STATUS_APPEALED),
            "claimant_wins":   sum(1 for r in rulings if r["decision"] == "CLAIMANT_WINS"),
            "respondent_wins": sum(1 for r in rulings if r["decision"] == "RESPONDENT_WINS"),
            "splits":          sum(1 for r in rulings if r["decision"] == "SPLIT"),
        }
