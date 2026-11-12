#!/usr/bin/env python3
"""Candidate-branch reader disclosure state bound to exact chapter bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


class ReaderStateError(ValueError):
    pass


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_bindings(state: dict[str, Any], branch_id: str) -> None:
    if state.get("branch_id") != branch_id:
        raise ReaderStateError("READER_STATE_BRANCH_MISMATCH")
    for row in state.get("source_bindings", []):
        path = Path(row["path"])
        if not path.is_file() or digest(path) != row["sha256"]:
            raise ReaderStateError(f"READER_STATE_UPSTREAM_INVALID:{row.get('chapter_id')}")


def evidence_line(text: str, quote: str) -> int:
    if not quote or text.count(quote) != 1:
        raise ReaderStateError("DISCLOSURE_EVIDENCE_MUST_OCCUR_ONCE")
    return text[:text.index(quote)].count("\n") + 1


def update_state(state_path: Path, branch_id: str, chapter_id: str,
                 chapter_path: Path, claims: list[dict[str, Any]]) -> dict[str, Any]:
    if state_path.exists():
        state = read_json(state_path)
        verify_bindings(state, branch_id)
    else:
        state = {
            "schema_version": "reader-known-state-v2" if claims and all(x.get("fact_id") for x in claims) else "reader-known-state-v1",
            "branch_id": branch_id,
            "source_bindings": [],
            "disclosures": [],
        }
    chapter_path = chapter_path.resolve()
    text = chapter_path.read_text(encoding="utf-8")
    chapter_sha = digest(chapter_path)
    existing = [row for row in state["source_bindings"] if row["chapter_id"] == chapter_id]
    if existing and existing[0]["sha256"] != chapter_sha:
        raise ReaderStateError("READER_STATE_CHAPTER_REVISION_REQUIRES_INVALIDATION")
    if not existing:
        state["source_bindings"].append({
            "chapter_id": chapter_id, "path": str(chapter_path), "sha256": chapter_sha,
        })
    for claim in claims:
        if claim.get("status", "DISCLOSED") != "DISCLOSED":
            raise ReaderStateError("ONLY_REALIZED_DISCLOSURES_CAN_UPDATE_STATE")
        quote = claim["evidence_quote"]
        if state["schema_version"] == "reader-known-state-v2" and not claim.get("fact_id"):
            raise ReaderStateError("DISCLOSURE_FACT_ID_REQUIRED")
        if state["schema_version"] == "reader-known-state-v2" and claim.get("truth_scope") not in {"NARRATOR_FACT", "CHARACTER_BELIEF", "REPORTED_CLAIM"}:
            raise ReaderStateError("DISCLOSURE_TRUTH_SCOPE_REQUIRED")
        if state["schema_version"] == "reader-known-state-v3":
            if not claim.get("fact_id") or claim.get("truth_scope") not in {"NARRATOR_FACT", "CHARACTER_BELIEF", "REPORTED_CLAIM"}:
                raise ReaderStateError("V3_DISCLOSURE_FACT_AND_TRUTH_REQUIRED")
            if claim.get("knowledge_level") not in {"INTRODUCED", "DEMONSTRATED", "EXPLAINED", "DEEPENED"}:
                raise ReaderStateError("V3_DISCLOSURE_KNOWLEDGE_LEVEL_REQUIRED")
            if claim["truth_scope"] != "NARRATOR_FACT" and claim["knowledge_level"] in {"EXPLAINED", "DEEPENED"}:
                raise ReaderStateError("RUMOR_OR_BELIEF_CANNOT_BE_WORLD_EXPLANATION")
        record = {
            **({"fact_id": claim["fact_id"]} if claim.get("fact_id") else {}),
            "entity_id": claim["entity_id"],
            "information": claim["information"],
            "status": "DISCLOSED",
            "source_chapter": chapter_id,
            "source_branch": branch_id,
            **({"truth_scope": claim["truth_scope"]} if claim.get("truth_scope") else {}),
            **({"knowledge_level": claim["knowledge_level"]} if claim.get("knowledge_level") else {}),
            "source_sha256": chapter_sha,
            "evidence": {"quote": quote, "line": evidence_line(text, quote)},
        }
        if record not in state["disclosures"]:
            state["disclosures"].append(record)
    write_json(state_path, state)
    return state


def resolve_view(state_path: Path, branch_id: str, entity_ids: list[str],
                 introduction_plan: list[dict[str, Any]],
                 before_chapter_id: str | None = None,
                 require_fact_ids: bool = False) -> dict[str, Any]:
    state = read_json(state_path)
    verify_bindings(state, branch_id)
    relevant = [
        row for row in state.get("disclosures", [])
        if row["entity_id"] in entity_ids
        and (before_chapter_id is None or int(row["source_chapter"]) < int(before_chapter_id))
    ]
    known = {row["entity_id"] for row in relevant}
    known_facts = {row["fact_id"] for row in relevant if row.get("fact_id")}
    narrator_facts = {row["fact_id"] for row in relevant if row.get("fact_id")
                      and row.get("truth_scope", "NARRATOR_FACT") == "NARRATOR_FACT"}
    is_v3 = state.get("schema_version") == "reader-known-state-v3"
    facts_by_entity = {
        entity_id: {row["fact_id"] for row in relevant
                    if row["entity_id"] == entity_id and row.get("fact_id")}
        for entity_id in entity_ids
    }
    for row in introduction_plan:
        decision = row.get("decision")
        if decision not in {"INTRODUCE", "ALREADY_KNOWN", "DEFER", "NOT_NEEDED"}:
            raise ReaderStateError("INVALID_INTRODUCTION_DECISION")
        needed = row.get("needed_fact_ids", [])
        if require_fact_ids and not isinstance(needed, list):
            raise ReaderStateError("INTRODUCTION_FACT_IDS_REQUIRED")
        if decision == "ALREADY_KNOWN":
            eligible = ({x["fact_id"] for x in relevant if x["entity_id"] == row["entity_id"]
                         and x.get("fact_id") in narrator_facts} if is_v3 else facts_by_entity.get(row["entity_id"], set()))
            if require_fact_ids and (not needed or not set(needed) <= eligible):
                raise ReaderStateError(f"ALREADY_KNOWN_FACT_MISMATCH:{row['entity_id']}")
            if not require_fact_ids and row["entity_id"] not in known:
                raise ReaderStateError(f"ALREADY_KNOWN_WITHOUT_DISCLOSURE:{row['entity_id']}")
    return {
        "schema_version": "reader-view-v2" if require_fact_ids else "reader-view-v1",
        "branch_id": branch_id,
        "state_path": str(state_path.resolve()),
        "state_sha256": digest(state_path),
        "known_disclosures": relevant,
        "known_fact_ids": sorted(known_facts),
        **({"known_narrator_fact_ids": sorted(narrator_facts),
            "knowledge_levels_by_fact_id": {fact_id: sorted({row["knowledge_level"] for row in relevant
                if row.get("fact_id") == fact_id and row.get("knowledge_level")})
                for fact_id in known_facts}}
           if is_v3 else {}),
        **({"known_fact_ids_by_entity": {key: sorted(value) for key, value in facts_by_entity.items()}}
           if require_fact_ids else {}),
        "introduction_plan": introduction_plan,
        "planned_is_not_disclosed": True,
    }


def invalidate_from(state_path: Path, branch_id: str, chapter_id: str) -> dict[str, Any]:
    state = read_json(state_path)
    if state.get("branch_id") != branch_id:
        raise ReaderStateError("READER_STATE_BRANCH_MISMATCH")
    order = [row["chapter_id"] for row in state.get("source_bindings", [])]
    if chapter_id not in order:
        return state
    cut = order.index(chapter_id)
    invalid = {row["chapter_id"] for row in state["source_bindings"][cut:]}
    state["source_bindings"] = state["source_bindings"][:cut]
    state["disclosures"] = [row for row in state["disclosures"] if row["source_chapter"] not in invalid]
    write_json(state_path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    update = sub.add_parser("update")
    update.add_argument("--state", type=Path, required=True)
    update.add_argument("--branch-id", required=True)
    update.add_argument("--chapter-id", required=True)
    update.add_argument("--chapter", type=Path, required=True)
    update.add_argument("--claims", type=Path, required=True)
    invalid = sub.add_parser("invalidate-from")
    invalid.add_argument("--state", type=Path, required=True)
    invalid.add_argument("--branch-id", required=True)
    invalid.add_argument("--chapter-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "update":
            result = update_state(args.state, args.branch_id, args.chapter_id,
                                  args.chapter, read_json(args.claims)["claims"])
        else:
            result = invalidate_from(args.state, args.branch_id, args.chapter_id)
    except (OSError, KeyError, ReaderStateError, json.JSONDecodeError) as error:
        print(f"READER_STATE_BLOCKED:{error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
