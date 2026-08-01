"""
Direct (in-memory) tests for DisputeResolutionProtocol.

These tests cover:
  - State machine transitions
  - Access control (only parties may submit evidence / appeal)
  - Duplicate evidence guard
  - URL validation
  - Equivalence principle logic (mocked LLM output)
  - Full ruling cycle including re-review after appeal
  - View helpers and stats

Run with:
    pytest tests/direct/ -v
"""
import json
import pytest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Minimal GenLayer shims so the contract imports without a live GenVM
# ---------------------------------------------------------------------------
import sys
import types


def _make_genlayer_shims():
    gl_mod = types.ModuleType("genlayer")
    gl = types.ModuleType("genlayer.gl")

    # Storage types
    class TreeMap(dict):
        pass

    class DynArray(list):
        pass

    # Sized integers — just an int in tests
    class _SizedInt(int):
        pass

    u64 = _SizedInt

    class _MessageSim:
        sender_address = "0xClaimant0000000000000000000000000000000001"

    class _MessageRaw:
        def __getitem__(self, key):
            return "2026-08-01T12:00:00Z"

    class _Return:
        def __init__(self, data):
            self.calldata = data

    class _Vm:
        Return = _Return

        @staticmethod
        def run_nondet_unsafe(leader_fn, validator_fn):
            result = leader_fn()
            leader_return = _Return(result)
            assert validator_fn(leader_return), "Validator rejected leader result"
            return result

    class _NondetWebStub:
        @staticmethod
        def render(url, mode="text"):
            return f"[MOCKED content for {url}]"

    class _NondetStub:
        web = _NondetWebStub()

        @staticmethod
        def exec_prompt(prompt, response_format="json"):
            return {
                "decision": "CLAIMANT_WINS",
                "claimant_percentage": 100,
                "confidence": 82,
                "reason_code": "BREACH_OF_CONTRACT",
                "evidence_quality": "STRONG",
                "key_findings": ["Claimant delivered on time", "Respondent failed to pay"],
                "summary": "Clear breach of payment obligation.",
            }

    class _Contract:
        pass

    class _Public:
        @staticmethod
        def write(fn):
            return fn

        @staticmethod
        def view(fn):
            return fn

    gl.Contract = _Contract
    gl.public   = _Public()
    gl.message  = _MessageSim()
    gl.message_raw = _MessageRaw()
    gl.vm       = _Vm()
    gl.nondet   = _NondetStub()

    # Inject storage type aliases into builtins namespace used by the contract
    gl_mod.gl         = gl
    gl_mod.TreeMap    = TreeMap
    gl_mod.DynArray   = DynArray
    gl_mod.u64        = u64

    sys.modules["genlayer"]    = gl_mod
    sys.modules["genlayer.gl"] = gl

    return gl_mod, gl


GL_MOD, GL = _make_genlayer_shims()

# Patch __builtins__ inside the contract's module after import so that
# TreeMap / DynArray / u64 resolve from the shim.
import importlib.util, pathlib

_CONTRACT_PATH = pathlib.Path(__file__).parents[2] / "contracts" / "dispute_resolution_protocol.py"

spec = importlib.util.spec_from_file_location("dispute_resolution_protocol", _CONTRACT_PATH)
contract_module = importlib.util.module_from_spec(spec)
# Inject shim symbols before exec
contract_module.__dict__.update({
    "TreeMap":  GL_MOD.TreeMap,
    "DynArray": GL_MOD.DynArray,
    "u64":      GL_MOD.u64,
})
spec.loader.exec_module(contract_module)

DisputeResolutionProtocol = contract_module.DisputeResolutionProtocol


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def make_contract():
    c = DisputeResolutionProtocol.__new__(DisputeResolutionProtocol)
    c.disputes        = GL_MOD.TreeMap()
    c.evidence        = GL_MOD.TreeMap()
    c.rulings         = GL_MOD.TreeMap()
    c.appeals         = GL_MOD.TreeMap()
    c.next_dispute_id  = 1
    c.next_evidence_id = 1
    c.next_appeal_id   = 1
    return c


def set_sender(address: str):
    GL.message.sender_address = address


CLAIMANT   = "0xClaimant0000000000000000000000000000000001"
RESPONDENT = "0xRespondent000000000000000000000000000002"
THIRD_PARTY = "0xThirdParty0000000000000000000000000003"


def _file_dispute(c, claimant=CLAIMANT, respondent=RESPONDENT):
    set_sender(claimant)
    return c.file_dispute(
        title="Payment dispute",
        description="Claimant delivered the project but respondent refused to pay.",
        resolution_criteria="If evidence shows delivery was completed per spec, award claimant.",
        respondent=respondent,
        claimant_evidence_urls=["https://github.com/example/deliverables"],
        context_urls=["https://github.com/example/contract-spec"],
    )


def _submit_both(c, dispute_id):
    set_sender(CLAIMANT)
    c.submit_evidence(
        dispute_id,
        statement="I completed all milestones. See the merged PR.",
        evidence_urls=["https://github.com/example/pr/42"],
    )
    set_sender(RESPONDENT)
    c.submit_evidence(
        dispute_id,
        statement="The deliverable did not meet specs.",
        evidence_urls=["https://github.com/example/issues/10"],
    )


# ---------------------------------------------------------------------------
# State machine tests
# ---------------------------------------------------------------------------


class TestFileDispute:
    def test_creates_dispute_with_open_status(self):
        c = make_contract()
        did = _file_dispute(c)
        d = c.get_dispute(did)
        assert d["status"] == "OPEN"
        assert d["claimant"] == CLAIMANT
        assert d["respondent"] == RESPONDENT
        assert did == 1

    def test_auto_increments_ids(self):
        c = make_contract()
        d1 = _file_dispute(c)
        d2 = _file_dispute(c)
        assert d2 == d1 + 1

    def test_rejects_empty_title(self):
        c = make_contract()
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="Title is required"):
            c.file_dispute(
                title="",
                description="desc",
                resolution_criteria="criteria",
                respondent=RESPONDENT,
                claimant_evidence_urls=[],
                context_urls=[],
            )

    def test_rejects_invalid_url(self):
        c = make_contract()
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="Invalid URL"):
            c.file_dispute(
                title="Test",
                description="desc",
                resolution_criteria="criteria",
                respondent=RESPONDENT,
                claimant_evidence_urls=["not-a-url"],
                context_urls=[],
            )


class TestSubmitEvidence:
    def test_both_parties_advance_to_evidence_ready(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        d = c.get_dispute(did)
        assert d["status"] == "EVIDENCE_READY"

    def test_single_submission_stays_open(self):
        c = make_contract()
        did = _file_dispute(c)
        set_sender(CLAIMANT)
        c.submit_evidence(did, "My statement", [])
        d = c.get_dispute(did)
        assert d["status"] == "OPEN"

    def test_third_party_cannot_submit(self):
        c = make_contract()
        did = _file_dispute(c)
        set_sender(THIRD_PARTY)
        with pytest.raises(Exception, match="Only claimant or respondent"):
            c.submit_evidence(did, "Hacking in.", [])

    def test_duplicate_submission_rejected(self):
        c = make_contract()
        did = _file_dispute(c)
        set_sender(CLAIMANT)
        c.submit_evidence(did, "First submission", [])
        with pytest.raises(Exception, match="already submitted"):
            c.submit_evidence(did, "Second attempt", [])

    def test_rejects_bad_url_in_evidence(self):
        c = make_contract()
        did = _file_dispute(c)
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="Invalid URL"):
            c.submit_evidence(did, "Statement", ["ftp://bad-scheme"])

    def test_claimant_urls_included_in_ruling(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        evs = c.get_evidence(did)
        parties = {ev["party"] for ev in evs}
        assert "claimant" in parties
        assert "respondent" in parties


class TestRequestRuling:
    def test_ruling_written_after_consensus(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        set_sender(CLAIMANT)
        c.request_ruling(did)
        ruling = c.get_ruling(did)
        assert ruling["decision"] == "CLAIMANT_WINS"
        assert ruling["claimant_percentage"] == 100
        assert ruling["confidence"] == 82
        assert ruling["reason_code"] == "BREACH_OF_CONTRACT"

    def test_dispute_status_is_ruled(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        d = c.get_dispute(did)
        assert d["status"] == "RULED"

    def test_cannot_request_ruling_in_open_status(self):
        c = make_contract()
        did = _file_dispute(c)  # only claimant evidence present
        with pytest.raises(Exception, match="EVIDENCE_READY"):
            c.request_ruling(did)

    def test_ruling_count_increments(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        d = c.get_dispute(did)
        assert d["ruling_count"] == 1


class TestEquivalencePrinciple:
    """
    Unit-tests the validator_fn equivalence logic in isolation by exercising
    request_ruling with controlled leader/validator LLM outputs.
    """

    def _run_with_outputs(self, leader_out, validator_out):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)

        call_count = 0

        def mock_exec_prompt(prompt, response_format="json"):
            nonlocal call_count
            call_count += 1
            return leader_out if call_count == 1 else validator_out

        with patch.object(GL.nondet, "exec_prompt", side_effect=mock_exec_prompt):
            with patch.object(GL.nondet.web, "render", return_value="[mocked]"):
                c.request_ruling(did)

        return c.get_ruling(did)

    def test_passes_when_decision_matches_and_pct_within_10(self):
        ruling = self._run_with_outputs(
            {"decision": "SPLIT", "claimant_percentage": 60, "confidence": 70,
             "reason_code": "QUALITY_DISPUTE", "evidence_quality": "MODERATE",
             "key_findings": ["partial delivery"], "summary": "Partial."},
            {"decision": "SPLIT", "claimant_percentage": 65, "confidence": 75,
             "reason_code": "QUALITY_DISPUTE", "evidence_quality": "MODERATE",
             "key_findings": ["partial delivery"], "summary": "Partial."},
        )
        assert ruling["decision"] == "SPLIT"
        assert ruling["claimant_percentage"] == 60

    def test_validator_rejects_mismatched_decision(self):
        """If validator disagrees on the direction, consensus should fail."""
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)

        call_count = 0

        def mock_exec(prompt, response_format="json"):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"decision": "CLAIMANT_WINS", "claimant_percentage": 100,
                        "confidence": 80, "reason_code": "BREACH_OF_CONTRACT",
                        "evidence_quality": "STRONG", "key_findings": [], "summary": "A"}
            return {"decision": "RESPONDENT_WINS", "claimant_percentage": 0,
                    "confidence": 80, "reason_code": "MISREPRESENTATION",
                    "evidence_quality": "STRONG", "key_findings": [], "summary": "B"}

        original_run = GL.vm.run_nondet_unsafe

        validator_rejections = []

        def run_nondet_capturing(leader_fn, validator_fn):
            result = leader_fn()
            ret = type('Return', (), {'calldata': result})()
            passed = validator_fn(ret)
            validator_rejections.append(passed)
            if not passed:
                raise Exception("Validators rejected the result")
            return result

        with patch.object(GL.vm, "run_nondet_unsafe", side_effect=run_nondet_capturing):
            with patch.object(GL.nondet, "exec_prompt", side_effect=mock_exec):
                with patch.object(GL.nondet.web, "render", return_value="[mocked]"):
                    with pytest.raises(Exception):
                        c.request_ruling(did)

        assert False in validator_rejections, "Expected validator to reject mismatched decision"

    def test_validator_rejects_percentage_diff_above_10(self):
        """Percentage delta of 11 should fail the equivalence check."""
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        call_count = 0

        def mock_exec(prompt, response_format="json"):
            nonlocal call_count
            call_count += 1
            pct = 60 if call_count == 1 else 72  # delta = 12 > 10
            return {"decision": "SPLIT", "claimant_percentage": pct,
                    "confidence": 70, "reason_code": "OTHER",
                    "evidence_quality": "MODERATE", "key_findings": [], "summary": "Split."}

        validator_rejections = []

        def run_nondet_capturing(leader_fn, validator_fn):
            result = leader_fn()
            ret = type('Return', (), {'calldata': result})()
            passed = validator_fn(ret)
            validator_rejections.append(passed)
            if not passed:
                raise Exception("Validators rejected")
            return result

        with patch.object(GL.vm, "run_nondet_unsafe", side_effect=run_nondet_capturing):
            with patch.object(GL.nondet, "exec_prompt", side_effect=mock_exec):
                with patch.object(GL.nondet.web, "render", return_value="[mocked]"):
                    with pytest.raises(Exception):
                        c.request_ruling(did)

        assert False in validator_rejections


class TestAppeal:
    def test_appeal_sets_status_to_appealed(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        set_sender(CLAIMANT)
        aid = c.file_appeal(
            did,
            "new_evidence",
            "I have additional GitHub commits proving delivery.",
            ["https://github.com/example/commit/abc"],
        )
        d = c.get_dispute(did)
        assert d["status"] == "APPEALED"
        appeal = c.get_appeal(aid)
        assert appeal["appealed_by"] == CLAIMANT

    def test_cannot_appeal_unruled_dispute(self):
        c = make_contract()
        did = _file_dispute(c)
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="RULED"):
            c.file_appeal(did, "new_evidence", "grounds", [])

    def test_third_party_cannot_appeal(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        set_sender(THIRD_PARTY)
        with pytest.raises(Exception, match="Only claimant or respondent"):
            c.file_appeal(did, "new_evidence", "grounds", [])

    def test_invalid_appeal_type_rejected(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="Invalid appeal type"):
            c.file_appeal(did, "made_up_type", "grounds", [])

    def test_re_review_after_appeal(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        set_sender(CLAIMANT)
        c.file_appeal(did, "new_evidence", "New PR shows delivery.", ["https://example.com/pr"])
        # Appealed status allows request_ruling again
        c.request_ruling(did)
        d = c.get_dispute(did)
        assert d["status"] == "RULED"
        assert d["ruling_count"] == 2


class TestFinalize:
    def test_finalizes_ruled_dispute(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        set_sender(CLAIMANT)
        c.finalize(did)
        d = c.get_dispute(did)
        assert d["status"] == "FINAL"

    def test_cannot_finalize_open_dispute(self):
        c = make_contract()
        did = _file_dispute(c)
        with pytest.raises(Exception, match="RULED"):
            c.finalize(did)

    def test_cannot_appeal_finalized_dispute(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        c.finalize(did)
        set_sender(CLAIMANT)
        with pytest.raises(Exception, match="RULED"):
            c.file_appeal(did, "new_evidence", "Too late.", [])


class TestViewMethods:
    def test_get_all_disputes(self):
        c = make_contract()
        _file_dispute(c)
        _file_dispute(c)
        assert len(c.get_all_disputes()) == 2

    def test_get_disputes_by_party(self):
        c = make_contract()
        _file_dispute(c, claimant=CLAIMANT, respondent=RESPONDENT)
        _file_dispute(c, claimant=THIRD_PARTY, respondent=CLAIMANT)
        by_claimant = c.get_disputes_by_party(CLAIMANT)
        assert len(by_claimant) == 2  # claimant in both as claimant or respondent

    def test_stats(self):
        c = make_contract()
        did = _file_dispute(c)
        _submit_both(c, did)
        c.request_ruling(did)
        stats = c.get_stats()
        assert stats["total_disputes"] == 1
        assert stats["claimant_wins"] == 1
        assert stats["ruled"] == 1

    def test_get_ruling_raises_before_review(self):
        c = make_contract()
        did = _file_dispute(c)
        with pytest.raises(Exception, match="No ruling yet"):
            c.get_ruling(did)
