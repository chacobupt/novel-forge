#!/usr/bin/env python3
"""Validate and retrieve role-separated full-scene dialogue examples.

V2 retrieves structure and surface orality independently.  A scene may return
SURFACE_ONLY when no honest structural analogue exists; this is preferable to
forcing a semantically wrong pair and still gives the writer a native-spoken
rhythm/action anchor.  Verbatim excerpts remain temporary execution material.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = ROOT / ".work" / "dialogue_fewshot_v5"
DEFAULT_SCHEMA = ROOT / "00_系统规则" / "对话fewshot标注结构_current.json"
DEFAULT_CORPUS = WORK_DIR / "corpus" / "production_corpus_v5.jsonl"
DEFAULT_ANNOTATIONS = WORK_DIR / "annotations" / "production_examples_v5.jsonl"
DEFAULT_REVIEW_QUEUE = WORK_DIR / "review" / "focused_queue.jsonl"
DEFAULT_MANIFEST = WORK_DIR / "manifests" / "production_manifest_v5.json"
DEFAULT_CONFIG = ROOT / "00_系统规则" / "dialogue_fewshot_retrieval_config.json"
DEFAULT_PRODUCTION_RUNTIME = ROOT / "00_系统规则" / "dialogue_retrieval_production.py"
CJK_RE = re.compile(r"[\u3400-\u9fff]")
FORBIDDEN_KEYS = {"relationship_type", "relationship_axis", "relationship_axes", "auto_relationship"}
STRUCTURAL_WEIGHTS = {
    "scene_function": 0.34,
    "speaker_objective": 0.24,
    "listener_resistance": 0.22,
    "power_position": 0.08,
    "pressure": 0.06,
    "publicity": 0.03,
    "emotion_trajectory": 0.03,
}
SURFACE_WEIGHTS = {
    "dialogue_register": 0.25,
    "speaking_strategy": 0.22,
    "companion_patterns": 0.22,
    "emotion_trajectory": 0.13,
    "pressure": 0.10,
    "publicity": 0.04,
    "story_domain": 0.04,
}
NEGATIVE_WEIGHTS = {
    "scene_function": 0.20,
    "speaker_objective": 0.20,
    "listener_resistance": 0.18,
    "dialogue_register": 0.12,
    "speaking_strategy": 0.12,
    "companion_patterns": 0.10,
    "pressure": 0.08,
}
STRUCTURAL_THRESHOLD = 0.58
SURFACE_THRESHOLD = 0.30
OBJECTIVE_CATEGORY_HINTS = {
    "care": {"daily_care"},
    "bargain": {"bargain_trade"},
    "command": {"command_crisis", "authority_probe"},
    "mobilize": {"command_crisis", "public_group"},
    "report": {"professional_work", "public_group"},
    "inform": {"professional_work", "general_interaction"},
    "probe": {"secret_evasion", "authority_probe"},
    "evade": {"secret_evasion"},
    "refuse": {"authority_probe", "bargain_trade"},
    "correct": {"authority_probe", "professional_work"},
    "ask": {"general_interaction", "professional_work"},
    "seek_help": {"daily_care", "general_interaction"},
    "commit": {"authority_probe", "public_group", "bargain_trade"},
    "reconcile": {"daily_care", "general_interaction"},
}
UNKNOWN_VALUES = {None, "", "unclear", "unknown", "UNKNOWN", "待核", "UNCLASSIFIED"}
ABC_TIER_FIELDS = {
    "A": ("scene_function", "speaker_objective", "listener_resistance", "speaking_strategy"),
    "B": ("power_position", "dialogue_register", "emotion_trajectory"),
    "C": ("pressure", "publicity", "companion_patterns", "story_domain"),
}
ALL_ABC_FIELDS = tuple(field for tier in ("A", "B", "C") for field in ABC_TIER_FIELDS[tier])
MULTI_VALUE_FIELDS = {"speaking_strategy", "companion_patterns"}


class ValidationError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def validate_default_manifest() -> dict[str, Any]:
    manifest = load_json(DEFAULT_MANIFEST)
    if manifest.get("status") != "ACTIVE":
        raise ValidationError(f"production manifest is not ACTIVE: {manifest.get('status')!r}")
    expected = manifest.get("hashes", {})
    actual = {
        "corpus_v5": hashlib.sha256(DEFAULT_CORPUS.read_bytes()).hexdigest(),
        "annotations_v5": hashlib.sha256(DEFAULT_ANNOTATIONS.read_bytes()).hexdigest(),
        "schema": hashlib.sha256(DEFAULT_SCHEMA.read_bytes()).hexdigest(),
        "production_gate": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    if "retrieval_config" in expected:
        actual["retrieval_config"] = hashlib.sha256(DEFAULT_CONFIG.read_bytes()).hexdigest()
    if "production_runtime" in expected:
        actual["production_runtime"] = hashlib.sha256(DEFAULT_PRODUCTION_RUNTIME.read_bytes()).hexdigest()
    mismatches = {key: {"expected": expected.get(key), "actual": value} for key, value in actual.items() if expected.get(key) != value}
    if mismatches:
        raise ValidationError(f"production manifest hash mismatch: {mismatches}")
    return {"status": "PASS", "version": manifest.get("version"), "hashes": actual}


def normalize_cjk(text: str) -> str:
    return "".join(CJK_RE.findall(text))


def forbidden_key_paths(value: Any, prefix: str = "") -> list[str]:
    hits: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in FORBIDDEN_KEYS:
                hits.append(path)
            hits.extend(forbidden_key_paths(child, path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(forbidden_key_paths(child, f"{prefix}[{index}]"))
    return hits


def _enum(schema: dict[str, Any], key: str) -> set[str]:
    return set(schema["retrieval_profile"]["enums"][key])


def validate_profile(profile: dict[str, Any], schema: dict[str, Any], runtime: bool = False) -> list[str]:
    errors: list[str] = []
    for field in schema["retrieval_profile"]["required_fields"]:
        if field == "speaking_strategy" and runtime and field not in profile:
            continue
        if field not in profile:
            errors.append(f"missing field {field}")
    if profile.get("scene_function") not in _enum(schema, "scene_function"):
        errors.append(f"unsupported scene_function {profile.get('scene_function')!r}")
    context = profile.get("pressure_context", {})
    if context.get("level") not in _enum(schema, "pressure"):
        errors.append(f"unsupported pressure {context.get('level')!r}")
    if context.get("publicity") not in _enum(schema, "publicness"):
        errors.append(f"unsupported publicity {context.get('publicity')!r}")
    for field in ("power_position", "speaker_objective", "listener_resistance", "emotion_trajectory", "dialogue_register"):
        if profile.get(field) not in _enum(schema, field):
            errors.append(f"unsupported {field} {profile.get(field)!r}")
    strategies = profile.get("speaking_strategy", [])
    if not isinstance(strategies, list) or len(strategies) > 3 or (not runtime and not strategies):
        errors.append("speaking_strategy must be a list with one to three values")
    elif set(strategies) - _enum(schema, "speaking_strategy"):
        errors.append(f"unsupported speaking_strategy {sorted(set(strategies) - _enum(schema, 'speaking_strategy'))}")
    return errors


def validate_assets(annotation_path: Path, corpus_path: Path, schema_path: Path) -> dict[str, Any]:
    schema = load_json(schema_path)
    corpus_rows = load_jsonl(corpus_path)
    annotations = load_jsonl(annotation_path)
    corpus = {row["scene_id"]: row for row in corpus_rows}
    errors: list[str] = []
    ids = [row.get("scene_id") for row in annotations]
    if len(ids) != len(set(ids)):
        errors.append("duplicate annotation scene_id")
    if set(ids) != set(corpus):
        missing = sorted(set(corpus) - set(ids))
        extra = sorted(set(ids) - set(corpus))
        errors.append(f"annotation coverage mismatch missing={len(missing)} extra={len(extra)}")
    for row in annotations:
        scene_id = row.get("scene_id", "<missing>")
        if forbidden_key_paths(row):
            errors.append(f"{scene_id}: removed relationship feature returned")
        source = corpus.get(scene_id)
        if not source:
            continue
        expected_hash = source.get("excerpt_sha256") or sha256_text(source["raw_excerpt"])
        if row.get("raw_excerpt_sha256") != expected_hash:
            errors.append(f"{scene_id}: raw excerpt hash mismatch")
        if row.get("polarity") != source.get("approved_polarity"):
            errors.append(f"{scene_id}: polarity mismatch")
        profile_errors = validate_profile(row.get("retrieval_profile", {}), schema)
        errors.extend(f"{scene_id}: {error}" for error in profile_errors)
        participants = row.get("participants", [])
        if not participants or len(participants) > schema["participant_speech_realization"]["limits"]["max_participants"]:
            errors.append(f"{scene_id}: invalid participant count")
        required_participant = set(schema["participant_speech_realization"]["required_fields"])
        for participant in participants:
            missing = sorted(required_participant - set(participant))
            if missing:
                errors.append(f"{scene_id}: participant missing {missing}")
        turns = row.get("key_turning_points", [])
        if len(turns) > schema["key_turning_points"]["limits"]["max"]:
            errors.append(f"{scene_id}: too many key turning points")
        eligibility = row.get("fewshot_eligibility", {})
        if source.get("approved_polarity") == "正例" and not isinstance(eligibility.get("eligible"), bool):
            errors.append(f"{scene_id}: positive missing eligibility decision")
        if source.get("approved_polarity") == "反例" and not row.get("negative_contrast"):
            errors.append(f"{scene_id}: negative missing contrast")
        capabilities = row.get("example_capabilities", {})
        if set(capabilities) != set(schema.get("example_capabilities", {}).get("required_fields", [])):
            errors.append(f"{scene_id}: example_capabilities must declare structural/surface_orality/project_negative")
        elif any(not isinstance(value, bool) for value in capabilities.values()):
            errors.append(f"{scene_id}: example_capabilities values must be booleans")
        if row.get("polarity") == "正例" and capabilities.get("surface_orality"):
            native = row.get("native_speech_quality", {})
            missing_native = set(schema.get("native_speech_quality", {}).get("required_fields", [])) - set(native)
            if missing_native:
                errors.append(f"{scene_id}: native_speech_quality missing {sorted(missing_native)}")
            if native.get("interpretable_but_unsayable") is not False:
                errors.append(f"{scene_id}: surface example is interpretable but not approved as sayable")
    if errors:
        raise ValidationError("\n".join(errors[:100]))
    return {
        "status": "PASS",
        "counts": {
            "corpus": len(corpus_rows),
            "annotations": len(annotations),
            "positive": sum(row.get("polarity") == "正例" for row in annotations),
            "negative": sum(row.get("polarity") == "反例" for row in annotations),
            "eligible_positive": sum(row.get("fewshot_eligibility", {}).get("eligible") is True for row in annotations if row.get("polarity") == "正例"),
            "production_positive": sum(is_production_positive(row) for row in annotations),
            "structural_positive": sum(is_production_positive(row, "structural") for row in annotations),
            "surface_positive": sum(is_production_positive(row, "surface_orality") for row in annotations),
        },
        "removed_relationship_features": "PASS",
        "hashes": {
            "schema": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            "corpus": hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
            "annotations": hashlib.sha256(annotation_path.read_bytes()).hexdigest(),
        },
    }


def _equal(a: Any, b: Any) -> float:
    return 1.0 if a == b and a not in (None, "", "unclear") else 0.0


def is_production_positive(annotation: dict[str, Any], role: str | None = None) -> bool:
    """Return role-specific production eligibility.

    V1 rows remain accepted by the no-role compatibility path used in older
    unit tests. V2 retrieval always supplies a role and therefore requires the
    corresponding capability flag.
    """
    base = annotation.get("polarity") == "正例" and annotation.get("fewshot_eligibility", {}).get("eligible") is True
    if not base:
        return False
    capabilities = annotation.get("example_capabilities")
    if role is None and capabilities is None:
        return annotation.get("needs_human_review") is False
    if role is None:
        return annotation.get("needs_human_review") is False and bool(
            capabilities.get("structural") or capabilities.get("surface_orality")
        )
    eligibility = annotation.get("fewshot_eligibility", {})
    if eligibility.get("context_sufficient") is not True or eligibility.get("excerpt_scope") in {None, "incomplete"}:
        return False
    if not capabilities or capabilities.get(role) is not True:
        return False
    if annotation.get("needs_human_review") is not False:
        return False
    if role == "structural":
        return True
    native = annotation.get("native_speech_quality", {})
    return (
        native.get("interpretable_but_unsayable") is False
        and native.get("forced_quip_risk") not in {"high", "HIGH"}
    )


def _overlap(a: list[str], b: list[str]) -> float:
    left, right = set(a or []), set(b or [])
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def profile_components(target: dict[str, Any], sample: dict[str, Any]) -> dict[str, float]:
    tc, sc = target["pressure_context"], sample["pressure_context"]
    return {
        "scene_function": _equal(target.get("scene_function"), sample.get("scene_function")),
        "pressure": _equal(tc.get("level"), sc.get("level")),
        "publicity": _equal(tc.get("publicity"), sc.get("publicity")),
        "power_position": _equal(target.get("power_position"), sample.get("power_position")),
        "speaker_objective": _equal(target.get("speaker_objective"), sample.get("speaker_objective")),
        "listener_resistance": _equal(target.get("listener_resistance"), sample.get("listener_resistance")),
        "emotion_trajectory": _equal(target.get("emotion_trajectory"), sample.get("emotion_trajectory")),
        "dialogue_register": _equal(target.get("dialogue_register"), sample.get("dialogue_register")),
        "speaking_strategy": _overlap(target.get("speaking_strategy", []), sample.get("speaking_strategy", [])),
        "companion_patterns": _overlap(target.get("companion_patterns", []), sample.get("companion_patterns", [])),
        "story_domain": _equal(target.get("story_domain"), sample.get("story_domain")),
    }


def score_with(weights: dict[str, float], components: dict[str, float]) -> float:
    return round(sum(weights[key] * components.get(key, 0.0) for key in weights), 4)


def score_profile(target: dict[str, Any], sample: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """Compatibility score used by older callers; structural semantics lead."""
    components = profile_components(target, sample)
    score = score_with(STRUCTURAL_WEIGHTS, components)
    return score, components


def structural_score(target: dict[str, Any], sample: dict[str, Any]) -> tuple[float, dict[str, float]]:
    components = profile_components(target, sample)
    return score_with(STRUCTURAL_WEIGHTS, components), components


def surface_score(target: dict[str, Any], sample: dict[str, Any]) -> tuple[float, dict[str, float]]:
    components = profile_components(target, sample)
    return score_with(SURFACE_WEIGHTS, components), components


def negative_score(target: dict[str, Any], sample: dict[str, Any]) -> tuple[float, dict[str, float]]:
    components = profile_components(target, sample)
    return score_with(NEGATIVE_WEIGHTS, components), components


def structural_semantics_pass(score: float, components: dict[str, float]) -> bool:
    primary = components["scene_function"] + components["speaker_objective"] + components["listener_resistance"]
    return score >= STRUCTURAL_THRESHOLD and components["scene_function"] == 1.0 and primary >= 2.0


def surface_semantics_pass(score: float, components: dict[str, float]) -> bool:
    # Surface examples intentionally need not share plot function. They must
    # match the speaking register plus at least one pressure/strategy/action
    # posture signal, or score strongly across the remaining fields.
    posture = components["speaking_strategy"] + components["companion_patterns"] + components["pressure"]
    return score >= SURFACE_THRESHOLD and components["dialogue_register"] == 1.0 and posture > 0.0


def expansion_surface_score(profile: dict[str, Any], row: dict[str, Any]) -> float:
    desired = OBJECTIVE_CATEGORY_HINTS.get(profile.get("speaker_objective"), {"general_interaction"})
    category = row.get("category_hint")
    category_score = 1.0 if category in desired else 0.0
    target_companion = profile.get("companion_patterns", [])
    candidate_companion = row.get("surface_profile", {}).get("companion_patterns", [])
    companion_score = _overlap(target_companion, candidate_companion)
    public_bonus = 1.0 if profile.get("pressure_context", {}).get("publicity") == "public" and category == "public_group" else 0.0
    quote_count = row.get("quote_count", 0)
    completeness = 1.0 if 3 <= quote_count <= 8 and len(row.get("raw_excerpt", "")) <= 1200 else 0.0
    return round(0.52 * category_score + 0.22 * companion_score + 0.12 * public_bonus + 0.14 * completeness, 4)


def retrieve_examples_legacy(profile: dict[str, Any], annotation_path: Path, corpus_path: Path, schema_path: Path) -> dict[str, Any]:
    if annotation_path.resolve() == DEFAULT_ANNOTATIONS.resolve() and corpus_path.resolve() == DEFAULT_CORPUS.resolve():
        validate_default_manifest()
    schema = load_json(schema_path)
    errors = validate_profile(profile, schema, runtime=True)
    if errors:
        # A malformed query is a processing error, not corpus coverage evidence.
        # This remains fail-closed for consumers accepting only MATCH/SURFACE_ONLY.
        return {"result": "INVALID_PROFILE", "error_kind": "INVALID_PROFILE", "coverage_assessed": False, "reasons": errors}
    corpus = {row["scene_id"]: row for row in load_jsonl(corpus_path)}
    annotations = load_jsonl(annotation_path)
    structural_ranked: list[tuple[float, dict[str, float], dict[str, Any]]] = []
    surface_ranked: list[tuple[float, dict[str, float], dict[str, Any]]] = []
    negative_ranked: list[tuple[float, dict[str, float], dict[str, Any]]] = []
    for annotation in annotations:
        sample_profile = annotation["retrieval_profile"]
        if is_production_positive(annotation, "structural"):
            score, components = structural_score(profile, sample_profile)
            structural_ranked.append((score, components, annotation))
        if is_production_positive(annotation, "surface_orality"):
            score, components = surface_score(profile, sample_profile)
            surface_ranked.append((score, components, annotation))
        if annotation.get("example_capabilities", {}).get("project_negative") or annotation.get("polarity") == "反例":
            score, components = negative_score(profile, sample_profile)
            negative_ranked.append((score, components, annotation))
    structural_ranked.sort(key=lambda item: (-item[0], item[2]["scene_id"]))
    surface_ranked.sort(key=lambda item: (-item[0], item[2]["scene_id"]))
    negative_ranked.sort(key=lambda item: (-item[0], item[2]["scene_id"]))
    structural = next((item for item in structural_ranked if structural_semantics_pass(item[0], item[1])), None)
    surface = next((item for item in surface_ranked if surface_semantics_pass(item[0], item[1])), None)
    if structural and surface and structural[2].get("source") == surface[2].get("source"):
        alternate = next(
            (
                item
                for item in surface_ranked
                if item[2].get("source") != structural[2].get("source")
                and surface_semantics_pass(item[0], item[1])
            ),
            None,
        )
        # Source diversity is a preference, not a prerequisite. Keeping the
        # approved same-source anchor is safer than manufacturing a gap.
        if alternate is not None:
            surface = alternate
    if not surface or not negative_ranked:
        reasons = []
        if not surface:
            reasons.append("no approved native-spoken surface anchor")
        if not negative_ranked:
            reasons.append("no project negative")
        return {"result": "OUT_OF_COVERAGE", "reasons": reasons}

    def expand(item: tuple[float, dict[str, float], dict[str, Any]]) -> dict[str, Any]:
        score, components, annotation = item
        raw = corpus[annotation["scene_id"]]
        return {
            "scene_id": annotation["scene_id"],
            "source": annotation["source"],
            "score": score,
            "score_components": components,
            "raw_excerpt": raw["raw_excerpt"],
            "raw_excerpt_sha256": annotation["raw_excerpt_sha256"],
            "annotation": annotation,
        }

    selected_negative = negative_ranked[0]
    positives: list[dict[str, Any]] = []
    if structural:
        expanded = expand(structural)
        expanded["role"] = "structural_positive"
        positives.append(expanded)
    expanded_surface = expand(surface)
    expanded_surface["role"] = "surface_positive"
    positives.append(expanded_surface)
    expansion_candidates: list[dict[str, Any]] = []
    if DEFAULT_REVIEW_QUEUE.exists():
        provisional = sorted(
            (
                (expansion_surface_score(profile, row), row)
                for row in load_jsonl(DEFAULT_REVIEW_QUEUE)
                if row.get("approval_status") == "PENDING_FOCUSED_AUDIT"
            ),
            key=lambda item: (-item[0], item[1]["scene_id"]),
        )
        used_sources: set[str] = set()
        for score, row in provisional:
            if score <= 0 or row.get("source") in used_sources:
                continue
            used_sources.add(row["source"])
            expansion_candidates.append(
                {
                    "scene_id": row["scene_id"],
                    "source": row["source"],
                    "role": "surface_positive_candidate",
                    "score": score,
                    "raw_excerpt": row["raw_excerpt"],
                    "raw_excerpt_sha256": row["excerpt_sha256"],
                    "approval_status": "PENDING_FOCUSED_AUDIT",
                    "surface_profile": row.get("surface_profile", {}),
                }
            )
            if len(expansion_candidates) == 5:
                break
    result = {
        "result": "MATCH" if structural else "SURFACE_ONLY",
        "profile": profile,
        "positive_examples": positives,
        "negative_example": expand(selected_negative),
        "structural_alternatives": [
            {**expand(item), "role": "structural_positive"}
            for item in structural_ranked[:5]
        ],
        "surface_alternatives": [
            {**expand(item), "role": "surface_positive"}
            for item in surface_ranked[:5]
        ],
        "expansion_surface_candidates": expansion_candidates,
        "packet_audit_status": "PENDING_FOCUSED_AUDIT",
        "packet_audit_rule": "自动首选不是准入结论。审计者必须阅读目标场景与候选完整窗口，逐项确认目标/阻力/状态变化或母语搭配/动作因果/独立嘴读；可从五个候选中替换，全部不合格则记缺口。",
        "structural_fallback": None if structural else "从冻结事实、人物目标和听者阻力独立设计结构；不得拿表层正例补剧情结构。",
        "prompt_rule": "结构正例只示范状态变化；表层正例只示范母语搭配、长短节奏、回应余味与动作交错。普通对话默认禁止新造比喻、绰号和机锋；不得继承专名、设定、独特措辞、口癖或连续事件。",
    }
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    result["packet_sha256"] = sha256_text(serialized)
    return result


def _valid_scalar(value: Any) -> bool:
    return value not in UNKNOWN_VALUES and not isinstance(value, (list, dict))


def _valid_set(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str) and item not in UNKNOWN_VALUES}


def _dimension_value(profile: dict[str, Any], field: str) -> Any:
    if field == "pressure":
        return profile.get("pressure_context", {}).get("level")
    if field == "publicity":
        return profile.get("pressure_context", {}).get("publicity")
    return profile.get(field)


def abc_dimension_scores(target: dict[str, Any], sample: dict[str, Any], field: str,
                         pool_profiles: list[dict[str, Any]]) -> tuple[float, float]:
    """Return frozen ABC RAW and INFO scores for one dimension."""
    return _abc_score_with_stats(sample, _abc_field_stats(target, pool_profiles, field))


def _abc_field_stats(target: dict[str, Any], pool_profiles: list[dict[str, Any]], field: str) -> dict[str, Any]:
    n = len(pool_profiles)
    norm = math.log(1 + n / 2)
    target_value = _dimension_value(target, field)
    if field in MULTI_VALUE_FIELDS:
        query = _valid_set(target_value)
        values = set().union(*(_valid_set(_dimension_value(row, field)) for row in pool_profiles)) if pool_profiles else set()
        dfs = {value: sum(value in _valid_set(_dimension_value(row, field)) for row in pool_profiles)
               for value in sorted(values | query)}
        df_query = sum(bool(query & _valid_set(_dimension_value(row, field))) for row in pool_profiles)
        return {"field": field, "n": n, "norm": norm, "query": query, "dfs": dfs, "df_query": df_query}
    df = sum(_dimension_value(row, field) == target_value for row in pool_profiles) if _valid_scalar(target_value) else 0
    return {"field": field, "n": n, "norm": norm, "query": target_value, "df": df}


def _abc_score_with_stats(sample: dict[str, Any], stats: dict[str, Any]) -> tuple[float, float]:
    n, norm, field = stats["n"], stats["norm"], stats["field"]
    if not n or not norm:
        return 0.0, 0.0
    sample_value = _dimension_value(sample, field)
    if field in MULTI_VALUE_FIELDS:
        query, candidate = stats["query"], _valid_set(sample_value)
        intersection = query & candidate
        union = query | candidate
        if not query or not candidate or not intersection or not union:
            return 0.0, 0.0
        raw = len(intersection) / len(union)
        idfs = {value: math.log(1 + n / (1 + stats["dfs"].get(value, 0))) for value in sorted(union)}
        denominator = sum(idfs[value] for value in sorted(union))
        weighted_jaccard = sum(idfs[value] for value in sorted(intersection)) / denominator if denominator else 0.0
        info = weighted_jaccard * math.log(1 + n / (1 + stats["df_query"])) / norm
        return raw, info
    if not _valid_scalar(stats["query"]) or not _valid_scalar(sample_value) or stats["query"] != sample_value:
        return 0.0, 0.0
    return 1.0, math.log(1 + n / (1 + stats["df"])) / norm


def abc_rank_pool(profile: dict[str, Any], annotations: list[dict[str, Any]], corpus: dict[str, dict[str, Any]],
                  role: str, config: dict[str, Any]) -> dict[str, Any]:
    pool = [row for row in annotations if is_production_positive(row, role)]
    pool_profiles = [row["retrieval_profile"] for row in pool]
    all_fields = ALL_ABC_FIELDS
    field_stats = {field: _abc_field_stats(profile, pool_profiles, field) for field in all_fields}
    info_weight = config["abc"]["hybrid_info_weight"]
    raw_weight = config["abc"]["hybrid_raw_weight"]
    tier_rows: dict[str, list[dict[str, Any]]] = {}
    tier_eligible_counts: dict[str, int] = {}
    for tier, fields in ABC_TIER_FIELDS.items():
        ranked: list[dict[str, Any]] = []
        for row in pool:
            raw_scores: dict[str, float] = {}
            info_scores: dict[str, float] = {}
            for field in all_fields:
                raw, info = _abc_score_with_stats(row["retrieval_profile"], field_stats[field])
                raw_scores[field], info_scores[field] = raw, info
            if not any(raw_scores[field] > 0 for field in fields):
                continue
            raw_tier = sum(raw_scores[field] for field in fields) / len(fields)
            info_tier = sum(info_scores[field] for field in fields) / len(fields)
            hybrid = info_weight * info_tier + raw_weight * raw_tier
            source = corpus[row["scene_id"]]
            line_span = source.get("line_span") or []
            parent = source.get("parent_window_id") or row.get("parent_window_id") or "|".join(
                [str(row.get("source", "")), str(source.get("chapter", "")), "-".join(map(str, line_span))]
            ) or row["scene_id"]
            ranked.append({
                "example_id": row["scene_id"],
                "scene_id": row["scene_id"],
                "source": row.get("source"),
                "parent": parent,
                "raw_excerpt_sha256": row["raw_excerpt_sha256"],
                "role": role,
                "tier": tier,
                "tier_score": hybrid,
                "raw_tier_score": raw_tier,
                "info_tier_score": info_tier,
                "global_information_score": sum(info_scores.values()) / len(info_scores),
                "matched_dimensions": [field for field in fields if raw_scores[field] > 0],
                "matched_dimension_count": sum(raw_scores[field] > 0 for field in fields),
                "score_components": raw_scores,
                "information_components": info_scores,
                "annotation": row,
            })
        ranked.sort(key=lambda item: (
            -item["tier_score"], -item["matched_dimension_count"],
            -item["global_information_score"], item["scene_id"],
        ))
        tier_eligible_counts[tier] = len(ranked)
        selected = ranked[:int(config["abc"]["tier_k"][tier])]
        for index, item in enumerate(selected, 1):
            item["tier_rank"] = index
        tier_rows[tier] = selected

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for tier in ("A", "B", "C"):
        for item in tier_rows[tier]:
            key = (item["parent"], item["raw_excerpt_sha256"])
            if key not in groups:
                groups[key] = {**item, "recall_tiers": [], "tier_rank": {}, "tier_score": {},
                               "equivalent_example_ids": []}
            group = groups[key]
            group["recall_tiers"].append(tier)
            group["tier_rank"][tier] = item["tier_rank"]
            group["tier_score"][tier] = item["tier_score"]
            if item["example_id"] not in group["equivalent_example_ids"]:
                group["equivalent_example_ids"].append(item["example_id"])
            group["matched_dimensions"] = sorted(set(group["matched_dimensions"]) | set(item["matched_dimensions"]))

    candidates = list(groups.values())
    ranker = structural_score if role == "structural" else surface_score
    for item in candidates:
        relevance, components = ranker(profile, item["annotation"]["retrieval_profile"])
        item["relevance_score"] = relevance
        item["relevance_components"] = components
    candidates.sort(key=lambda item: (
        -item["relevance_score"], -max(item["tier_score"].values()),
        -len(item["matched_dimensions"]), item["scene_id"],
    ))
    return {
        "role": role,
        "pool_size": len(pool),
        "tier_eligible_counts": tier_eligible_counts,
        "tier_selected_counts": {tier: len(tier_rows[tier]) for tier in ("A", "B", "C")},
        "tier_k": dict(config["abc"]["tier_k"]),
        "unique_candidate_count": len(candidates),
        "candidates": candidates,
    }


def _expand_abc_candidate(item: dict[str, Any], corpus: dict[str, dict[str, Any]], output_role: str) -> dict[str, Any]:
    return {
        **{key: value for key, value in item.items() if key != "annotation"},
        "role": output_role,
        "raw_excerpt": corpus[item["scene_id"]]["raw_excerpt"],
        "annotation": item["annotation"],
    }


def select_audited_positive_payload(audited_matches: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    """Build single and shadow multi-positive payloads only from audit MATCH rows."""
    matches = [row for row in audited_matches if row.get("audit_result") == "MATCH"]
    matches.sort(key=lambda row: (-float(row.get("relevance_score", 0)), row["example_id"]))
    primary = matches[:1]
    selected = list(primary)
    capabilities = set(primary[0].get("allowed_capabilities", [])) if primary else set()
    for row in matches[1:]:
        added = set(row.get("allowed_capabilities", [])) - capabilities
        if added:
            selected.append(row)
            capabilities.update(added)
        if len(selected) >= int(config.get("max_positive_examples", 3)):
            break
    return {
        "single_positive_payload": primary,
        "multi_positive_candidate_payload": selected,
        "multi_positive_enabled": bool(config.get("multi_positive_enabled", False)),
        "mode": "SHADOW" if not config.get("multi_positive_enabled", False) else "ACTIVE",
    }


def retrieve_examples_abc(profile: dict[str, Any], annotation_path: Path, corpus_path: Path,
                          schema_path: Path, config: dict[str, Any],
                          retrieval_mode: str = "abc_hybrid_v1") -> dict[str, Any]:
    if annotation_path.resolve() == DEFAULT_ANNOTATIONS.resolve() and corpus_path.resolve() == DEFAULT_CORPUS.resolve():
        validate_default_manifest()
    schema = load_json(schema_path)
    errors = validate_profile(profile, schema, runtime=True)
    if errors:
        return {"result": "INVALID_PROFILE", "error_kind": "INVALID_PROFILE", "coverage_assessed": False, "reasons": errors}
    corpus = {row["scene_id"]: row for row in load_jsonl(corpus_path)}
    annotations = load_jsonl(annotation_path)
    structural_route = abc_rank_pool(profile, annotations, corpus, "structural", config)
    surface_route = abc_rank_pool(profile, annotations, corpus, "surface_orality", config)
    negative_ranked = []
    for annotation in annotations:
        if annotation.get("example_capabilities", {}).get("project_negative") or annotation.get("polarity") == "反例":
            score, components = negative_score(profile, annotation["retrieval_profile"])
            negative_ranked.append((score, components, annotation))
    negative_ranked.sort(key=lambda item: (-item[0], item[2]["scene_id"]))
    if not surface_route["candidates"] or not negative_ranked:
        return {"result": "OUT_OF_COVERAGE", "machine_stage": "NO_CANDIDATES", "reasons": [
            "no approved surface candidates" if not surface_route["candidates"] else "no project negative"
        ]}

    def expand_negative(item: tuple[float, dict[str, float], dict[str, Any]]) -> dict[str, Any]:
        score, components, annotation = item
        return {"scene_id": annotation["scene_id"], "source": annotation["source"], "score": score,
                "score_components": components, "raw_excerpt": corpus[annotation["scene_id"]]["raw_excerpt"],
                "raw_excerpt_sha256": annotation["raw_excerpt_sha256"], "annotation": annotation}

    structural_top = structural_route["candidates"][:5]
    surface_top = surface_route["candidates"][:5]
    positives = []
    if structural_top:
        positives.append(_expand_abc_candidate(structural_top[0], corpus, "structural_positive"))
    positives.append(_expand_abc_candidate(surface_top[0], corpus, "surface_positive"))
    compact_route = lambda route: {**{k: v for k, v in route.items() if k != "candidates"}, "candidates": [
        {k: v for k, v in item.items() if k not in {"annotation", "relevance_components", "score_components", "information_components"}}
        for item in route["candidates"]
    ]}
    result = {
        "result": "MATCH" if structural_top else "SURFACE_ONLY",
        "machine_stage": "CANDIDATES_FOUND",
        "retrieval_mode": retrieval_mode,
        "profile": profile,
        "positive_examples": positives,
        "negative_example": expand_negative(negative_ranked[0]),
        "structural_alternatives": [_expand_abc_candidate(item, corpus, "structural_positive") for item in structural_top],
        "surface_alternatives": [_expand_abc_candidate(item, corpus, "surface_positive") for item in surface_top],
        "candidate_routes": {"structural": compact_route(structural_route), "surface_orality": compact_route(surface_route)},
        "packet_audit_status": "PENDING_FOCUSED_AUDIT",
        "packet_audit_rule": "CANDIDATES_FOUND不是AUDIT_MATCH。审计者必须阅读目标场景和候选完整窗口；只有MATCH可进入写作payload。",
        "structural_fallback": None if structural_top else "从冻结事实、人物目标和听者阻力独立设计结构。",
        "prompt_rule": "ABC只负责候选召回；相关性排序只导航；完整窗口审计决定是否可用。",
        "multi_positive": {"enabled": bool(config.get("multi_positive_enabled", False)),
                           "mode": "ACTIVE" if config.get("multi_positive_enabled", False) else "SHADOW",
                           "max_examples": int(config.get("max_positive_examples", 3)), "status": "AWAITING_AUDIT_MATCH_SET"},
    }
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    result["packet_sha256"] = sha256_text(serialized)
    return result


def retrieve_examples(profile: dict[str, Any], annotation_path: Path, corpus_path: Path, schema_path: Path,
                      mode: str | None = None, config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    config = load_json(config_path)
    selected_mode = mode or config.get("retrieval_mode", "legacy_v5")
    if selected_mode == "legacy_v5":
        return retrieve_examples_legacy(profile, annotation_path, corpus_path, schema_path)
    if selected_mode in {"abc_hybrid_v1", "abc_focused_audit_v1"}:
        return retrieve_examples_abc(profile, annotation_path, corpus_path, schema_path, config, selected_mode)
    raise ValidationError(f"unsupported retrieval mode: {selected_mode}")


def longest_overlap(left: str, right: str, cap: int = 80) -> str:
    a, b = normalize_cjk(left), normalize_cjk(right)
    for size in range(min(len(a), len(b), cap), 0, -1):
        for start in range(len(a) - size + 1):
            window = a[start:start + size]
            if window in b:
                return window
    return ""


def leak_check(candidate_path: Path, packet_path: Path, threshold: int = 14) -> dict[str, Any]:
    candidate = candidate_path.read_text(encoding="utf-8")
    packet = load_json(packet_path)
    checks = []
    for example in packet.get("positive_examples", []):
        overlap = longest_overlap(candidate, example["raw_excerpt"])
        checks.append({
            "scene_id": example["scene_id"],
            "max_overlap_chars": len(overlap),
            "overlap": overlap if len(overlap) >= threshold else "",
            "status": "FAIL" if len(overlap) >= threshold else "PASS",
        })
    return {
        "status": "FAIL" if any(item["status"] == "FAIL" for item in checks) else "PASS",
        "threshold": threshold,
        "checks": checks,
    }


def path_arg(value: str) -> Path:
    return Path(value).expanduser().resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--annotations", type=path_arg, default=DEFAULT_ANNOTATIONS)
    validate_parser.add_argument("--corpus", type=path_arg, default=DEFAULT_CORPUS)
    validate_parser.add_argument("--schema", type=path_arg, default=DEFAULT_SCHEMA)
    retrieve_parser = sub.add_parser("retrieve")
    retrieve_parser.add_argument("--profile", required=True)
    retrieve_parser.add_argument("--annotations", type=path_arg, default=DEFAULT_ANNOTATIONS)
    retrieve_parser.add_argument("--corpus", type=path_arg, default=DEFAULT_CORPUS)
    retrieve_parser.add_argument("--schema", type=path_arg, default=DEFAULT_SCHEMA)
    retrieve_parser.add_argument("--config", type=path_arg, default=DEFAULT_CONFIG)
    retrieve_parser.add_argument("--mode", choices=("legacy_v5", "abc_hybrid_v1", "abc_focused_audit_v1"))
    retrieve_parser.add_argument("--output", type=path_arg)
    leak_parser = sub.add_parser("leak-check")
    leak_parser.add_argument("--candidate", type=path_arg, required=True)
    leak_parser.add_argument("--packet", type=path_arg, required=True)
    leak_parser.add_argument("--threshold", type=int, default=14)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            result = validate_assets(args.annotations, args.corpus, args.schema)
            if args.annotations.resolve() == DEFAULT_ANNOTATIONS.resolve() and args.corpus.resolve() == DEFAULT_CORPUS.resolve():
                result["manifest_binding"] = validate_default_manifest()
        elif args.command == "retrieve":
            profile_value = Path(args.profile)
            profile = load_json(profile_value) if profile_value.exists() else json.loads(args.profile)
            result = retrieve_examples(profile, args.annotations, args.corpus, args.schema, args.mode, args.config)
        else:
            result = leak_check(args.candidate, args.packet, args.threshold)
    except (OSError, json.JSONDecodeError, KeyError, ValidationError, ValueError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if getattr(args, "output", None):
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(json.dumps({"status": "PASS", "output": str(args.output), "packet_sha256": result.get("packet_sha256")}, ensure_ascii=False))
    else:
        print(rendered)
    return 0 if result.get("status", result.get("result")) in {"PASS", "MATCH", "SURFACE_ONLY"} else 1


if __name__ == "__main__":
    sys.exit(main())
