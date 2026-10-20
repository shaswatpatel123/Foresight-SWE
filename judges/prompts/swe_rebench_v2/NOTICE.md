# Vendored SWE-rebench-V2 prompts

These four Jinja templates are copied verbatim from the SWE-rebench-V2 repo
(https://github.com/SWE-rebench/SWE-rebench-V2), MIT-licensed (see `LICENSE`),
used here to run the LLM-judge POC over our validated fix-chains.

- `pr_description.j2`  — generate a clean problem statement from PR text + patch
- `interfaces.j2`      — extract test-checked signatures (feeds `meta_info`)
- `meta_info.j2`       — RL-validity + `pr_categories` + difficulty classifier
- `verified_extra.j2`  — issue-clarity score 0–3 (needs 4 calibration anchors)

Consumed by `scripts/render_judge_prompts.py`. See `docs/JUDGE_POC.md`.

Citation: Badertdinov et al., *SWE-rebench V2: Language-Agnostic SWE Task
Collection at Scale*, arXiv:2602.23866.
