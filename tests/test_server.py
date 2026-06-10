"""Unit tests for the deterministic logic in server.py.

These are plain pytest UNIT TESTS for the server's pure functions and the
conflicts gate. They are NOT the eval harness — the LLM-driven evaluation
lives in evals/ (see README, "Evaluation").
"""
import asyncio
import json
import re

import pytest
from pydantic import ValidationError

import server


# ---------------------------------------------------------------------------
# Name normalization and similarity
# ---------------------------------------------------------------------------
def test_normalize_name_strips_punctuation_and_case():
    assert server.normalize_name("  Meridian Fabrication, LLC. ") == "meridian fabrication llc"


def test_strip_legal_suffixes():
    assert server.strip_legal_suffixes("meridian fabrication llc") == "meridian fabrication"
    assert server.strip_legal_suffixes("northgate assurance company") == "northgate assurance"
    # A name that is ONLY suffix tokens is returned unchanged rather than emptied.
    assert server.strip_legal_suffixes("company llc") == "company llc"


def test_similarity_exact_and_suffix_variants():
    assert server.name_similarity("Harriet Quill", "Harriet Quill") == 1.0
    assert server.name_similarity("Meridian Fabrication", "Meridian Fabrication LLC") == 1.0
    assert server.name_similarity("Northgate Assurance Co", "Northgate Assurance Company") == 1.0


def test_similarity_token_order_insensitive():
    assert server.name_similarity("Quill Harriet", "Harriet Quill") == 1.0


def test_similarity_unrelated_names_low():
    assert server.name_similarity("Zephyrine Okonkwo-Baptiste", "Harriet Quill") < server.POSSIBLE_MATCH_THRESHOLD


def test_classify_match_thresholds():
    assert server.classify_match(1.0) == "exact"
    assert server.classify_match(0.9) == "strong"
    assert server.classify_match(0.75) == "possible"
    assert server.classify_match(0.5) is None


# ---------------------------------------------------------------------------
# Conflict screen
# ---------------------------------------------------------------------------
def test_screen_exact_client_hit_is_pending():
    result = server.run_conflict_screen(["Harriet Quill"])
    assert result["status"] == "pending"
    matches = result["screened_names"][0]["matches"]
    assert matches[0]["record_id"] == "P-0004"
    assert matches[0]["role"] == "client"
    assert matches[0]["match_strength"] == "exact"


def test_screen_suffix_variant_matches_adverse_party():
    result = server.run_conflict_screen(["Northgate Assurance Co"])
    assert result["status"] == "pending"
    top = result["screened_names"][0]["matches"][0]
    assert top["record_id"] == "P-0003"
    assert top["role"] == "adverse_party"


def test_screen_partial_name_strongest_match_first():
    result = server.run_conflict_screen(["Meridian Fabrication"])
    matches = result["screened_names"][0]["matches"]
    assert matches[0]["record_id"] == "P-0001"  # closer than the Holdings parent
    ids = [m["record_id"] for m in matches]
    assert "P-0002" in ids  # the related entity is also reported
    scores = [m["score"] for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_screen_unknown_name_is_cleared_with_no_matches():
    result = server.run_conflict_screen(["Zephyrine Okonkwo-Baptiste"])
    assert result["status"] == "cleared"
    assert result["screened_names"][0]["match_count"] == 0
    assert result["screened_names"][0]["matches"] == []


def test_screen_mixed_names_overall_pending():
    result = server.run_conflict_screen(["Zephyrine Okonkwo-Baptiste", "Dorian Voss"])
    assert result["status"] == "pending"
    counts = {s["query"]: s["match_count"] for s in result["screened_names"]}
    assert counts["Zephyrine Okonkwo-Baptiste"] == 0
    assert counts["Dorian Voss"] >= 1


def test_screen_provenance_present():
    result = server.run_conflict_screen(["Harriet Quill"])
    prov = result["provenance"]
    assert prov["dataset_version"] == "1.0.0"
    assert prov["dataset_as_of"] == "2026-06-01"
    assert "parties.json" in prov["source"]
    assert "FICTIONAL" in prov["source"]


def test_screen_is_deterministic():
    a = server.run_conflict_screen(["Meridian Fabrication", "Ottoline Marsh"])
    b = server.run_conflict_screen(["Meridian Fabrication", "Ottoline Marsh"])
    assert a == b


# ---------------------------------------------------------------------------
# Risk matrix
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "severity,likelihood,expected",
    [
        ("high", "high", "critical"),
        ("high", "medium", "high"),
        ("medium", "high", "high"),
        ("high", "low", "medium"),
        ("low", "high", "medium"),
        ("medium", "medium", "medium"),
        ("medium", "low", "low"),
        ("low", "medium", "low"),
        ("low", "low", "low"),
    ],
)
def test_risk_matrix_all_combinations(severity, likelihood, expected):
    assert server.derive_risk_rating(severity, likelihood) == expected


def test_risk_rating_none_when_band_missing():
    assert server.derive_risk_rating("high", None) is None
    assert server.derive_risk_rating(None, "low") is None


# ---------------------------------------------------------------------------
# Matter validation (via the tool function, which is pure)
# ---------------------------------------------------------------------------
def test_validate_matter_reports_missing_fields_and_defaults_conflicts():
    params = server.MatterInput(matter_name="Test Matter")
    result = json.loads(server.intake_validate_matter(params))
    assert result["valid"] is True
    assert result["normalized"]["conflicts"]["status"] == "not-run"
    assert "counterparty" in result["missing_recommended_fields"]
    assert "severity" in result["missing_recommended_fields"]
    assert any("Conflicts gate" in w for w in result["warnings"])


def test_validate_matter_full_record_has_no_missing_fields():
    params = server.MatterInput(
        matter_name="Quill v. Voss 2026",
        counterparty="Dorian Voss",
        matter_type="employment",
        our_role="plaintiff",
        jurisdiction="Superior Court, King County",
        practice_area="employment",
        source="web-inquiry",
        conflicts_status="pending",
        severity="high",
        likelihood="medium",
        materiality="none",
        response_deadline="2026-07-15",
    )
    result = json.loads(server.intake_validate_matter(params))
    assert result["missing_recommended_fields"] == []
    assert result["normalized"]["risk_triage"]["risk_rating"] == "high"
    assert result["normalized"]["conflicts"]["status"] == "pending"
    assert not any("Conflicts gate" in w for w in result["warnings"])


def test_validate_matter_unknown_practice_area_warns():
    params = server.MatterInput(matter_name="Test", practice_area="maritime")
    result = json.loads(server.intake_validate_matter(params))
    assert any("Unknown practice_area 'maritime'" in w for w in result["warnings"])


def test_validate_matter_rejects_bad_date():
    with pytest.raises(ValidationError):
        server.MatterInput(matter_name="Test", response_deadline="07/15/2026")


def test_validate_matter_rejects_unknown_enum_and_extra_field():
    with pytest.raises(ValidationError):
        server.MatterInput(matter_name="Test", matter_type="maritime")
    with pytest.raises(ValidationError):
        server.MatterInput(matter_name="Test", unexpected_field="x")


# ---------------------------------------------------------------------------
# Follow-up template
# ---------------------------------------------------------------------------
def test_followup_uses_canonical_question_and_merge_slots():
    result = server.build_followup_template("personal-injury", ["incident_date"])
    assert result["matter_label"] == "Personal Injury"
    assert result["questions"][0]["question"].startswith("When did the incident occur?")
    assert result["merge_slots"] == ["{{client_name}}", "{{firm_name}}", "{{sender_name}}"]
    for slot in result["merge_slots"]:
        assert slot in result["body_template"]
    assert "attorney-client relationship" in result["body_template"]


def test_followup_unknown_field_gets_generic_question():
    result = server.build_followup_template("business", ["favorite_color"])
    assert result["questions"][0]["question"] == "Could you provide more detail on: favorite color?"


def test_followup_free_form_matter_type():
    result = server.build_followup_template("contract", ["counterparty"])
    assert result["matter_label"] == "contract"


def test_followup_is_deterministic():
    a = server.build_followup_template("employment", ["employer_name", "termination_date"])
    b = server.build_followup_template("employment", ["employer_name", "termination_date"])
    assert a == b
    assert [q["field"] for q in a["questions"]] == ["employer_name", "termination_date"]


# ---------------------------------------------------------------------------
# Conflicts gate + triage log
# ---------------------------------------------------------------------------
def test_gate_blocks_not_run_without_override():
    err = server.conflicts_gate_error("not-run", None, None)
    assert err is not None and err.startswith("Error: conflicts gate")


def test_gate_allows_not_run_with_full_override():
    assert server.conflicts_gate_error("not-run", "K. Patel", "Screen deferred per partner") is None


def test_gate_allows_other_statuses():
    for status in ("cleared", "pending", "waived"):
        assert server.conflicts_gate_error(status, None, None) is None


def test_log_triage_refuses_not_run_and_writes_nothing(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv(server.LOG_PATH_ENV_VAR, str(log))
    params = server.LogTriageInput(matter_name="Test", conflicts_status="not-run")
    result = server.intake_log_triage(params)
    assert result.startswith("Error: conflicts gate")
    assert not log.exists()


def test_log_triage_override_fields_must_come_together():
    with pytest.raises(ValidationError):
        server.LogTriageInput(matter_name="Test", conflicts_status="not-run",
                              conflicts_override_by="K. Patel")


def test_log_triage_appends_rows_with_entry_numbers(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv(server.LOG_PATH_ENV_VAR, str(log))

    first = json.loads(server.intake_log_triage(server.LogTriageInput(
        matter_name="Matter One", conflicts_status="pending",
        parties_checked=["Harriet Quill"], summary="screen hit; review queued")))
    second = json.loads(server.intake_log_triage(server.LogTriageInput(
        matter_name="Matter Two", conflicts_status="cleared")))

    assert first["logged"] is True and first["entry_number"] == 1
    assert second["entry_number"] == 2
    lines = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["matter_name"] == "Matter One"
    assert lines[0]["conflicts"] == {"status": "pending", "override": None}
    assert lines[1]["entry_number"] == 2


def test_log_triage_records_explicit_override(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv(server.LOG_PATH_ENV_VAR, str(log))
    result = json.loads(server.intake_log_triage(server.LogTriageInput(
        matter_name="Bypass Matter", conflicts_status="not-run",
        conflicts_override_by="K. Patel",
        conflicts_override_rationale="Emergency TRO intake; screen to follow today")))
    assert result["logged"] is True
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert row["conflicts"]["status"] == "not-run"
    assert row["conflicts"]["override"]["by"] == "K. Patel"


# ---------------------------------------------------------------------------
# Server registration invariants
# ---------------------------------------------------------------------------
def test_all_tool_names_are_intake_prefixed_and_complete():
    tools = asyncio.run(server.mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == [
        "intake_check_conflicts",
        "intake_draft_followup",
        "intake_list_practice_areas",
        "intake_log_triage",
        "intake_validate_matter",
    ]
    for name in names:
        assert re.fullmatch(r"intake_[a-z_]+", name)


def test_read_only_annotations():
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    for name in ("intake_list_practice_areas", "intake_check_conflicts",
                 "intake_validate_matter", "intake_draft_followup"):
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.openWorldHint is False
    log_tool = tools["intake_log_triage"]
    assert log_tool.annotations.readOnlyHint is False
    assert log_tool.annotations.destructiveHint is False  # append-only, never destructive


def test_sample_data_is_labeled_fictional():
    for path in (server.PRACTICE_AREAS_PATH, server.PARTIES_PATH):
        meta = server._load_json(path)["_dataset"]
        assert meta["fictional"] is True
        assert "FICTIONAL" in meta["notice"]
