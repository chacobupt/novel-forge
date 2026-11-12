#!/usr/bin/env python3
"""Few-shot Reference Guarantee v1 integrity and dispatch checks."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


SELECTION_KINDS = {"STRICT_MATCH", "PARTIAL_REFERENCE", "NEAREST_REFERENCE", "CORPUS_REFERENCE", "LEGACY_REFERENCE"}
COMPATIBILITY_SCAN_LIMIT = 40
COMPATIBILITY_TIER_LIMITS = {"T1": 8, "T2": 8, "T3": 8, "T4_T5": 16}


class ReferenceGuaranteeError(ValueError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ReferenceGuaranteeError(message)


def normalize_boundaries(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def normalize_reference(row: dict[str, Any], focus_ids: list[str] | None = None) -> dict[str, Any]:
    window = row.get("window") or row.get("source_window") or row.get("full_window") or row.get("complete_window")
    _require(isinstance(window, str) and window.strip(), "REFERENCE_WINDOW_MISSING")
    actual_sha = sha256_text(window)
    expected_sha = row.get("raw_excerpt_sha256") or actual_sha
    _require(expected_sha == actual_sha, f"REFERENCE_SHA_MISMATCH:{row.get('example_id')}")
    bindings = list(dict.fromkeys(row.get("focus_bindings") or focus_ids or ([row["focus_id"]] if row.get("focus_id") else [])))
    normalized = {
        "example_id": row["example_id"],
        "role": row.get("role", "structure"),
        "allowed_capability": row.get("allowed_capability") or row.get("capability") or "仅迁移所示交流机制与自然表达",
        "do_not_transfer": normalize_boundaries(row.get("do_not_transfer", row.get("forbid", []))),
        "window": window,
        "raw_excerpt_sha256": actual_sha,
        "corpus_polarity": row.get("corpus_polarity", "正例"),
        "audit_verdict": row.get("audit_verdict", row.get("audit_result", "UNJUDGED")),
        "selection_kind": row.get("selection_kind", "LEGACY_REFERENCE"),
        "focus_bindings": bindings,
    }
    if "structure_transfer" in row:
        normalized["structure_transfer"] = bool(row["structure_transfer"])
    if row.get("selection_reason"):
        normalized["selection_reason"] = row["selection_reason"]
    return normalized


def _compatibility_tier(row: dict[str, Any]) -> str:
    kind = row.get("selection_kind")
    if kind == "STRICT_MATCH" and row.get("audit_verdict") == "MATCH":
        return "T1"
    if kind == "PARTIAL_REFERENCE":
        return "T2"
    if kind == "NEAREST_REFERENCE":
        return "T3"
    if row.get("surface_orality_candidate") is True or row.get("role") in {"surface", "surface_orality"}:
        return "T4"
    if kind in {"CORPUS_REFERENCE", "LEGACY_REFERENCE"}:
        return "T5"
    raise ReferenceGuaranteeError(f"REFERENCE_COMPATIBILITY_TIER_UNKNOWN:{row.get('example_id')}")


def select_compatibility_aware_reference(candidates: list[dict[str, Any]], focus_id: str,
                                         max_unique_windows: int = COMPATIBILITY_SCAN_LIMIT,
                                         review_resolver=None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Select the first literary-compatible reference without weakening the guarantee.

    Candidates are already in semantic order. Literary decisions are evidence supplied by
    the compatibility stage; this selector neither invents nor reruns those decisions.
    """
    _require(max_unique_windows == COMPATIBILITY_SCAN_LIMIT, "REFERENCE_COMPATIBILITY_BUDGET_DRIFT")
    grouped = {tier: [] for tier in ("T1", "T2", "T3", "T4", "T5")}
    seen_input: set[str] = set()
    for raw in candidates:
        row = normalize_reference(raw, [focus_id])
        sha = row["raw_excerpt_sha256"]
        if sha in seen_input:
            continue
        seen_input.add(sha)
        merged = {**raw, **row, "literary_compatibility": raw.get("literary_compatibility")}
        grouped[_compatibility_tier(merged)].append(merged)

    scanned, scanned_windows = [], set()
    tier_limits = {"T1": 8, "T2": 8, "T3": 8, "T4": 16, "T5": 16}
    tail_used = 0
    for tier in ("T1", "T2", "T3", "T4", "T5"):
        for row in grouped[tier]:
            if len(scanned_windows) >= max_unique_windows:
                break
            if tier in {"T4", "T5"} and tail_used >= COMPATIBILITY_TIER_LIMITS["T4_T5"]:
                break
            if sum(1 for item in scanned if item["tier"] == tier) >= tier_limits[tier]:
                break
            sha = row["raw_excerpt_sha256"]
            if sha in scanned_windows:
                continue
            scanned_windows.add(sha)
            if tier in {"T4", "T5"}:
                tail_used += 1
            literary = row.get("literary_compatibility") or {}
            if not literary and review_resolver is not None:
                literary = review_resolver(row, tier, max_unique_windows - len(scanned_windows) + 1)
            decision = literary.get("decision", "MISSING")
            scanned.append({"scan_index": len(scanned_windows), "tier": tier,
                            "example_id": row["example_id"], "raw_excerpt_sha256": sha,
                            "literary_decision": decision, "reason": literary.get("reason", "")})
            if decision != "ALLOW":
                continue
            selected = dict(row)
            selected["focus_bindings"] = [focus_id]
            selected["selection_reason"] = (
                f"{tier}: semantic order retained; complete window passed Literary Compatibility")
            selected["literary_compatibility"] = literary
            literary_surface_only = (
                literary.get("final_decision") == "SURFACE_ONLY"
                or literary.get("structural_conflict") is True
            )
            if tier in {"T4", "T5"} or literary_surface_only:
                capabilities = literary.get("surface_capabilities")
                if isinstance(capabilities, dict):
                    capabilities = capabilities.get("allowed", [])
                selected.update({
                    "role": "surface",
                    "structure_transfer": False,
                    "allowed_capability": "；".join(capabilities or [])
                        or row.get("surface_allowed_capability")
                        or "仅学习自然表达、交流节奏与互动质感；不迁移结构",
                })
            else:
                selected["structure_transfer"] = row.get("structure_transfer", True)
            receipt = {"status": "PASS", "focus_id": focus_id,
                       "selected_example_id": selected["example_id"], "selected_tier": tier,
                       "unique_windows_scanned": len(scanned_windows), "max_unique_windows": max_unique_windows,
                       "tier_limits": COMPATIBILITY_TIER_LIMITS, "scan": scanned}
            return selected, receipt
        if len(scanned_windows) >= max_unique_windows:
            break
    raise ReferenceGuaranteeError(f"REFERENCE_COMPATIBILITY_EXHAUSTED:{focus_id}")


def validate_references(references: list[dict[str, Any]], required_focus_ids: list[str], *, allow_no_focus: bool = False) -> None:
    _require(bool(references) or (allow_no_focus and not required_focus_ids), "WRITER_CALL_HAS_NO_REFERENCE")
    covered: set[str] = set()
    for raw in references:
        row = normalize_reference(raw)
        _require(row["corpus_polarity"] == "正例", f"NON_POSITIVE_REFERENCE:{row['example_id']}")
        _require(row["selection_kind"] in SELECTION_KINDS, f"UNKNOWN_SELECTION_KIND:{row['selection_kind']}")
        if row["selection_kind"] == "STRICT_MATCH":
            _require(row["audit_verdict"] == "MATCH", f"STRICT_WITHOUT_MATCH:{row['example_id']}")
        if row["selection_kind"] != "STRICT_MATCH":
            _require(row["audit_verdict"] != "MATCH" or row["selection_kind"] == "LEGACY_REFERENCE",
                     f"MATCH_MISLABELLED_AS_FALLBACK:{row['example_id']}")
        covered.update(row["focus_bindings"])
    missing = sorted(set(required_focus_ids) - covered)
    _require(not missing, "REQUIRED_FOCUS_REFERENCE_MISSING:" + ",".join(missing))


def validate_active_sources(references: list[dict[str, Any]], project_root: Path) -> dict[str, Any]:
    pointer = json.loads((project_root / "00_系统规则/dialogue_fewshot_active_data.json").read_text(encoding="utf-8"))
    corpus_path = project_root / pointer["corpus"]
    annotation_path = project_root / pointer["annotations"]
    corpus = {row["scene_id"]: row for row in
              (json.loads(line) for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip())}
    annotations = {row["scene_id"]: row for row in
                   (json.loads(line) for line in annotation_path.read_text(encoding="utf-8").splitlines() if line.strip())}
    checked = []
    for raw in references:
        row = normalize_reference(raw)
        source, annotation = corpus.get(row["example_id"]), annotations.get(row["example_id"])
        _require(source is not None and annotation is not None, f"REFERENCE_NOT_IN_ACTIVE_DATA:{row['example_id']}")
        _require(source.get("raw_excerpt") == row["window"], f"REFERENCE_SOURCE_TEXT_MISMATCH:{row['example_id']}")
        _require(annotation.get("raw_excerpt_sha256") == row["raw_excerpt_sha256"], f"REFERENCE_SOURCE_SHA_MISMATCH:{row['example_id']}")
        eligible = annotation.get("fewshot_eligibility", {}).get("eligible") is True
        capabilities = annotation.get("example_capabilities", {})
        role_key = "structural" if row["role"] in {"structure", "structural_positive"} else "surface_orality"
        _require(annotation.get("polarity") == "正例" and eligible and annotation.get("needs_human_review") is False,
                 f"REFERENCE_NOT_PRODUCTION_POSITIVE:{row['example_id']}")
        _require(capabilities.get(role_key) is True, f"REFERENCE_ROLE_NOT_APPROVED:{row['example_id']}:{role_key}")
        checked.append(row["example_id"])
    return {"status": "PASS", "checked_example_ids": checked,
            "corpus_sha256": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            "annotations_sha256": hashlib.sha256(annotation_path.read_bytes()).hexdigest()}


def validate_actual_task_message(message: str, references: list[dict[str, Any]], required_focus_ids: list[str], *, allow_no_focus: bool = False) -> dict[str, Any]:
    validate_references(references, required_focus_ids, allow_no_focus=allow_no_focus)
    seen_windows: set[str] = set()
    bindings: dict[str, list[str]] = {}
    for raw in references:
        row = normalize_reference(raw)
        window = row["window"]
        _require(message.count(window) >= 1, f"REFERENCE_NOT_IN_ACTUAL_MESSAGE:{row['example_id']}")
        _require(window not in seen_windows, f"DUPLICATE_REFERENCE_WINDOW_IN_BINDINGS:{row['raw_excerpt_sha256']}")
        seen_windows.add(window)
        bindings[row["raw_excerpt_sha256"]] = row["focus_bindings"]
    _require("None" not in message, "RENDERED_NONE_REFERENCE")
    return {
        "status": "PASS",
        "actual_task_message_sha256": sha256_text(message),
        "required_focus_ids": required_focus_ids,
        "delivered_unique_reference_count": len(seen_windows),
        "reference_focus_bindings": bindings,
    }


def detect_missing_retrieval_events(plan: dict[str, Any], executed_event_ids: list[str]) -> list[str]:
    executed = set(executed_event_ids)
    return [row["event_id"] for row in plan.get("events", [])
            if row.get("needs_dialogue_retrieval") is True
            and row.get("scene_treatment", {}).get("disposition") not in {"MERGE", "REMOVE"}
            and row["event_id"] not in executed]


def write_call_receipt(message_path: Path, output_path: Path, references: list[dict[str, Any]],
                       required_focus_ids: list[str], model: str, reasoning_effort: str,
                       request_id: str = "UNAVAILABLE") -> dict[str, Any]:
    message = message_path.read_text(encoding="utf-8")
    receipt = validate_actual_task_message(message, references, required_focus_ids)
    receipt.update({
        "schema_version": "fewshot-reference-delivery-receipt-v1",
        "model": model,
        "reasoning_effort": reasoning_effort,
        "request_id": request_id,
        "actual_task_message_path": str(message_path.resolve()),
        "output_path": str(output_path.resolve()),
        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest() if output_path.exists() else "PENDING",
    })
    return receipt
