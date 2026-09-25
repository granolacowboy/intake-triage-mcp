#!/usr/bin/env python3
"""intake_triage_mcp — a deterministic MCP server for legal intake triage.

This server gives an MCP client (e.g., Claude) a small set of validated,
deterministic tools for triaging incoming legal inquiries:

- enumerate the firm's practice areas (bundled sample data),
- screen party names against a bundled conflicts dataset,
- validate and normalize a structured matter summary,
- produce a deterministic follow-up email template with merge slots,
- append a triage record to a local JSONL log (gated on conflicts status).

Design notes:
- The server makes NO LLM calls and NO network calls. Extracting structure
  from a raw inquiry (names, dates, narrative) is the client model's job;
  this server provides deterministic validation, matching, and templating.
- All bundled sample data is FICTIONAL and clearly labeled as such. It is
  not derived from any real law firm, client, or matter.
- This software supports intake workflows; it does not provide legal advice,
  and its conflict screen is illustrative — it never substitutes for a real
  conflicts clearance process.
- stdio transport: stdout is reserved for JSON-RPC framing, so all logging
  goes to stderr.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# Logging — stderr ONLY (stdio servers must never write logs to stdout).
# --------------------------------------------------------------------------
logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("intake_triage_mcp")

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent / "data"
PRACTICE_AREAS_PATH = DATA_DIR / "practice_areas.json"
PARTIES_PATH = DATA_DIR / "parties.json"

LOG_PATH_ENV_VAR = "INTAKE_TRIAGE_LOG_PATH"
DEFAULT_LOG_PATH = "triage_log.jsonl"

# Conflict-screen match thresholds (similarity scores in [0, 1]).
EXACT_MATCH_THRESHOLD = 0.95
STRONG_MATCH_THRESHOLD = 0.83
POSSIBLE_MATCH_THRESHOLD = 0.70

# Common legal-entity suffixes ignored when comparing organization names.
LEGAL_SUFFIXES = frozenset({
    "llc", "llp", "lp", "pllc", "plc", "pc", "pa",
    "inc", "incorporated", "corp", "corporation",
    "co", "company", "ltd", "limited", "group", "partners",
})

DATE_FORMAT = "%Y-%m-%d"

# Severity x likelihood -> risk rating (3x3 matrix, documented in README).
RISK_MATRIX: Dict[tuple, str] = {
    ("high", "high"): "critical",
    ("high", "medium"): "high",
    ("medium", "high"): "high",
    ("high", "low"): "medium",
    ("low", "high"): "medium",
    ("medium", "medium"): "medium",
    ("medium", "low"): "low",
    ("low", "medium"): "low",
    ("low", "low"): "low",
}

# Fields the validator recommends for a complete intake record, in the order
# they are reported when missing.
RECOMMENDED_FIELDS = (
    "counterparty",
    "matter_type",
    "our_role",
    "jurisdiction",
    "practice_area",
    "source",
    "severity",
    "likelihood",
    "response_deadline",
)

# Canonical follow-up questions for known intake fields. Anything not listed
# here gets a deterministic generic phrasing.
FIELD_QUESTIONS: Dict[str, str] = {
    "incident_date": "When did the incident occur? An exact or approximate date helps us assess filing deadlines.",
    "injury_description": "Could you describe the injuries or harm involved?",
    "treatment_status": "Have you received medical treatment, and is treatment ongoing?",
    "insurance_carrier": "Which insurance carrier(s), if any, are involved?",
    "counterparty": "Who is the other party (person, company, or agency) involved?",
    "jurisdiction": "Where did this take place (city, county, and state)?",
    "employer_name": "What is the name of the employer involved?",
    "employment_status": "Are you currently employed there, or has the employment ended?",
    "termination_date": "If the employment ended, on what date did it end?",
    "marriage_date": "What is the date of the marriage?",
    "children_count": "How many minor children, if any, are involved?",
    "existing_will": "Do you have an existing will, trust, or other estate documents?",
    "assets_overview": "Could you give a brief overview of the assets involved (home, accounts, business interests)?",
    "family_structure": "Who are the family members or beneficiaries you would like covered?",
    "business_entity_type": "What type of business entity is involved (LLC, corporation, partnership, sole proprietorship)?",
    "contract_date": "When was the contract or agreement signed?",
    "dispute_value": "Approximately how much money is at stake in the dispute?",
    "charges": "What charges have been filed or threatened, if known?",
    "arrest_date": "When did the arrest or citation occur?",
    "court_date": "Do you have an upcoming court date? If so, when?",
    "response_deadline": "Have you received any documents with a deadline to respond? If so, what is the deadline?",
    "source": "How did this matter first reach you (e.g., demand letter, court papers, referral)?",
}

# --------------------------------------------------------------------------
# Enums (string-valued; conflicts statuses follow the
# anthropics/claude-for-legal matter-intake convention)
# --------------------------------------------------------------------------
class ResponseFormat(str, Enum):
    """Output format for tool responses."""
    MARKDOWN = "markdown"
    JSON = "json"


class ConflictsStatus(str, Enum):
    """Conflicts-check status, per the claude-for-legal matter-intake enum."""
    CLEARED = "cleared"
    PENDING = "pending"
    NOT_RUN = "not-run"
    WAIVED = "waived"


class MatterType(str, Enum):
    """Matter type, per the claude-for-legal matter-intake enum."""
    CONTRACT = "contract"
    EMPLOYMENT = "employment"
    IP = "ip"
    REGULATORY = "regulatory"
    INVESTIGATION = "investigation"
    PRODUCT = "product"
    OTHER = "other"


class OurRole(str, Enum):
    """The firm's/client's role, per the claude-for-legal matter-intake enum."""
    PLAINTIFF = "plaintiff"
    DEFENDANT = "defendant"
    CLAIMANT = "claimant"
    RESPONDENT = "respondent"
    INVESTIGATED = "investigated"


class MatterSource(str, Enum):
    """How the matter arrived. First six values follow claude-for-legal
    matter-intake; the last three are intake-desk extensions for
    consumer-facing inquiries."""
    DEMAND_LETTER = "demand-letter"
    COMPLAINT_SERVED = "complaint-served"
    SUBPOENA = "subpoena"
    REGULATOR_INQUIRY = "regulator-inquiry"
    INTERNAL_REPORT = "internal-report"
    PRE_SUIT_THREAT = "pre-suit-threat"
    REFERRAL = "referral"
    WEB_INQUIRY = "web-inquiry"
    PHONE_INQUIRY = "phone-inquiry"


class RiskBand(str, Enum):
    """Severity / likelihood band."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskRating(str, Enum):
    """Derived risk rating (severity x likelihood matrix)."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Materiality(str, Enum):
    """Materiality posture, per the claude-for-legal matter-intake enum."""
    RESERVED = "reserved"
    DISCLOSED = "disclosed"
    MONITORED = "monitored"
    NONE = "none"


# --------------------------------------------------------------------------
# Shared utilities
# --------------------------------------------------------------------------
@lru_cache(maxsize=None)
def _load_json(path: Path) -> Dict[str, Any]:
    """Load and cache a bundled JSON data file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _dataset_provenance(path: Path, record_id: Optional[str] = None) -> Dict[str, Any]:
    """Provenance block attached to every result that cites bundled data.

    Includes source, dataset version/as-of date, and a citation-ready record
    identifier (per the claude-for-legal CONNECTORS.md convention).
    """
    meta = _load_json(path).get("_dataset", {})
    return {
        "source": f"{path.name} (bundled FICTIONAL sample dataset)",
        "dataset_version": meta.get("version"),
        "dataset_as_of": meta.get("as_of"),
        "identifier": record_id,
    }


def handle_error(e: Exception) -> str:
    """Consistent, actionable error formatting across all tools."""
    if isinstance(e, FileNotFoundError):
        return (f"Error: bundled data file not found ({e}). "
                "Check that the data/ directory ships alongside server.py.")
    if isinstance(e, json.JSONDecodeError):
        return f"Error: bundled data file is not valid JSON ({e})."
    if isinstance(e, PermissionError):
        return f"Error: permission denied writing to the triage log ({e})."
    if isinstance(e, OSError):
        return f"Error: file system error ({e})."
    return f"Error: {type(e).__name__}: {e}"


def fmt(data: Any, rf: ResponseFormat = ResponseFormat.JSON) -> str:
    """Serialize a result. JSON is the default; markdown is a flat render."""
    if rf == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)
    if isinstance(data, dict):
        return "\n".join(f"**{k}**: {json.dumps(v, default=str) if isinstance(v, (dict, list)) else v}"
                         for k, v in data.items())
    return json.dumps(data, indent=2, default=str)


# --------------------------------------------------------------------------
# Deterministic core logic (pure functions — covered by unit tests)
# --------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def strip_legal_suffixes(normalized: str) -> str:
    """Remove trailing legal-entity suffixes from a normalized name."""
    tokens = normalized.split()
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) if tokens else normalized


def name_similarity(a: str, b: str) -> float:
    """Similarity in [0, 1] between two party names.

    Deterministic: max of (1) ratio on normalized strings, (2) ratio on
    suffix-stripped strings, (3) ratio on sorted suffix-stripped tokens.
    """
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    sa, sb = strip_legal_suffixes(na), strip_legal_suffixes(nb)
    ta, tb = " ".join(sorted(sa.split())), " ".join(sorted(sb.split()))
    return max(
        difflib.SequenceMatcher(None, na, nb).ratio(),
        difflib.SequenceMatcher(None, sa, sb).ratio(),
        difflib.SequenceMatcher(None, ta, tb).ratio(),
    )


def classify_match(score: float) -> Optional[str]:
    """Map a similarity score to a match strength (or None if below report threshold)."""
    if score >= EXACT_MATCH_THRESHOLD:
        return "exact"
    if score >= STRONG_MATCH_THRESHOLD:
        return "strong"
    if score >= POSSIBLE_MATCH_THRESHOLD:
        return "possible"
    return None


def screen_party_name(query: str, parties: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Screen one party name against the dataset. Returns matches sorted by
    score (desc), then record id (asc) for deterministic ordering."""
    matches = []
    for record in parties:
        score = name_similarity(query, record["name"])
        strength = classify_match(score)
        if strength is not None:
            matches.append({
                "record_id": record["id"],
                "matched_name": record["name"],
                "role": record["role"],
                "matter_id": record.get("matter_id"),
                "notes": record.get("notes"),
                "score": round(score, 3),
                "match_strength": strength,
            })
    matches.sort(key=lambda m: (-m["score"], m["record_id"]))
    return matches


def run_conflict_screen(party_names: List[str]) -> Dict[str, Any]:
    """Deterministic conflict screen over the bundled fictional dataset.

    Overall status semantics:
    - 'pending'  -> at least one name matched a dataset record; a human
                    conflicts review is required before engagement.
    - 'cleared'  -> no name matched anything in THIS dataset. This is a
                    screen-level clearance only, not a firm-wide clearance.
    The statuses 'not-run' and 'waived' are never produced by the screen
    itself; they exist for human determinations recorded via intake_log_triage.
    """
    parties = _load_json(PARTIES_PATH)["parties"]
    screened = []
    any_match = False
    for raw in party_names:
        matches = screen_party_name(raw, parties)
        if matches:
            any_match = True
        screened.append({
            "query": raw,
            "match_count": len(matches),
            "matches": matches,
        })
    status = ConflictsStatus.PENDING if any_match else ConflictsStatus.CLEARED
    return {
        "status": status.value,
        "status_semantics": {
            "cleared": "No hits in the bundled sample dataset. Screen-level only — not a firm-wide conflicts clearance.",
            "pending": "One or more potential matches found. A human conflicts review is required before engagement.",
            "not-run": "Not produced by this tool; recorded via intake_log_triage when no screen has happened.",
            "waived": "Not produced by this tool; recorded via intake_log_triage when a human documents a waiver.",
        },
        "screened_names": screened,
        "provenance": _dataset_provenance(PARTIES_PATH),
        "disclaimer": ("Illustrative screen against FICTIONAL sample data. "
                       "It does not replace your firm's conflicts clearance process."),
    }


def derive_risk_rating(severity: Optional[str], likelihood: Optional[str]) -> Optional[str]:
    """Severity x likelihood -> rating, or None if either band is missing."""
    if severity is None or likelihood is None:
        return None
    return RISK_MATRIX[(severity, likelihood)]


def build_followup_template(matter_type: str, missing_fields: List[str]) -> Dict[str, Any]:
    """Deterministic follow-up email template with merge slots. No LLM calls."""
    areas = _load_json(PRACTICE_AREAS_PATH)["practice_areas"]
    area_by_id = {a["id"].lower(): a for a in areas}
    key = matter_type.strip().lower()
    if key in area_by_id:
        matter_label = area_by_id[key]["name"]
    else:
        matter_label = re.sub(r"[-_]+", " ", key).strip() or "legal"

    questions = []
    for field_name in missing_fields:
        canonical = field_name.strip().lower()
        question = FIELD_QUESTIONS.get(
            canonical,
            f"Could you provide more detail on: {canonical.replace('_', ' ')}?",
        )
        questions.append({"field": canonical, "question": question})

    numbered = "\n".join(f"{i + 1}. {q['question']}" for i, q in enumerate(questions))
    body = (
        "Dear {{client_name}},\n"
        "\n"
        f"Thank you for contacting {{{{firm_name}}}} about your {matter_label.lower()} inquiry. "
        "To help our attorneys evaluate it, could you reply with the following information:\n"
        "\n"
        f"{numbered}\n"
        "\n"
        "Please note that contacting our office does not create an attorney-client "
        "relationship, and we cannot provide legal advice unless and until an engagement "
        "is confirmed. If anything is time-sensitive — for example, court papers you have "
        "received or an approaching deadline — please say so in your reply.\n"
        "\n"
        "Kind regards,\n"
        "{{sender_name}}\n"
        "{{firm_name}}"
    )
    return {
        "matter_type": matter_type,
        "matter_label": matter_label,
        "subject_template": f"Following up on your {matter_label.lower()} inquiry — a few quick questions",
        "body_template": body,
        "merge_slots": ["{{client_name}}", "{{firm_name}}", "{{sender_name}}"],
        "questions": questions,
        "note": "Deterministic template with merge slots. The MCP client (or a human) fills the slots; this server makes no LLM calls.",
    }


def conflicts_gate_error(status: str, override_by: Optional[str],
                         override_rationale: Optional[str]) -> Optional[str]:
    """The conflicts gate (per claude-for-legal matter-intake): logging a
    triage record with conflicts status 'not-run' is a hard STOP unless an
    explicit, attributed override is provided. Returns an error string when
    the gate blocks, else None."""
    if status != ConflictsStatus.NOT_RUN.value:
        return None
    if override_by and override_rationale:
        return None
    return (
        "Error: conflicts gate — conflicts_status is 'not-run', so this intake "
        "cannot be logged. Do not proceed silently. Choose one: "
        "(1) run intake_check_conflicts on the involved parties and log with the "
        "resulting status; "
        "(2) log with conflicts_status='pending' once a named person is running the "
        "check; or "
        "(3) bypass explicitly by providing BOTH conflicts_override_by and "
        "conflicts_override_rationale — the override is recorded permanently in the "
        "log row."
    )


def triage_log_path() -> Path:
    """Resolve the triage log path (env override, else ./triage_log.jsonl)."""
    return Path(os.environ.get(LOG_PATH_ENV_VAR, DEFAULT_LOG_PATH))


def append_triage_row(row: Dict[str, Any], path: Path) -> int:
    """Append one JSON line to the triage log. Returns the 1-based entry number.

    Append-only: never rewrites or deletes existing lines.
    """
    existing = 0
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            existing = sum(1 for line in f if line.strip())
    row = dict(row, entry_number=existing + 1)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return existing + 1


# --------------------------------------------------------------------------
# MCP server + input models
# --------------------------------------------------------------------------
mcp = FastMCP("intake_triage_mcp")

_STRICT = ConfigDict(str_strip_whitespace=True, validate_assignment=True, extra="forbid")


class ListPracticeAreasInput(BaseModel):
    """Input model for listing practice areas."""
    model_config = _STRICT

    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'json' (default, machine-readable) or 'markdown' (human-readable).",
    )


class CheckConflictsInput(BaseModel):
    """Input model for the conflict screen."""
    model_config = _STRICT

    party_names: List[str] = Field(
        ...,
        description="Party names to screen, e.g. ['Jane Doe', 'Acme Widgets LLC']. "
                    "Include every person and organization mentioned in the inquiry.",
        min_length=1,
        max_length=25,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'json' (default) or 'markdown'.",
    )

    @field_validator("party_names")
    @classmethod
    def names_not_blank(cls, v: List[str]) -> List[str]:
        cleaned = [n.strip() for n in v if n and n.strip()]
        if not cleaned:
            raise ValueError("party_names must contain at least one non-empty name")
        if any(len(name) > 200 for name in cleaned):
            raise ValueError("each party name must be 200 characters or fewer")
        return cleaned


class MatterInput(BaseModel):
    """Input model for matter validation. Field set is modeled on the
    claude-for-legal matter-intake skill: identification, source, risk
    triage, materiality, and key dates. Only matter_name is required —
    the tool's job is to report what is missing."""
    model_config = _STRICT

    # Identification
    matter_name: str = Field(..., description="Matter name as commonly referenced, e.g. 'Quill v. Voss 2026'.",
                             min_length=1, max_length=200)
    counterparty: Optional[str] = Field(default=None, description="Opposing party, if known.", max_length=200)
    matter_type: Optional[MatterType] = Field(
        default=None,
        description="Matter type: contract | employment | ip | regulatory | investigation | product | other.")
    our_role: Optional[OurRole] = Field(
        default=None,
        description="Prospective client's role: plaintiff | defendant | claimant | respondent | investigated.")
    jurisdiction: Optional[str] = Field(default=None, description="Court, forum, or regulator, e.g. 'N.D. Cal.'.",
                                        max_length=200)
    practice_area: Optional[str] = Field(
        default=None,
        description="Practice-area id from intake_list_practice_areas, e.g. 'personal-injury'.",
        max_length=100)

    # Source
    source: Optional[MatterSource] = Field(
        default=None,
        description="How the matter arrived: demand-letter | complaint-served | subpoena | regulator-inquiry | "
                    "internal-report | pre-suit-threat | referral | web-inquiry | phone-inquiry.")

    # Conflicts
    conflicts_status: Optional[ConflictsStatus] = Field(
        default=None,
        description="Conflicts status if a check has been run or recorded: cleared | pending | not-run | waived. "
                    "Defaults to 'not-run' in the normalized record when omitted.")

    # Risk triage
    severity: Optional[RiskBand] = Field(default=None, description="Severity band: high | medium | low.")
    likelihood: Optional[RiskBand] = Field(default=None, description="Likelihood band: high | medium | low.")

    # Materiality
    materiality: Optional[Materiality] = Field(
        default=None, description="Materiality posture: reserved | disclosed | monitored | none.")

    # Key dates (ISO YYYY-MM-DD)
    response_deadline: Optional[str] = Field(default=None, description="Response deadline, YYYY-MM-DD.")
    next_hearing: Optional[str] = Field(default=None, description="Next hearing or conference date, YYYY-MM-DD.")
    statute_of_limitations: Optional[str] = Field(default=None,
                                                  description="Statute-of-limitations cutoff, YYYY-MM-DD.")

    # Narrative
    summary: Optional[str] = Field(default=None, description="One-paragraph factual summary of the inquiry.",
                                   max_length=4000)

    @field_validator("response_deadline", "next_hearing", "statute_of_limitations")
    @classmethod
    def valid_iso_date(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            datetime.strptime(v, DATE_FORMAT)
        except ValueError as exc:
            raise ValueError(f"date must be in YYYY-MM-DD format, got '{v}'") from exc
        return v


class DraftFollowupInput(BaseModel):
    """Input model for the deterministic follow-up email template."""
    model_config = _STRICT

    matter_type: str = Field(
        ...,
        description="Practice-area id from intake_list_practice_areas (e.g. 'personal-injury') or a free-form "
                    "matter type (e.g. 'contract'). Used only to label the template.",
        min_length=1, max_length=100)
    missing_fields: List[str] = Field(
        ...,
        description="Intake fields still missing, e.g. ['incident_date', 'insurance_carrier']. Each becomes one "
                    "question in the template (canonical phrasing for known fields, generic otherwise).",
        min_length=1, max_length=20)

    @field_validator("missing_fields")
    @classmethod
    def fields_not_blank(cls, v: List[str]) -> List[str]:
        cleaned = [f.strip() for f in v if f and f.strip()]
        if not cleaned:
            raise ValueError("missing_fields must contain at least one non-empty field name")
        if any(len(field_name) > 100 for field_name in cleaned):
            raise ValueError("each missing field name must be 100 characters or fewer")
        return cleaned


class LogTriageInput(BaseModel):
    """Input model for appending a triage record to the local JSONL log."""
    model_config = _STRICT

    matter_name: str = Field(..., description="Matter name for the log row.", min_length=1, max_length=200)
    conflicts_status: ConflictsStatus = Field(
        ...,
        description="Conflicts status for the record: cleared | pending | not-run | waived. "
                    "'not-run' is REFUSED unless an explicit override (by + rationale) is provided.")
    practice_area: Optional[str] = Field(default=None, description="Practice-area id, if known.", max_length=100)
    risk_rating: Optional[RiskRating] = Field(
        default=None, description="Derived risk rating: critical | high | medium | low.")
    parties_checked: Optional[List[str]] = Field(
        default=None, description="Party names that were screened for conflicts.", max_length=25)
    summary: Optional[str] = Field(default=None, description="One-line triage summary for the log.", max_length=1000)
    conflicts_override_by: Optional[str] = Field(
        default=None, description="Name of the person explicitly bypassing the conflicts gate (only with 'not-run').",
        max_length=200)
    conflicts_override_rationale: Optional[str] = Field(
        default=None, description="Documented rationale for bypassing the conflicts gate (only with 'not-run').",
        max_length=1000)

    @field_validator("parties_checked")
    @classmethod
    def checked_parties_are_bounded(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        cleaned = [name.strip() for name in v if name and name.strip()]
        if len(cleaned) != len(v):
            raise ValueError("parties_checked cannot contain blank names")
        if any(len(name) > 200 for name in cleaned):
            raise ValueError("each checked party name must be 200 characters or fewer")
        return cleaned

    @model_validator(mode="after")
    def override_fields_together(self) -> "LogTriageInput":
        if bool(self.conflicts_override_by) != bool(self.conflicts_override_rationale):
            raise ValueError(
                "conflicts_override_by and conflicts_override_rationale must be provided together")
        if self.conflicts_override_by and self.conflicts_status != ConflictsStatus.NOT_RUN:
            raise ValueError(
                "conflicts override fields are only valid when conflicts_status='not-run'")
        return self


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool(
    name="intake_list_practice_areas",
    annotations={
        "title": "List Practice Areas",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def intake_list_practice_areas(params: ListPracticeAreasInput) -> str:
    """List the practice areas the intake desk can triage into, from the bundled
    fictional sample dataset.

    Use this first to learn the valid practice-area ids and the core intake
    fields each area expects, then reference those ids in intake_validate_matter
    and intake_draft_followup. Read-only; never modifies anything.

    Args:
        params (ListPracticeAreasInput): Validated input containing:
            - response_format (ResponseFormat): 'json' (default) or 'markdown'.

    Returns:
        str: JSON-formatted string (or markdown) with this schema:

        {
            "count": int,                  # number of practice areas
            "practice_areas": [
                {
                    "id": str,             # stable id, e.g. "personal-injury"
                    "name": str,           # display name, e.g. "Personal Injury"
                    "description": str,    # what the area covers
                    "typical_matter_types": [str],   # claude-for-legal matter_type values
                    "core_intake_fields": [str]      # fields the intake desk asks for
                }
            ],
            "provenance": {"source": str, "dataset_version": str, "dataset_as_of": str, "identifier": null},
            "disclaimer": str              # fictional-data notice
        }

    Examples:
        - Use when: "What kinds of cases does the firm handle?" -> call with defaults.
        - Use when: choosing a practice_area id before intake_validate_matter.
        - Don't use when: you need to screen names for conflicts (use intake_check_conflicts).

    Error Handling:
        - Returns "Error: bundled data file not found ..." if data/practice_areas.json is missing.
    """
    try:
        data = _load_json(PRACTICE_AREAS_PATH)
        areas = data["practice_areas"]
        result = {
            "count": len(areas),
            "practice_areas": areas,
            "provenance": _dataset_provenance(PRACTICE_AREAS_PATH),
            "disclaimer": data["_dataset"]["notice"],
        }
        if params.response_format == ResponseFormat.MARKDOWN:
            lines = [f"# Practice Areas ({len(areas)})", ""]
            for a in areas:
                lines.append(f"## {a['name']} (`{a['id']}`)")
                lines.append(a["description"])
                lines.append(f"- Typical matter types: {', '.join(a['typical_matter_types'])}")
                lines.append(f"- Core intake fields: {', '.join(a['core_intake_fields'])}")
                lines.append("")
            lines.append(f"_{result['disclaimer']}_")
            return "\n".join(lines)
        return fmt(result)
    except Exception as e:
        logger.exception("intake_list_practice_areas failed")
        return handle_error(e)


@mcp.tool(
    name="intake_check_conflicts",
    annotations={
        "title": "Screen Party Names for Conflicts",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def intake_check_conflicts(params: CheckConflictsInput) -> str:
    """Screen party names against the bundled (fictional) conflicts dataset using
    deterministic fuzzy matching.

    Matching normalizes case/punctuation, ignores legal-entity suffixes
    (LLC, Inc, Co, ...), and compares sorted tokens, so 'Northgate Assurance Co'
    matches 'Northgate Assurance Company'. Every match carries provenance
    (source, dataset version/as-of date, record identifier). Read-only.

    Status semantics (claude-for-legal enum: cleared | pending | not-run | waived):
        - 'pending': at least one potential match found -> human conflicts review required.
        - 'cleared': no hits in THIS sample dataset -> screen-level only, NOT a
          firm-wide conflicts clearance.
        - 'not-run' / 'waived': never produced by this tool; they are human
          determinations recorded via intake_log_triage.

    Args:
        params (CheckConflictsInput): Validated input containing:
            - party_names (List[str]): 1-25 names of people/organizations to screen.
            - response_format (ResponseFormat): 'json' (default) or 'markdown'.

    Returns:
        str: JSON-formatted string (or markdown) with this schema:

        {
            "status": str,                       # "pending" or "cleared"
            "status_semantics": {str: str},      # what each enum value means
            "screened_names": [
                {
                    "query": str,                # name as screened
                    "match_count": int,
                    "matches": [                 # sorted by score desc, record_id asc
                        {
                            "record_id": str,    # e.g. "P-0003"
                            "matched_name": str,
                            "role": str,         # client | adverse_party | related_entity
                            "matter_id": str,    # e.g. "M-2022-008"
                            "notes": str,
                            "score": float,      # 0-1 similarity
                            "match_strength": str  # exact | strong | possible
                        }
                    ]
                }
            ],
            "provenance": {"source": str, "dataset_version": str, "dataset_as_of": str, "identifier": null},
            "disclaimer": str
        }

    Examples:
        - Use when: "New inquiry from Jane Doe against Acme Widgets LLC" ->
          party_names=["Jane Doe", "Acme Widgets LLC"].
        - Use when: re-screening after the client names additional parties.
        - Don't use when: recording a human conflicts decision (use intake_log_triage).

    Error Handling:
        - Pydantic rejects empty party_names lists and blank names.
        - Returns "Error: bundled data file not found ..." if data/parties.json is missing.
    """
    try:
        result = run_conflict_screen(params.party_names)
        if params.response_format == ResponseFormat.MARKDOWN:
            lines = [f"# Conflict Screen — status: **{result['status']}**", ""]
            for s in result["screened_names"]:
                lines.append(f"## {s['query']} — {s['match_count']} match(es)")
                for m in s["matches"]:
                    lines.append(f"- {m['matched_name']} ({m['record_id']}), role: {m['role']}, "
                                 f"matter: {m['matter_id']}, strength: {m['match_strength']} ({m['score']})")
                lines.append("")
            lines.append(f"_{result['disclaimer']}_")
            return "\n".join(lines)
        return fmt(result)
    except Exception as e:
        logger.exception("intake_check_conflicts failed")
        return handle_error(e)


@mcp.tool(
    name="intake_validate_matter",
    annotations={
        "title": "Validate and Normalize a Matter Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def intake_validate_matter(params: MatterInput) -> str:
    """Validate a structured matter summary and return a normalized intake record
    plus a list of missing recommended fields.

    The field set is modeled on the claude-for-legal matter-intake skill
    (identification / source / risk triage / materiality / key dates). The
    client model extracts these fields from the raw inquiry; this tool
    deterministically validates enums and dates, derives the risk rating from
    the severity x likelihood matrix, and defaults conflicts to 'not-run'
    when no status is supplied. Pure computation — stores nothing.

    Risk matrix (severity, likelihood) -> rating:
        high+high=critical; high+medium / medium+high = high;
        high+low / low+high / medium+medium = medium; all remaining = low.

    Args:
        params (MatterInput): Validated input containing:
            - matter_name (str, required): e.g. 'Quill v. Voss 2026'.
            - counterparty, jurisdiction, practice_area, summary (Optional[str]).
            - matter_type: contract | employment | ip | regulatory | investigation | product | other.
            - our_role: plaintiff | defendant | claimant | respondent | investigated.
            - source: demand-letter | complaint-served | subpoena | regulator-inquiry |
              internal-report | pre-suit-threat | referral | web-inquiry | phone-inquiry.
            - conflicts_status: cleared | pending | not-run | waived (defaults to 'not-run').
            - severity, likelihood: high | medium | low.
            - materiality: reserved | disclosed | monitored | none.
            - response_deadline, next_hearing, statute_of_limitations: 'YYYY-MM-DD' strings.

    Returns:
        str: JSON-formatted string with this schema:

        {
            "valid": true,
            "normalized": {
                "identification": {"matter_name": str, "counterparty": str|null, "matter_type": str|null,
                                    "our_role": str|null, "jurisdiction": str|null, "practice_area": str|null},
                "source": str|null,
                "conflicts": {"status": str, "note": str},
                "risk_triage": {"severity": str|null, "likelihood": str|null, "risk_rating": str|null},
                "materiality": str|null,
                "key_dates": {"response_deadline": str|null, "next_hearing": str|null,
                               "statute_of_limitations": str|null},
                "summary": str|null
            },
            "missing_recommended_fields": [str],   # subset of: counterparty, matter_type, our_role,
                                                   # jurisdiction, practice_area, source, severity,
                                                   # likelihood, response_deadline
            "warnings": [str],                     # e.g. unknown practice_area id, conflicts gate notice
            "disclaimer": str
        }

        Invalid enum values, malformed dates, or unexpected fields are rejected
        by Pydantic before this tool runs (the client sees a validation error).

    Examples:
        - Use when: you have extracted structure from a raw inquiry and need a
          normalized record plus the gaps to chase (feed missing fields to
          intake_draft_followup).
        - Don't use when: you only need a conflicts screen (use intake_check_conflicts).

    Error Handling:
        - Enum/date/extra-field errors are raised as Pydantic validation errors.
        - Returns "Error: ..." strings for unexpected internal failures.
    """
    try:
        warnings: List[str] = []

        practice_area = params.practice_area
        if practice_area is not None:
            valid_ids = {a["id"] for a in _load_json(PRACTICE_AREAS_PATH)["practice_areas"]}
            if practice_area.lower() in valid_ids:
                practice_area = practice_area.lower()
            else:
                warnings.append(
                    f"Unknown practice_area '{practice_area}'. Valid ids: {', '.join(sorted(valid_ids))}.")

        conflicts_status = params.conflicts_status or ConflictsStatus.NOT_RUN
        if conflicts_status == ConflictsStatus.NOT_RUN:
            warnings.append(
                "Conflicts gate: conflicts status is 'not-run'. Run intake_check_conflicts on all parties "
                "before logging this intake — intake_log_triage will refuse 'not-run' without an explicit override.")

        risk_rating = derive_risk_rating(
            params.severity.value if params.severity else None,
            params.likelihood.value if params.likelihood else None,
        )

        provided = {
            "counterparty": params.counterparty,
            "matter_type": params.matter_type,
            "our_role": params.our_role,
            "jurisdiction": params.jurisdiction,
            "practice_area": params.practice_area,
            "source": params.source,
            "severity": params.severity,
            "likelihood": params.likelihood,
            "response_deadline": params.response_deadline,
        }
        missing = [f for f in RECOMMENDED_FIELDS if provided.get(f) is None]

        result = {
            "valid": True,
            "normalized": {
                "identification": {
                    "matter_name": params.matter_name,
                    "counterparty": params.counterparty,
                    "matter_type": params.matter_type.value if params.matter_type else None,
                    "our_role": params.our_role.value if params.our_role else None,
                    "jurisdiction": params.jurisdiction,
                    "practice_area": practice_area,
                },
                "source": params.source.value if params.source else None,
                "conflicts": {
                    "status": conflicts_status.value,
                    "note": "Defaulted to 'not-run' because no status was supplied."
                            if params.conflicts_status is None else "Status supplied by caller.",
                },
                "risk_triage": {
                    "severity": params.severity.value if params.severity else None,
                    "likelihood": params.likelihood.value if params.likelihood else None,
                    "risk_rating": risk_rating,
                },
                "materiality": params.materiality.value if params.materiality else None,
                "key_dates": {
                    "response_deadline": params.response_deadline,
                    "next_hearing": params.next_hearing,
                    "statute_of_limitations": params.statute_of_limitations,
                },
                "summary": params.summary,
            },
            "missing_recommended_fields": missing,
            "warnings": warnings,
            "disclaimer": "Triage support only — not legal advice. An attorney owns every decision.",
        }
        return fmt(result)
    except Exception as e:
        logger.exception("intake_validate_matter failed")
        return handle_error(e)


@mcp.tool(
    name="intake_draft_followup",
    annotations={
        "title": "Draft Follow-up Email Template",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def intake_draft_followup(params: DraftFollowupInput) -> str:
    """Build a deterministic follow-up email TEMPLATE asking a prospective client
    for missing intake information. Same inputs always produce the same template;
    no LLM calls, no sending, no storage.

    Each missing field becomes one numbered question: known fields (e.g.
    'incident_date', 'insurance_carrier') get canonical phrasing; unknown
    fields get a deterministic generic phrasing. The template keeps
    {{client_name}}, {{firm_name}}, and {{sender_name}} as merge slots and
    includes a no-attorney-client-relationship notice.

    Args:
        params (DraftFollowupInput): Validated input containing:
            - matter_type (str): practice-area id (e.g. 'personal-injury') or free-form
              matter type; used only to label the subject/body.
            - missing_fields (List[str]): 1-20 field names, e.g. from
              intake_validate_matter's missing_recommended_fields.

    Returns:
        str: JSON-formatted string with this schema:

        {
            "matter_type": str,            # echo of input
            "matter_label": str,           # display label, e.g. "Personal Injury"
            "subject_template": str,
            "body_template": str,          # multi-line, contains merge slots
            "merge_slots": ["{{client_name}}", "{{firm_name}}", "{{sender_name}}"],
            "questions": [{"field": str, "question": str}],
            "note": str
        }

    Examples:
        - Use when: intake_validate_matter reported missing_recommended_fields and
          you need an email to request them -> matter_type='personal-injury',
          missing_fields=['incident_date', 'insurance_carrier'].
        - Don't use when: you need free-form prose tailored to the inquiry — that is
          the client model's job, using this template as the skeleton.

    Error Handling:
        - Pydantic rejects empty matter_type and empty missing_fields.
        - Returns "Error: ..." strings for unexpected internal failures.
    """
    try:
        return fmt(build_followup_template(params.matter_type, params.missing_fields))
    except Exception as e:
        logger.exception("intake_draft_followup failed")
        return handle_error(e)


@mcp.tool(
    name="intake_log_triage",
    annotations={
        "title": "Append Triage Record to Local Log",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def intake_log_triage(params: LogTriageInput) -> str:
    """Append one triage record to a local JSONL log file. WRITE tool — appends a
    line to the log; it never overwrites, edits, or deletes existing entries,
    and it touches nothing outside that one file.

    The log path is $INTAKE_TRIAGE_LOG_PATH, defaulting to ./triage_log.jsonl.
    Each row gets a 1-based entry_number and a UTC timestamp.

    CONFLICTS GATE (hard stop, per the claude-for-legal matter-intake skill):
    conflicts_status='not-run' is REFUSED unless BOTH conflicts_override_by and
    conflicts_override_rationale are provided. An override is recorded
    permanently in the log row. Acceptable alternatives: run
    intake_check_conflicts first, or log as 'pending' with a named owner.

    Args:
        params (LogTriageInput): Validated input containing:
            - matter_name (str, required): matter name for the row.
            - conflicts_status (ConflictsStatus, required): cleared | pending | not-run | waived.
            - practice_area (Optional[str]), risk_rating (Optional: critical|high|medium|low),
              parties_checked (Optional[List[str]]), summary (Optional[str]).
            - conflicts_override_by / conflicts_override_rationale (Optional[str]): both or neither.

    Returns:
        str: JSON-formatted string with this schema:

        Success:
        {
            "logged": true,
            "log_path": str,
            "entry_number": int,           # 1-based line number in the JSONL log
            "row": {
                "entry_number": int,
                "logged_at": str,          # UTC ISO-8601 timestamp
                "matter_name": str,
                "practice_area": str|null,
                "conflicts": {"status": str, "override": {"by": str, "rationale": str}|null},
                "risk_rating": str|null,
                "parties_checked": [str]|null,
                "summary": str|null
            }
        }

        Refusal (conflicts gate):
        "Error: conflicts gate — conflicts_status is 'not-run' ..." (with the three
        acceptable paths spelled out; nothing is written).

    Examples:
        - Use when: triage is complete and conflicts were screened ->
          conflicts_status='pending' (or 'cleared'), parties_checked=[...].
        - Use when: a human explicitly bypasses the gate -> conflicts_status='not-run',
          conflicts_override_by='K. Patel', conflicts_override_rationale='...'.
        - Don't use when: conflicts have not been addressed at all — run
          intake_check_conflicts first.

    Error Handling:
        - Gate refusal returns an "Error: conflicts gate ..." string and writes nothing.
        - Returns "Error: permission denied ..." / "Error: file system error ..." on I/O failures.
    """
    try:
        gate = conflicts_gate_error(
            params.conflicts_status.value,
            params.conflicts_override_by,
            params.conflicts_override_rationale,
        )
        if gate:
            logger.warning("conflicts gate refused log for matter %r", params.matter_name)
            return gate

        override = None
        if params.conflicts_status == ConflictsStatus.NOT_RUN:
            override = {"by": params.conflicts_override_by,
                        "rationale": params.conflicts_override_rationale}

        row = {
            "logged_at": datetime.now(timezone.utc).isoformat(),
            "matter_name": params.matter_name,
            "practice_area": params.practice_area,
            "conflicts": {"status": params.conflicts_status.value, "override": override},
            "risk_rating": params.risk_rating.value if params.risk_rating else None,
            "parties_checked": params.parties_checked,
            "summary": params.summary,
        }
        path = triage_log_path()
        entry_number = append_triage_row(row, path)
        row["entry_number"] = entry_number
        logger.info("logged triage entry %d to %s", entry_number, path)
        return fmt({"logged": True, "log_path": str(path), "entry_number": entry_number, "row": row})
    except Exception as e:
        logger.exception("intake_log_triage failed")
        return handle_error(e)


if __name__ == "__main__":
    mcp.run()  # stdio transport by default
