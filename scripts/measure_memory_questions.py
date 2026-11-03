"""Offline owner/code prefixes and synthetic #1101 questions; no worker data or model.

PYTHONPATH=fluid .venv/bin/python scripts/measure_memory_questions.py
Catalog sections/tools are rendered by the shipped native/delegated factories.
Memory is measured separately (the harness sends its current snapshot as a note).
Tokens are ceil(chars/4), not billed tokens; delegated CLI's own preset is excluded.
"""
import json
import tempfile
from pathlib import Path
from fluid.codemode.skills import SkillStore
from scripts.measure_prompt_catalog import measure as catalog


def measure():
    results = {}
    for name, jobs in {"pair": {"Code": ("Opus", "Sol")},
                       "four_code_and_review": {"Code": ("Opus", "Sol", "Sonnet", "Haiku"),
                                                "Review": ("Opus", "Sol")}}.items():
        with tempfile.TemporaryDirectory(prefix="question-measure-") as home:
            store = SkillStore(Path(home))
            for job, models in jobs.items():
                for model in models:
                    store.save_memory(f"{job} work goes to {model}.", protected=True,
                                      when="When applying the synthetic owner routing rules")
            footer = store.learner_proposal_footer()
            section = store.render_memory_section()
            results[name] = {"questions": footer.count("[owner question; unresolved conflict]"),
                             "footer_chars": len(footer), "memory_chars": len(section),
                             "footer_tokens_estimate": (len(footer) + 3) // 4,
                             "memory_tokens_estimate": (len(section) + 3) // 4}
    return {"token_method": "ceil(chars/4); not provider billing", "memory": results,
            "catalog": {leg: catalog(native=(leg == "native")) for leg in ("native", "delegated")}}


if __name__ == "__main__":
    print(json.dumps(measure(), indent=2))
