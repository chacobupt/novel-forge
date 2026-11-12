"""Historical function excerpts; not a standalone module. See README.md for omitted project services."""
from __future__ import annotations

def _paths(config: dict[str, Any], allowed_types: set[str] | None = None) -> list[Path]:
    found = []
    if allowed_types is None:
        for item in config.get("source_paths", []):
            path = path_in_project(item)
            found.extend([path] if path.is_file() else [child for child in path.rglob("*") if child.is_file() and child.suffix in {".md", ".json"}])
    if config.get("include_approved_artifacts"):
        for item in registry().get("artifacts", {}).values():
            if item.get("status") in {"approved", "human_approved", "canon"} and item.get("path") and (allowed_types is None or item.get("type") in allowed_types):
                found.append(path_in_project(item["path"]))
    if config.get("include_current_artifacts"):
        artifacts = registry().get("artifacts", {})
        for artifact_id in task().get("current_artifacts", []):
            item = artifacts.get(artifact_id, {})
            if item.get("status") not in TOMBSTONE_STATUSES and item.get("path") and (allowed_types is None or item.get("type") in allowed_types):
                found.append(path_in_project(item["path"]))
    return list({str(path): path for path in found}.values())

def _text_chunks(text: str, max_chunk_chars: int = 1800) -> list[str]:
    blocks = re.split(r"(?=^#{1,6}\s)|\n\s*\n", text, flags=re.MULTILINE)
    chunks: list[str] = []
    current = ""
    for raw in blocks:
        block = raw.strip()
        if not block:
            continue
        if len(block) > max_chunk_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(block[index:index + max_chunk_chars] for index in range(0, len(block), max_chunk_chars))
        elif not current:
            current = block
        elif len(current) + len(block) + 2 <= max_chunk_chars:
            current += "\n\n" + block
        else:
            chunks.append(current)
            current = block
    if current:
        chunks.append(current)
    return chunks

def _artifact_ids_by_path() -> dict[str, str]:
    return {
        item["path"]: artifact_id
        for artifact_id, item in registry().get("artifacts", {}).items()
        if item.get("path") and not item.get("tombstone")
    }

def _compile_context_result(query: str, phase: str, max_chars: int, allowed_types: set[str] | None = None) -> dict[str, Any]:
    policy = read_json(ROOT / "system/memory_policy.json")
    if max_chars < 1:
        raise StoryForgeError("max_chars must be positive")
    phase_limit = int(policy.get("phase_budgets_chars", {}).get(phase, policy.get("hard_max_chars", 12000)))
    effective_max_chars = min(max_chars, phase_limit, int(policy.get("hard_max_chars", 12000)))
    terms = list(dict.fromkeys(re.findall(r"[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", query)))
    result = {"schema_version": "storyforge.context_slice/v2", "project_id": project()["project_id"], "phase": phase, "query": query, "compiled_at": now_iso(), "requested_max_chars": max_chars, "max_chars": effective_max_chars, "layers": {}}
    total = 0
    artifact_ids = _artifact_ids_by_path()
    for name in ("M1", "M2", "M3"):
        config = policy.get("layers", {}).get(name, {})
        budget, used, selected = int(effective_max_chars * config.get("budget_ratio", 0)), 0, []
        candidates = []
        for path in _paths(config, allowed_types):
            text = path.read_text(encoding="utf-8", errors="replace")
            for chunk_index, chunk in enumerate(_text_chunks(text)):
                matched_terms = [term for term in terms if term in chunk]
                if not matched_terms:
                    continue
                exact_score = sum(chunk.count(term) for term in matched_terms)
                heading_bonus = 2 if chunk.startswith("#") and exact_score else 0
                candidates.append((len(matched_terms), exact_score * 5 + heading_bonus, path.stat().st_mtime, path, chunk_index, chunk))
        for coverage, score, _, path, chunk_index, text in sorted(candidates, reverse=True, key=lambda item: (item[0], item[1], item[2], -item[4])):
            excerpt = text[: max(0, budget - used)]
            if excerpt:
                path_text = str(path.relative_to(ROOT))
                selected.append({"section_id": f"{name}-{len(selected) + 1:03d}", "artifact_id": artifact_ids.get(path_text, f"local:{path_text}"), "path": path_text, "chunk_index":chunk_index,"matched_terms":coverage,"score": score, "chars": len(excerpt), "sha256":hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),"excerpt": excerpt})
                used += len(excerpt)
            if used >= budget:
                break
        result["layers"][name] = {"budget": budget, "used": used, "sources": selected}
        total += used
    result["used_chars"] = total
    return result

def compile_context(query: str, phase: str, max_chars: int, output: str | None = None) -> dict[str, Any]:
    result = _compile_context_result(query, phase, max_chars)
    destination = path_in_project(output or "06_记忆/context/latest_context.json")
    write_json(destination, result)
    context_root = ROOT / "06_记忆/context"
    if destination.parent == context_root:
        for stale in context_root.glob("*.json"):
            if stale != destination:
                stale.unlink()
    result["output"] = str(destination.relative_to(ROOT))
    return result

def _json_pointer_value(data: Any, pointer: str) -> Any:
    if pointer in {"", "/"}:
        return data
    if not pointer.startswith("/"):
        raise StoryForgeError(f"JSON selector must be empty or start with '/': {pointer}")
    value = data
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, list):
            id_keys = (
                "id", "record_id", "constraint_id", "pattern_id", "movement_id",
                "hook_id", "unit_id", "chapter_id", "scene_id",
            )
            matches = [
                item for item in value
                if isinstance(item, dict) and any(item.get(key) == part for key in id_keys)
            ]
            if len(matches) == 1:
                value = matches[0]
            elif len(matches) > 1:
                raise StoryForgeError(f"JSON selector is ambiguous: {pointer}")
            else:
                raise StoryForgeError(f"JSON selector not found: {pointer}")
        else:
            raise StoryForgeError(f"JSON selector not found: {pointer}")
    return value

def _atomic_record(spec: str, purpose: str, required: bool) -> dict[str, Any]:
    artifact_id, separator, selector = spec.partition("#")
    if not artifact_id:
        raise StoryForgeError("context record must use ARTIFACT_ID or ARTIFACT_ID#/json/pointer")
    selector = selector if separator else ""
    quarantined = (
        project().get("architecture_migration", {}).get("quarantined_context_records", [])
    )
    for quarantined_spec in quarantined:
        quarantined_artifact, quarantined_separator, quarantined_selector = str(
            quarantined_spec
        ).partition("#")
        quarantined_selector = quarantined_selector if quarantined_separator else ""
        selectors_overlap = (
            not selector
            or not quarantined_selector
            or selector == quarantined_selector
            or selector.startswith(quarantined_selector.rstrip("/") + "/")
            or quarantined_selector.startswith(selector.rstrip("/") + "/")
        )
        if artifact_id == quarantined_artifact and selectors_overlap:
            raise StoryForgeError(
                f"context record is quarantined pending authority migration: {spec}"
            )
    item = artifact_record(artifact_id)
    artifact_type = str(item.get("type") or "")
    if item.get("status") in TOMBSTONE_STATUSES or item.get("tombstone"):
        raise StoryForgeError(f"context record uses retired artifact: {artifact_id}")
    if artifact_type in ROLLING_LEGACY_CONTAMINATION_TYPES:
        raise StoryForgeError(
            f"rolling context rejects legacy contamination: {artifact_id} ({artifact_type})"
        )
    if artifact_type not in ROLLING_CONTEXT_ALLOWED_TYPES[purpose]:
        raise StoryForgeError(
            f"artifact type {artifact_type} is not allowed in {purpose} context: {artifact_id}"
        )
    if purpose == "reader" and selector not in ROLLING_READER_VISIBLE_SELECTORS.get(artifact_type, set()):
        raise StoryForgeError(
            f"reader context may not see {artifact_type} selector {selector or '<whole artifact>'}"
        )
    path = path_in_project(item.get("path", ""))
    if not path.is_file():
        raise StoryForgeError(f"context artifact payload is missing: {artifact_id}")
    actual_sha = sha256_file(path)
    if item.get("sha256") != actual_sha:
        raise StoryForgeError(
            f"context artifact hash is stale: {artifact_id}; refresh or register a new candidate"
        )
    if path.suffix.lower() == ".json":
        value = _json_pointer_value(read_json(path), selector)
        content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    else:
        if selector:
            raise StoryForgeError(f"non-JSON context artifact cannot use a selector: {artifact_id}")
        content = path.read_text(encoding="utf-8")
    content = str(content)
    if not content.strip():
        raise StoryForgeError(f"context record is empty: {spec}")
    markers = [marker for marker in ROLLING_TRUNCATION_MARKERS if marker in content]
    if markers:
        raise StoryForgeError(
            f"context record contains truncation markers ({', '.join(markers)}): {spec}"
        )
    record_id = artifact_id + (f"#{selector}" if selector else "")
    return {
        "record_id": record_id,
        "artifact_id": artifact_id,
        "artifact_type": artifact_type,
        "selector": selector,
        "required": required,
        "chars": len(content),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content": content,
    }

def compile_atomic_context(
    purpose: str,
    required_specs: Iterable[str],
    optional_specs: Iterable[str] = (),
    max_chars: int = 18000,
    output: str | None = None,
) -> dict[str, Any]:
    """Compile whole, explicitly selected records; never summarize or truncate them."""
    if purpose not in ROLLING_CONTEXT_PURPOSES:
        raise StoryForgeError(f"unknown rolling context purpose: {purpose}")
    if max_chars < 1:
        raise StoryForgeError("max_chars must be positive")
    policy = read_json(ROOT / "system/memory_policy.json")
    purpose_limits = policy.get("rolling_context_budgets_chars", {})
    hard_limit = int(policy.get("rolling_context_hard_max_chars", 40000))
    purpose_limit = int(purpose_limits.get(purpose, hard_limit))
    effective_limit = min(max_chars, purpose_limit, hard_limit)
    required_list = list(dict.fromkeys(required_specs))
    optional_list = [item for item in dict.fromkeys(optional_specs) if item not in required_list]
    if not required_list:
        raise StoryForgeError("rolling context requires at least one --required-record")
    required_records = [_atomic_record(spec, purpose, True) for spec in required_list]
    required_chars = sum(item["chars"] for item in required_records)
    if required_chars > effective_limit:
        raise StoryForgeError(
            f"required atomic records need {required_chars} chars but {purpose} budget is "
            f"{effective_limit}; increase the budget or select fewer complete records"
        )
    records = list(required_records)
    used = required_chars
    omitted: list[str] = []
    for spec in optional_list:
        record = _atomic_record(spec, purpose, False)
        if used + record["chars"] > effective_limit:
            omitted.append(record["record_id"])
            continue
        records.append(record)
        used += record["chars"]
    result = {
        "schema_version": "storyforge.longform.context_pack_manifest/v1",
        "project_id": project()["project_id"],
        "purpose": purpose,
        "compiled_at": now_iso(),
        "requested_max_chars": max_chars,
        "max_chars": effective_limit,
        "used_chars": used,
        "complete": True,
        "required_record_ids": [item["record_id"] for item in required_records],
        "records": records,
        "omitted_optional_records": omitted,
        "checks": {
            "missing_required_records": [],
            "truncation_markers": [],
            "legacy_contamination": [],
            "role_visibility_issues": [],
        },
    }
    destination = path_in_project(output or f"06_记忆/context/{purpose}_atomic_current.json")
    write_json(destination, result)
    return result
