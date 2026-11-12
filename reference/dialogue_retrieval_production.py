#!/usr/bin/env python3
"""Production orchestration for Canonical-v2 + ABC + Focused Audit.

ABC and the frozen R1 score only order complete windows.  A candidate enters
the writer payload only after the Focused Audit returns MATCH.  Technical
failures fall back visibly to the unchanged legacy_v5 route.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "00_系统规则"
CONFIG = RULES / "dialogue_fewshot_retrieval_config.json"
ACTIVE_DATA_POINTER = RULES / "dialogue_fewshot_active_data.json"
RANKER = RULES / "dialogue_fewshot_ranker_candidate_v1.json"
CONTRACT = RULES / "dialogue_target_profile_contract_v2.json"
DOMAIN_REGISTRY = RULES / "dialogue_story_domain_v1.json"
PRODUCTION_MODE = "abc_focused_audit_v1"
LEGACY_MODE = "legacy_v5"
AUDIT_MODEL = "gpt-6-luna"
AUDIT_EFFORT = "low"
DO_NOT_TRANSFER_MINIMUM = "专有名词、世界观设定、原作人物身份、具体事件、具体道具、具体事实、原句措辞、临时术语"


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


gate = _module("dialogue_fewshot_gate_production", RULES / "dialogue_fewshot_gate.py")
guarantee = _module("fewshot_reference_guarantee_production", RULES / "fewshot_reference_guarantee.py")


class ProductionRetrievalError(RuntimeError):
    pass


def read_json(path: Path | str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def active_data_paths() -> tuple[Path, Path, Path]:
    """Resolve only the versioned production data source.

    The retrieval algorithm and its configuration remain independent of this
    pointer, and historical/offline gate defaults stay bound to frozen v5.
    """
    if not ACTIVE_DATA_POINTER.exists():
        return gate.DEFAULT_MANIFEST, gate.DEFAULT_CORPUS, gate.DEFAULT_ANNOTATIONS
    pointer = read_json(ACTIVE_DATA_POINTER)
    return ROOT / pointer["manifest"], ROOT / pointer["corpus"], ROOT / pointer["annotations"]


def validate_active_data_manifest() -> dict[str, Any]:
    manifest_path, corpus_path, annotation_path = active_data_paths()
    manifest = read_json(manifest_path)
    if manifest.get("status") != "ACTIVE":
        raise ProductionRetrievalError(f"active data manifest is not ACTIVE: {manifest.get('status')!r}")
    expected = manifest.get("hashes", {})
    corpus_key = "corpus" if "corpus" in expected else "corpus_v5"
    annotation_key = "annotations" if "annotations" in expected else "annotations_v5"
    # The data manifest freezes corpus/schema assets. Runtime and policy are
    # versioned independently so a production adapter release does not mutate
    # historical corpus evidence.
    actual = {
        corpus_key: hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        annotation_key: hashlib.sha256(annotation_path.read_bytes()).hexdigest(),
        "schema": hashlib.sha256(gate.DEFAULT_SCHEMA.read_bytes()).hexdigest(),
        "production_gate": hashlib.sha256((RULES / "dialogue_fewshot_gate.py").read_bytes()).hexdigest(),
    }
    mismatches = {key: {"expected": expected.get(key), "actual": value} for key, value in actual.items() if expected.get(key) != value}
    if mismatches:
        raise ProductionRetrievalError(f"active data manifest hash mismatch: {mismatches}")
    return {"status": "PASS", "manifest": manifest_path, "corpus": corpus_path, "annotations": annotation_path, "hashes": actual}


def canonical_sha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def audit_output_schema() -> dict[str, Any]:
    props = {
        "candidate_id": {"type": "string"},
        "structure_result": {"type": "string", "enum": ["MATCH", "PARTIAL", "NO_MATCH", "NOT_APPLICABLE"]},
        "surface_result": {"type": "string", "enum": ["MATCH", "PARTIAL", "NO_MATCH", "NOT_APPLICABLE"]},
        "structure_allowed_capability": {"type": "string"},
        "surface_allowed_capability": {"type": "string"},
        "do_not_transfer": {"type": "string"},
        "concise_reason": {"type": "string"},
    }
    item = {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}
    return {"type": "object", "properties": {"results": {"type": "array", "items": item}}, "required": ["results"], "additionalProperties": False}


def audit_prompt(scene_brief: dict[str, Any], focus: dict[str, Any], batch: list[dict[str, Any]]) -> str:
    candidates = [
        {"candidate_id": f"C{i:02d}", "requested_roles": row["requested_roles"], "complete_window": row["source_window"]}
        for i, row in enumerate(batch, 1)
    ]
    packet = {
        "scene_writing_brief": scene_brief,
        "retrieval_focus": {
            "description": focus.get("metadata", {}).get("focus_description", ""),
            "profile": focus["profile"],
        },
        "candidate_windows": candidates,
    }
    return (
        "你是隔离的对话few-shot完整窗口审计者。逐个候选独立判断，不排序、不比较；看不到来源、排名、分数或QREL。"
        "只评requested_roles，未要求的角色必须填NOT_APPLICABLE。Structure只有主交流功能、objective、resistance处理、回应推进机制、状态变化五项全部可迁移才MATCH；局部相似为PARTIAL。"
        "Surface只有人物关系姿态、语域、strategy、节奏、动作—台词因果、无不可迁移风格六项整体成立才MATCH；仅口气或句式好为PARTIAL。"
        "不得因为需要few-shot而放宽。allowed_capability仅为MATCH写可学能力，否则写‘不可进入写作输入’。"
        "do_not_transfer至少包含专名、世界观、具体道具、原事件链、原措辞。只返回schema JSON。\n"
        + json.dumps(packet, ensure_ascii=False)
    )


class LunaFocusedAuditor:
    """Application-internal Codex audit path used by the final Shadow B."""

    model_id = AUDIT_MODEL

    def __init__(self, timeout: int = 300, retries: int = 1):
        self.timeout = timeout
        self.retries = retries

    def __call__(self, scene_brief: dict[str, Any], focus: dict[str, Any], batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_error = ""
        for _ in range(self.retries + 1):
            try:
                with tempfile.TemporaryDirectory(prefix="production_focused_audit_") as tmp:
                    tmp_path = Path(tmp)
                    output = tmp_path / "output.json"
                    schema = tmp_path / "schema.json"
                    schema.write_text(json.dumps(audit_output_schema(), ensure_ascii=False), encoding="utf-8")
                    command = [
                        "codex", "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
                        "--sandbox", "read-only", "-m", AUDIT_MODEL,
                        "-c", f'model_reasoning_effort="{AUDIT_EFFORT}"',
                        "--output-schema", str(schema), "-o", str(output), "-",
                    ]
                    dispatch_sandbox = os.environ.get("CODEX_CANDIDATE_ROLE_SANDBOX")
                    if dispatch_sandbox:
                        sandbox_path = Path(dispatch_sandbox).resolve()
                        if not sandbox_path.is_file():
                            raise ProductionRetrievalError("candidate audit dispatch sandbox missing")
                        command = ["/usr/bin/sandbox-exec", "-f", str(sandbox_path)] + command
                    run = subprocess.run(
                        command, input=audit_prompt(scene_brief, focus, batch), text=True,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=self.timeout,
                        cwd=Path("/private/tmp") if dispatch_sandbox else ROOT,
                    )
                    if run.returncode or not output.exists():
                        raise ProductionRetrievalError((run.stdout or "")[-2000:])
                    results = read_json(output).get("results")
                    expected = {f"C{i:02d}" for i in range(1, len(batch) + 1)}
                    if not isinstance(results, list) or {row.get("candidate_id") for row in results} != expected:
                        raise ProductionRetrievalError("Focused Audit schema coverage mismatch")
                    return results
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ProductionRetrievalError) as error:
                last_error = str(error)
        raise ProductionRetrievalError(f"Focused Audit failed after retry: {last_error}")


def validate_runtime_config(config: dict[str, Any]) -> None:
    expected = {"A": 143, "B": 190, "C": 47}
    if config.get("retrieval_mode") != PRODUCTION_MODE:
        raise ProductionRetrievalError(f"unexpected production mode: {config.get('retrieval_mode')}")
    if config.get("abc", {}).get("tier_k") != expected:
        raise ProductionRetrievalError("ABC budget drift")
    if config.get("multi_positive_enabled") is not True or config.get("max_positive_examples") != 3:
        raise ProductionRetrievalError("multi-positive production contract drift")
    audit = config.get("focused_audit", {})
    if audit.get("model") != AUDIT_MODEL or audit.get("max_unique_windows_per_focus") != 40:
        raise ProductionRetrievalError("Focused Audit production contract drift")
    if audit.get("round_sizes") != [8, 8, 8, 16] or sum(audit["round_sizes"]) != 40:
        raise ProductionRetrievalError("Focused Audit progressive budget drift")
    if config.get("fallback_mode") != LEGACY_MODE:
        raise ProductionRetrievalError("legacy fallback unavailable")


def validate_canonical_focuses(document: dict[str, Any], parent_id: str) -> list[dict[str, Any]]:
    if "v2" not in str(document.get("schema_version", "")):
        raise ProductionRetrievalError("Canonical profile is not v2")
    focuses = [row for row in document.get("retrieval_focuses", []) if row.get("parent_target_id") == parent_id]
    if not 1 <= len(focuses) <= 3:
        raise ProductionRetrievalError(f"invalid focus count for {parent_id}: {len(focuses)}")
    schema = gate.load_json(gate.DEFAULT_SCHEMA)
    seen = set()
    for focus in focuses:
        focus_id = focus.get("retrieval_focus_id")
        if not focus_id or focus_id in seen:
            raise ProductionRetrievalError("focus id missing or duplicated")
        seen.add(focus_id)
        errors = gate.validate_profile(focus.get("profile", {}), schema, runtime=True)
        if errors:
            raise ProductionRetrievalError(f"invalid Canonical profile {focus_id}: {errors}")
        actual = canonical_sha(focus["profile"])
        if focus.get("profile_sha256") != actual:
            raise ProductionRetrievalError(f"Canonical profile SHA mismatch: {focus_id}")
    return sorted(focuses, key=lambda row: row["retrieval_focus_id"])


def _coarse_queue(profile: dict[str, Any], annotations: list[dict[str, Any]], corpus: dict[str, dict[str, Any]],
                  config: dict[str, Any], ranker: dict[str, Any], include_full_union: bool = False):
    routes = {}
    for output_role, capability_role in (("structure", "structural"), ("surface", "surface_orality")):
        route = gate.abc_rank_pool(profile, annotations, corpus, capability_role, config)
        weights = ranker["weights"][output_role]
        for item in route["candidates"]:
            item["eleven_d_score"] = round(sum(weights.get(key, 0) * value for key, value in item["score_components"].items()), 10)
        route["candidates"].sort(key=lambda row: (-row["eleven_d_score"], -max(row["tier_score"].values()), row["scene_id"]))
        routes[output_role] = route

    by_window: dict[tuple[str, str], dict[str, Any]] = {}
    ordered_keys = []
    maximum = max(len(route["candidates"]) for route in routes.values())
    for index in range(maximum):
        for role in ("structure", "surface"):
            rows = routes[role]["candidates"]
            if index >= len(rows):
                continue
            item = rows[index]
            key = (str(item.get("parent", "")), item["raw_excerpt_sha256"])
            if key not in by_window:
                by_window[key] = {
                    "example_id": item["scene_id"],
                    "source_window": corpus[item["scene_id"]]["raw_excerpt"],
                    "raw_excerpt_sha256": item["raw_excerpt_sha256"],
                    "requested_roles": [],
                    "role_ranks": {},
                    "relevance_score": item["eleven_d_score"],
                }
                ordered_keys.append(key)
            row = by_window[key]
            if role not in row["requested_roles"]:
                row["requested_roles"].append(role)
            row["role_ranks"][role] = index + 1
            row["relevance_score"] = max(row["relevance_score"], item["eleven_d_score"])
    full_union = [by_window[key] for key in ordered_keys]
    queue = full_union[:40]
    counts = {
        "A_candidates": sum(route["tier_selected_counts"]["A"] for route in routes.values()),
        "B_candidates": sum(route["tier_selected_counts"]["B"] for route in routes.values()),
        "C_candidates": sum(route["tier_selected_counts"]["C"] for route in routes.values()),
        "union_unique_candidates": len(full_union),
        "focused_audit_queue_candidates": len(queue),
        "role_unique_candidates": {role: route["unique_candidate_count"] for role, route in routes.items()},
    }
    return (queue, counts, full_union) if include_full_union else (queue, counts)


def _audit_focus(scene_brief: dict[str, Any], focus: dict[str, Any], queue: list[dict[str, Any]],
                 auditor: Callable[[dict[str, Any], dict[str, Any], list[dict[str, Any]]], list[dict[str, Any]]],
                 round_sizes: list[int], compatible_stop: Callable | None = None) -> dict[str, Any]:
    audited = []
    offset = 0
    rounds = []
    for size in round_sizes:
        batch = queue[offset:offset + size]
        if not batch:
            break
        results = auditor(scene_brief, focus, batch)
        mapped = {row["candidate_id"]: row for row in results}
        for index, candidate in enumerate(batch, 1):
            result = mapped[f"C{index:02d}"]
            audited.append({**candidate, **result})
        offset += len(batch)
        structure = [row for row in audited if row["structure_result"] == "MATCH"]
        surface = [row for row in audited if row["surface_result"] == "MATCH"]
        rounds.append({"windows_read": len(audited), "structure_matches": len(structure), "surface_matches": len(surface)})
        if compatible_stop is not None:
            if compatible_stop(audited):
                break
        elif structure and surface:
            break
    if len(audited) > 40:
        raise ProductionRetrievalError("Focused Audit exceeded 40 unique windows")
    return {"audited": audited, "rounds": rounds}


def select_match_examples(audited: list[dict[str, Any]], maximum: int = 3) -> list[dict[str, Any]]:
    structure = [row for row in audited if row.get("structure_result") == "MATCH"]
    surface = [row for row in audited if row.get("surface_result") == "MATCH"]
    candidates = []
    if structure:
        candidates.append((structure[0], "Primary", "structure", structure[0]["structure_allowed_capability"]))
    elif surface:
        candidates.append((surface[0], "Primary", "surface", surface[0]["surface_allowed_capability"]))
    if structure:
        complement = next((row for row in surface if row["raw_excerpt_sha256"] != structure[0]["raw_excerpt_sha256"]), None)
        if complement:
            candidates.append((complement, "Complement", "surface", complement["surface_allowed_capability"]))
    selected = []
    covered = set()
    seen_windows = set()
    for row, selection_role, capability_kind, allowed in candidates:
        ability_key = (capability_kind, allowed.strip())
        if row["raw_excerpt_sha256"] in seen_windows or ability_key in covered:
            continue
        audit_boundary = (row.get("do_not_transfer") or "").strip()
        do_not_transfer = DO_NOT_TRANSFER_MINIMUM + ("；审计补充：" + audit_boundary if audit_boundary else "")
        selected.append({
            "example_id": row["example_id"],
            "source_window": row["source_window"],
            "role": capability_kind,
            "selection_role": selection_role,
            "allowed_capability": allowed,
            "do_not_transfer": do_not_transfer,
            "raw_excerpt_sha256": row["raw_excerpt_sha256"],
            "corpus_polarity": "正例", "audit_verdict": "MATCH", "audit_result": "MATCH",
            "selection_kind": "STRICT_MATCH", "focus_bindings": [],
        })
        seen_windows.add(row["raw_excerpt_sha256"])
        covered.add(ability_key)
        if len(selected) >= maximum:
            break
    return selected


def _fallback_reference(audited: list[dict[str, Any]], full_union: list[dict[str, Any]],
                        focus_id: str, profile: dict[str, Any] | None = None,
                        annotations: list[dict[str, Any]] | None = None,
                        corpus: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Choose one honest positive reference without changing audit verdicts."""
    for row in audited:
        for role, key in (("structure", "structure_result"), ("surface", "surface_result")):
            if row.get(key) == "PARTIAL" and (not row.get("requested_roles") or role in row.get("requested_roles", [])):
                return {
                    "example_id": row["example_id"], "source_window": row["source_window"],
                    "raw_excerpt_sha256": row["raw_excerpt_sha256"], "role": role,
                    "selection_role": "Fallback", "allowed_capability": "仅参考该窗口中可理解的局部交流组织与自然表达，不视为语义匹配",
                    "do_not_transfer": DO_NOT_TRANSFER_MINIMUM, "corpus_polarity": "正例",
                    "audit_verdict": "PARTIAL", "selection_kind": "PARTIAL_REFERENCE",
                    "focus_bindings": [focus_id],
                }
    audited_by_sha = {row["raw_excerpt_sha256"]: row for row in audited}
    if full_union:
        # If the audited set contains no usable partial, prefer the nearest
        # still-unaudited positive from the complete union. This makes the
        # fallback genuinely wider than the Focused Audit Top40.
        row = next((item for item in full_union
                    if item["raw_excerpt_sha256"] not in audited_by_sha), full_union[0])
        seen = audited_by_sha.get(row["raw_excerpt_sha256"])
        verdicts = [] if not seen else [seen.get("structure_result"), seen.get("surface_result")]
        verdict = "NO_MATCH" if "NO_MATCH" in verdicts else "UNJUDGED"
        role = row.get("requested_roles", ["structure"])[0]
        return {
            "example_id": row["example_id"], "source_window": row["source_window"],
            "raw_excerpt_sha256": row["raw_excerpt_sha256"], "role": role,
            "selection_role": "Fallback", "allowed_capability": "仅参考完整场景的自然表达和交流节拍，不视为当前focus的语义匹配",
            "do_not_transfer": DO_NOT_TRANSFER_MINIMUM, "corpus_polarity": "正例",
            "audit_verdict": verdict, "selection_kind": "NEAREST_REFERENCE",
            "focus_bindings": [focus_id],
        }
    ranked = []
    for annotation in annotations or []:
        roles = [role for role in ("structural", "surface_orality")
                 if gate.is_production_positive(annotation, role)]
        if not roles:
            continue
        scores = []
        if "structural" in roles:
            scores.append((gate.structural_score(profile or {}, annotation["retrieval_profile"])[0], "structure"))
        if "surface_orality" in roles:
            scores.append((gate.surface_score(profile or {}, annotation["retrieval_profile"])[0], "surface"))
        score, role = max(scores)
        ranked.append((-score, annotation["scene_id"], role, annotation))
    if ranked:
        _, example_id, role, annotation = sorted(ranked)[0]
        window = (corpus or {})[example_id]["raw_excerpt"]
        return {
            "example_id": example_id, "source_window": window,
            "raw_excerpt_sha256": annotation["raw_excerpt_sha256"], "role": role,
            "selection_role": "Fallback", "allowed_capability": "仅参考完整场景的自然表达和交流节拍，不视为检索命中",
            "do_not_transfer": DO_NOT_TRANSFER_MINIMUM, "corpus_polarity": "正例",
            "audit_verdict": "UNJUDGED", "selection_kind": "CORPUS_REFERENCE",
            "focus_bindings": [focus_id],
        }
    raise ProductionRetrievalError(f"NO_POSITIVE_REFERENCE_AVAILABLE:{focus_id}")


def compatibility_candidate_pool(audited: list[dict[str, Any]], full_union: list[dict[str, Any]],
                                 focus_id: str, annotations: list[dict[str, Any]],
                                 reviews: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve semantic order, then expose bounded surface/corpus fallbacks.

    This does not alter Focused Audit.  It only materializes complete positive
    windows so the guarantee can select *after* literary compatibility.
    """
    annotation_by_id = {row["scene_id"]: row for row in annotations}
    audited_by_sha = {row["raw_excerpt_sha256"]: row for row in audited}
    pool: list[dict[str, Any]] = []

    def add(source: dict[str, Any], role: str, kind: str, verdict: str,
            allowed: str, *, surface: bool = False) -> None:
        review = reviews.get(source["example_id"])
        if review and review.get("raw_excerpt_sha256") and review["raw_excerpt_sha256"] != source["raw_excerpt_sha256"]:
            raise ProductionRetrievalError("LITERARY_REVIEW_WINDOW_SHA_MISMATCH")
        pool.append({
            "example_id": source["example_id"], "source_window": source["source_window"],
            "raw_excerpt_sha256": source["raw_excerpt_sha256"], "role": role,
            "selection_role": "CompatibilityScan", "allowed_capability": allowed,
            "do_not_transfer": DO_NOT_TRANSFER_MINIMUM,
            "corpus_polarity": "正例", "audit_verdict": verdict,
            "selection_kind": kind, "focus_bindings": [focus_id],
            "surface_orality_candidate": surface,
            "literary_compatibility": reviews.get(source["example_id"]),
        })

    for row in audited:
        matches = [("structure", "structure_result", "structure_allowed_capability"),
                   ("surface", "surface_result", "surface_allowed_capability")]
        matched = next(((role, allowed) for role, result, allowed in matches
                        if row.get(result) == "MATCH"), None)
        if matched:
            add(row, matched[0], "STRICT_MATCH", "MATCH", row[matched[1]], surface=matched[0] == "surface")
    for row in audited:
        if row.get("structure_result") == "MATCH" or row.get("surface_result") == "MATCH":
            continue
        partial = next(((role, allowed) for role, result, allowed in (
            ("structure", "structure_result", "structure_allowed_capability"),
            ("surface", "surface_result", "surface_allowed_capability")) if row.get(result) == "PARTIAL"), None)
        if partial:
            add(row, partial[0], "PARTIAL_REFERENCE", "PARTIAL",
                "仅参考局部交流组织与自然表达，不视为语义匹配", surface=partial[0] == "surface")
    for row in full_union:
        if row["raw_excerpt_sha256"] in audited_by_sha:
            continue
        role = row.get("requested_roles", ["structure"])[0]
        add(row, role, "NEAREST_REFERENCE", "UNJUDGED",
            "仅参考完整场景的自然表达和交流节拍，不视为当前focus的语义匹配")
    for row in full_union:
        annotation = annotation_by_id.get(row["example_id"], {})
        if not gate.is_production_positive(annotation, "surface_orality"):
            continue
        add(row, "surface", "CORPUS_REFERENCE", "UNJUDGED",
            "仅学习自然表达、交流节奏与互动质感；不迁移结构", surface=True)
    for row in full_union:
        annotation = annotation_by_id.get(row["example_id"], {})
        if not any(gate.is_production_positive(annotation, role) for role in ("structural", "surface_orality")):
            continue
        add(row, "structure", "CORPUS_REFERENCE", "UNJUDGED",
            "仅参考完整窗口的局部表达质感；不视为语义命中")
    return pool


def _legacy_payload(parent_id: str, focuses: list[dict[str, Any]],
                    annotation_path: Path | None = None, corpus_path: Path | None = None) -> dict[str, Any]:
    annotation_path = annotation_path or gate.DEFAULT_ANNOTATIONS
    corpus_path = corpus_path or gate.DEFAULT_CORPUS
    examples = []
    for focus in focuses:
        result = gate.retrieve_examples(
            focus["profile"], annotation_path, corpus_path, gate.DEFAULT_SCHEMA,
            mode=LEGACY_MODE, config_path=CONFIG,
        )
        for row in result.get("positive_examples", []):
            if any(item["example_id"] == row["scene_id"] for item in examples):
                continue
            examples.append({
                "example_id": row["scene_id"], "source_window": row["raw_excerpt"],
                "role": row.get("role", "legacy_positive"), "selection_role": "Legacy",
                "allowed_capability": "仅按其既有role迁移对话结构或表层口语能力",
                "do_not_transfer": DO_NOT_TRANSFER_MINIMUM,
            })
            if len(examples) == 3:
                break
        if len(examples) == 3:
            break
    return {"parent_id": parent_id, "examples": examples, "selected_count": len(examples), "retrieval_mode": LEGACY_MODE}


class CachedAuditor:
    """Exact request cache; no reuse across model, algorithm, target or window changes."""
    def __init__(self, auditor, directory):
        self.auditor = auditor
        self.directory = Path(directory)
        self.model_id = getattr(auditor, "model_id", AUDIT_MODEL)
        self.hits = 0
        self.misses = 0

    def __call__(self, scene, focus, batch):
        key = canonical_sha({"scene": scene, "focus": focus, "batch": batch,
                             "model": self.model_id,
                             "implementation_sha": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        path = self.directory / (key + ".json")
        if path.is_file():
            cached = read_json(path)
            if cached.get("request_sha") != key or cached.get("result_sha") != canonical_sha(cached.get("results")):
                raise ProductionRetrievalError("AUDIT_CACHE_INTEGRITY_FAILURE")
            self.hits += 1
            return cached["results"]
        results = self.auditor(scene, focus, batch)
        expected = {f"C{i:02d}" for i in range(1, len(batch) + 1)}
        if len(results) != len(expected) or {r.get("candidate_id") for r in results} != expected:
            raise ProductionRetrievalError("AUDIT_CACHE_RESULT_INCOMPLETE")
        self.misses += 1
        self.directory.mkdir(parents=True, exist_ok=True)
        import tempfile, os
        fd, temporary = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump({"request_sha":key, "result_sha":canonical_sha(results), "results":results}, stream, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return results


def run_scene(scene_brief: dict[str, Any], canonical_document: dict[str, Any],
              auditor: Callable | None = None, config_path: Path = CONFIG,
              compatibility_aware: bool = False,
              literary_reviews: dict[str, dict[str, Any]] | None = None,
              production_profile: dict[str, Any] | None = None) -> dict[str, Any]:
    production_profile = production_profile or {}
    from reference_review_request import LiteraryReviewNeeded, resolver
    demand_review = bool(production_profile.get("on_demand_literary_review"))
    compatibility_aware = compatibility_aware or bool(production_profile.get("compatibility_aware_reference_selection"))
    context_sha = canonical_sha({"scene": scene_brief, "canonical": canonical_document})
    if production_profile.get("production_simplification"):
        # Reuse only decisions for this exact target and window. Stable source traits
        # alone cannot certify relational/literary compatibility with a new scene.
        for review in (literary_reviews or {}).values():
            if review.get("target_context_sha256") != context_sha or not review.get("raw_excerpt_sha256"):
                return {"status": "BLOCKED", "writer_payload": None,
                        "log": {"exception": "LITERARY_REVIEW_TARGET_BINDING_REQUIRED", "fallback": False}}
        if not literary_reviews and not demand_review:
            return {"status": "BLOCKED", "writer_payload": None,
                    "log": {"exception": "LITERARY_REVIEWS_REQUIRED_BEFORE_AUDIT", "fallback": False}}
    parent_id = scene_brief.get("target_id") or scene_brief.get("parent_id")
    if not parent_id:
        raise ProductionRetrievalError("scene brief missing target_id/parent_id")
    config = read_json(config_path)
    requested_mode = config.get("retrieval_mode", LEGACY_MODE)
    log: dict[str, Any] = {
        "timestamp": now_iso(), "parent_id": parent_id,
        "target_context_sha256": context_sha,
        "production_profile_sha256": canonical_sha(production_profile), "retrieval_requested": requested_mode,
        "retrieval_executed": None, "fallback": False, "fallback_reason": None,
    }
    focuses: list[dict[str, Any]] = []
    try:
        active_data = validate_active_data_manifest()
        active_corpus = active_data["corpus"]
        active_annotations = active_data["annotations"]
        if requested_mode == LEGACY_MODE:
            focuses = validate_canonical_focuses(canonical_document, parent_id)
            payload = _legacy_payload(parent_id, focuses, active_annotations, active_corpus)
            log.update({"retrieval_mode": LEGACY_MODE, "retrieval_executed": LEGACY_MODE,
                        "focus_ids": [row["retrieval_focus_id"] for row in focuses],
                        "writer_payload_sha": canonical_sha(payload)})
            return {"status": "PASS", "writer_payload": payload, "log": log}
        validate_runtime_config(config)
        focuses = validate_canonical_focuses(canonical_document, parent_id)
        corpus = {row["scene_id"]: row for row in gate.load_jsonl(active_corpus)}
        annotations = gate.load_jsonl(active_annotations)
        ranker = read_json(RANKER)
        active_auditor = auditor or LunaFocusedAuditor()
        if production_profile.get("production_simplification") and production_profile.get("audit_cache_root"):
            active_auditor = CachedAuditor(active_auditor, production_profile["audit_cache_root"])
        guarantee_active = bool(config.get("reference_guarantee"))
        all_selected = []
        covered_parent_capabilities = set()
        focus_logs = []
        all_literary_reviews = literary_reviews or {}
        for focus in focuses:
            if demand_review:
                literary_reviews = {key: value for key, value in all_literary_reviews.items() if value.get("focus_id") == focus["retrieval_focus_id"]}
            queue, abc_counts, full_union = _coarse_queue(
                focus["profile"], annotations, corpus, config, ranker, include_full_union=True)
            if not queue:
                raise ProductionRetrievalError("EMPTY_RETRIEVAL_CANDIDATE_POOL_NO_MODEL_CALL")
            if demand_review:
                for review in (literary_reviews or {}).values():
                    if review.get("focus_id") != focus["retrieval_focus_id"]:
                        raise ProductionRetrievalError("LITERARY_REVIEW_FOCUS_BINDING_REQUIRED")
            def compatible_stop(audited):
                pool = compatibility_candidate_pool(audited, [], focus["retrieval_focus_id"], annotations, literary_reviews or {})
                # Stop only at the highest tier. Lower-tier availability must not
                # pre-empt an as-yet unread STRICT match in the remaining budget.
                pool = [row for row in pool if row["selection_kind"] == "STRICT_MATCH"]
                try:
                    guarantee.select_compatibility_aware_reference(pool, focus["retrieval_focus_id"],
                        review_resolver=resolver(pool, literary_reviews or {}, context_sha, focus["retrieval_focus_id"]) if demand_review else None)
                    return True
                except guarantee.ReferenceGuaranteeError:
                    return False
            audit = _audit_focus(scene_brief.get("scene_writing_brief", scene_brief), focus, queue, active_auditor,
                                 config["focused_audit"]["round_sizes"],
                                 compatible_stop=compatible_stop if production_profile.get("production_simplification") else None)
            compatibility_receipt = None
            if compatibility_aware:
                pool = compatibility_candidate_pool(audit["audited"], full_union,
                    focus["retrieval_focus_id"], annotations, literary_reviews or {})
                picked, compatibility_receipt = guarantee.select_compatibility_aware_reference(
                    pool, focus["retrieval_focus_id"],
                    review_resolver=resolver(pool, literary_reviews or {}, context_sha, focus["retrieval_focus_id"]) if demand_review else None)
                selected = [picked]
            else:
                selected = select_match_examples(audit["audited"], config["max_positive_examples"])
                if not selected and guarantee_active:
                    selected = [_fallback_reference(audit["audited"], full_union, focus["retrieval_focus_id"],
                                                    focus["profile"], annotations, corpus)]
            for row in selected:
                row["focus_bindings"] = [focus["retrieval_focus_id"]]
            counts = {
                "MATCH": sum(row["structure_result"] == "MATCH" or row["surface_result"] == "MATCH" for row in audit["audited"]),
                "PARTIAL": sum((row["structure_result"] == "PARTIAL" or row["surface_result"] == "PARTIAL") and not
                               (row["structure_result"] == "MATCH" or row["surface_result"] == "MATCH") for row in audit["audited"]),
                "NO_MATCH": sum(row["structure_result"] in {"NO_MATCH", "NOT_APPLICABLE"} and
                                row["surface_result"] in {"NO_MATCH", "NOT_APPLICABLE"} for row in audit["audited"]),
            }
            focus_logs.append({
                "focus_id": focus["retrieval_focus_id"],
                "focus_role": focus.get("metadata", {}).get("focus_description", ""),
                "canonical_profile_sha": focus["profile_sha256"], "canonical_profile_version": "v2",
                "ABC": abc_counts,
                "audit": {"model": getattr(active_auditor, "model_id", AUDIT_MODEL), "windows_read": len(audit["audited"]),
                          "rounds": audit["rounds"], **counts},
                "compatibility_selection": compatibility_receipt,
                "selected": [{key: row[key] for key in ("example_id", "selection_role", "role", "allowed_capability",
                                                            "selection_kind", "audit_verdict", "raw_excerpt_sha256")} for row in selected],
            })
            for row in selected:
                existing = next((item for item in all_selected
                                 if item["raw_excerpt_sha256"] == row["raw_excerpt_sha256"]), None)
                if existing:
                    existing["focus_bindings"] = list(dict.fromkeys(existing["focus_bindings"] + row["focus_bindings"]))
                    continue
                capability_key = (row["role"], row["allowed_capability"].strip())
                if not guarantee_active:
                    if any(item["example_id"] == row["example_id"] for item in all_selected):
                        continue
                    if capability_key in covered_parent_capabilities:
                        continue
                if capability_key in covered_parent_capabilities and any(
                        focus["retrieval_focus_id"] in item.get("focus_bindings", []) for item in all_selected):
                    continue
                all_selected.append({**row, "focus_id": focus["retrieval_focus_id"]})
                covered_parent_capabilities.add(capability_key)
                if not guarantee_active and len(all_selected) == config["max_positive_examples"]:
                    break
        payload = {"parent_id": parent_id, "retrieval_mode": PRODUCTION_MODE,
                   "examples": all_selected if guarantee_active else all_selected[:3],
                   "selected_count": len(all_selected) if guarantee_active else min(3, len(all_selected))}
        log.update({
            "retrieval_mode": PRODUCTION_MODE, "retrieval_executed": PRODUCTION_MODE,
            "focus_ids": [row["retrieval_focus_id"] for row in focuses], "focuses": focus_logs,
            "focused_audit_batches": sum(len(row["audit"]["rounds"]) for row in focus_logs),
            "windows_audited": sum(row["audit"]["windows_read"] for row in focus_logs),
            "audit_cache_hits": getattr(active_auditor, "hits", 0),
            "audit_uncached_batches": getattr(active_auditor, "misses", sum(len(row["audit"]["rounds"]) for row in focus_logs)),
            "fallback": False, "writer_payload_sha": canonical_sha(payload),
        })
        return {"status": "PASS", "writer_payload": payload, "log": log}
    except LiteraryReviewNeeded as pending:
        log.update({"fallback": False, "exception": "LITERARY_REVIEW_PENDING",
                    "audit_cache_hits": getattr(active_auditor, "hits", 0),
                    "audit_uncached_batches": getattr(active_auditor, "misses", None)})
        return {"status": "WAITING_LITERARY_REVIEW", "writer_payload": None,
                "review_request": {**pending.request, "scene": scene_brief, "canonical": canonical_document}, "log": log}
    except Exception as error:
        if compatibility_aware:
            log.update({"retrieval_mode": requested_mode, "retrieval_executed": requested_mode,
                        "fallback": False, "fallback_reason": "REFERENCE_COMPATIBILITY_EXHAUSTED" if isinstance(error, guarantee.ReferenceGuaranteeError) else "COMPATIBILITY_PIPELINE_ERROR",
                        "exception": str(error),
                        "focus_ids": [row["retrieval_focus_id"] for row in focuses]})
            return {"status": "BLOCKED", "writer_payload": None, "log": log}
        try:
            if not focuses:
                focuses = validate_canonical_focuses(canonical_document, parent_id)
            _, fallback_corpus, fallback_annotations = active_data_paths()
            payload = _legacy_payload(parent_id, focuses, fallback_annotations, fallback_corpus)
            log.update({
                "retrieval_mode": requested_mode, "retrieval_executed": LEGACY_MODE, "fallback": True,
                "fallback_reason": type(error).__name__, "exception": str(error),
                "focus_ids": [row["retrieval_focus_id"] for row in focuses], "writer_payload_sha": canonical_sha(payload),
            })
            return {"status": "PASS_WITH_FALLBACK", "writer_payload": payload, "log": log}
        except Exception as fallback_error:
            log.update({"fallback": True, "fallback_reason": type(error).__name__, "exception": str(error),
                        "fallback_exception": str(fallback_error)})
            return {"status": "BLOCKED", "writer_payload": None, "log": log}


def compact_runtime_result(result: dict[str, Any]) -> dict[str, Any]:
    """Remove complete source windows before persisting operational evidence."""
    compact = json.loads(json.dumps(result, ensure_ascii=False))
    payload = compact.get("writer_payload")
    if payload:
        for example in payload.get("examples", []):
            example.pop("source_window", None)
    return compact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scene", type=Path, required=True)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--payload-output", type=Path, help="完整结果供Builder精确绑定；不同于操作摘要")
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--literary-reviews", type=Path)
    parser.add_argument("--review-request-output", type=Path)
    args = parser.parse_args()
    result = run_scene(read_json(args.scene), read_json(args.canonical),
                       production_profile=read_json(args.profile) if args.profile else None,
                       literary_reviews=read_json(args.literary_reviews) if args.literary_reviews else None)
    if args.review_request_output and result.get("review_request"):
        args.review_request_output.parent.mkdir(parents=True, exist_ok=True)
        args.review_request_output.write_text(json.dumps(result["review_request"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.payload_output:
        args.payload_output.parent.mkdir(parents=True, exist_ok=True)
        args.payload_output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rendered = json.dumps(compact_runtime_result(result), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] in {"PASS", "PASS_WITH_FALLBACK"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
