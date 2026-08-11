"""Pydantic-AI agent surfaces.

**Scope: exactly two call sites.** Herald's chat, and Fahis's adjudication step.

Not the pipeline. Scout does STAC search, Analyst does windowed COG reads and two
PyTorch forward passes, Oracle does weighted arithmetic over five measured
inputs — none of them call a model, so an agent framework has nothing to offer
them. Keeping it out of `app/eo/`, `app/ml/` and `agents/oracle.py` is what keeps
`score`/`confidence`/`severity` deterministic and `test_oracle.py` possible.

Not advisory generation either. That is a single call with no tools, and its
deterministic `_template` fallback is the most safety-critical line in the
codebase — there is no upside to routing it through another abstraction.

**What the framework buys at these two sites:**

* `deps_type` + `RunContext` express "this run may only read subscriber X's data"
  as a *type*, replacing a closure the old code relied on a test to police.
* `output_type` replaces `llm/client.complete_json`'s hand-rolled
  json_schema → json_object → prompt ladder with maintained equivalents.
* `UsageLimits` bounds requests and tokens per run.
* **`FunctionModel` makes behaviour testable offline** — the existing 224 tests
  are structural (grep for forbidden imports, assert enum parity); they cannot
  assert "the model actually consulted the subscriber's own alerts before
  answering". That is the main reason for this migration.

`app/llm/` is retained, not replaced: `advisory/generator.py` still uses it, and
`llm/budget.py` still owns the cross-request daily ceiling that `UsageLimits`
(per-run only) cannot express.
"""

from app.agentic.provider import available, build_model, model_label

__all__ = ["available", "build_model", "model_label"]
