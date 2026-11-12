# Novel Forge

**A story-first, multi-agent workflow for long-form web fiction.**

[中文](README.md) · [Quick start (Chinese)](docs/quickstart.md)

Novel Forge organizes fiction writing around reader desire, consequential character choices and delivered rewards. It separates planning, prose, cold reading, factual continuity and revision so an outline cannot excuse missing information in the actual text.

## What the system addresses

1. **500+ chapter book analysis:** source-bound indexing, bounded reading windows, chapter causality, foreground/background allocation, information timing, cross-window threads and setup/payoff links. Historical reports cover books indexed at 1,498 and 1,594 chapters, plus a book whose last numbered chapter is 668; 269 windows in total.
2. **Story-unit collaboration:** one task owns a complete narrative unit, with sequential chapter generation, bounded reviews and checkpoints inside it. This reduces fragmented handoffs; it does not mean one model call produces the unit.
3. **Long-term memory:** M1 authority, M2 story state and M3 recent continuity, combined with complete atomic record selection, source hashes, role visibility and fail-closed budgets. The author reports completing a three-million-Chinese-character novel in Dot with stable mainline and plot continuity. This is an author-reported production experience, not a publicly reproduced zero-drift benchmark.
4. **Payoff few-shot design:** retrieval profiles and writer payloads are separate; examples inform desire, consequential choices, usable rewards, weaving and composite climaxes.
5. **Dialogue few-shot retrieval:** an independent Canonical/ABC/Focused Audit pipeline checks structural and surface fit before delivery. The later lightweight route uses a different bounded single-book selection path.
6. **Separate review standards:** unit design rules and v5 comparisons; v7.1 prose, scene allocation, 29 expression issue categories, naturalness checks and cross-scene dialogue review.

See [implementation inventory](reference/README.md), [review standards](standards/README.md) and [evidence boundaries](docs/evidence-and-completeness.md).

## Included

- A 36-step workflow from concept to global story assets, volume planning, chapters and memory updates.
- Eight portable role prompts and 21 historical baseline contracts (Chinese).
- Eight layers of reference analysis: desire, experience, mechanism, events, relationships, scenes, concrete affordances and expression.
- JSON templates, a dependency-free prompt assembler and a release checker, source segment reader and atomic context packer.

```bash
python3 tools/assemble.py --role coordinator --input examples/task.json --output task-prompt.txt
python3 tools/check_release.py
```

Use the generated prompt with your own assistant. Give the cold reader a fresh context containing only prose and necessary previous text. The assembler does not call a model or orchestrate agents.

## Status

This is a methodology and prompt toolkit, not a turnkey autonomous novel generator. Key historical source modules and compiler excerpts are included under reference/, with dependencies documented; private manuscripts, production assets and the full deployment are not distributed. The portable prompts are newly consolidated and have not been validated on a complete long-form project. No claims of bestseller performance or human preference accuracy are made.

Reference texts are user-supplied. No third-party novels, private manuscripts, credentials or production run logs are distributed.

Contributions are most useful when they include a reproducible failure, the proposed change and a before/after comparison. MIT licensed.
