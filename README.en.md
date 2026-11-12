# Novel Forge

**A story-first, multi-agent workflow for long-form web fiction.**

[中文](README.md) · [Quick start (Chinese)](docs/quickstart.md)

Novel Forge organizes fiction writing around reader desire, consequential character choices and delivered rewards. It separates planning, prose, cold reading, factual continuity and revision so an outline cannot excuse missing information in the actual text.

## Included

- A 36-step workflow from concept to global story assets, volume planning, chapters and memory updates.
- Eight portable role prompts and 21 historical baseline contracts (Chinese).
- Eight layers of reference analysis: desire, experience, mechanism, events, relationships, scenes, concrete affordances and expression.
- JSON templates, a dependency-free prompt assembler and a release checker.

```bash
python3 tools/assemble.py --role coordinator --input examples/task.json --output task-prompt.txt
python3 tools/check_release.py
```

Use the generated prompt with your own assistant. Give the cold reader a fresh context containing only prose and necessary previous text. The assembler does not call a model or orchestrate agents.

## Status

This is a methodology and prompt toolkit, not a turnkey autonomous novel generator. The private runner is not included because it is coupled to private manuscripts and state. The portable prompts are newly consolidated and have not been validated on a complete long-form project. No claims of bestseller performance or human preference accuracy are made.

Reference texts are user-supplied. No third-party novels, private manuscripts, credentials or production run logs are distributed.

Contributions are most useful when they include a reproducible failure, the proposed change and a before/after comparison. MIT licensed.
