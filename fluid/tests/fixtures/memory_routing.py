"""Text shapes of an owner store's subagent-routing rules (paraphrased; no private data).

Each correction was saved as a new dated row, so six rules about one topic were
live at once and contradicted each other. ``UNRELATED`` are facts of the same
store's other subjects, used to show the overlap hint stays quiet on them.
"""

# (text, key, protected)
ROUTING = [
    ("Owner rule (2026-10-11): backend and general subagent work (backend engineer, docs, "
     "workflows, hub code) runs on GPT Astra, not Opus; Opus is for frontend and rendered "
     "UI only.", "subagents.model-routing", True),
    ("Owner (2026-10-08): GPT 6.1 Sol is poor at frontend work; its UI mockups are only "
     "loose sketches. Final frontend/UI implementation must go to an Opus agent; Sol stays "
     "fine for backend coding and review.", "delegation.frontend_model", True),
    ("Owner rule (2026-10-19): going forward, code work goes to GPT 6.1 Sol subagents "
     "(replaces GPT 6 Astra, set earlier the same day); reviews go to Opus. Frontend and "
     "rendered UI still go to Opus per the Sol frontend rule. Each coding subagent gets its "
     "own worktree and branch.", "workflow.parallel_subagents.model", True),
    ("Owner model routing (2026-10-22): coordinator picks the model per job by quality "
     "judgment. Never use any Fable model. GPT subscription usage is effectively unlimited: "
     "use subagents freely. Claude is the limited budget: check usage before Opus. Default: "
     "risky or cross-cutting code and UI on Opus; bounded backend on GPT 6.1 Sol; routine "
     "reviews on Sonnet, risky reviews on Opus.", "delegation.model", True),
    ("Owner rule (2026-10-23): for now every coding subagent runs on Opus; do not route "
     "subagent code work to GPT 6.1 Sol until the owner lifts this.", None, True),
    ("LIFTED 2026-10-26: the 2026-10-23 for-now Opus-only subagent override was temporary. "
     "Model routing is back to the per-job pick: GPT 6.1 Sol fine for bounded backend code "
     "and read-only review; Opus for risky or cross-cutting code and UI.", None, False),
]

UNRELATED = [
    "On the Linux container worker, managed PostgreSQL tools are on PATH under "
    "/usr/lib/postgresql/16/bin; disposable test clusters need a fresh data dir.",
    "Hub, site and license deploys run from main only, in order hub then site then license, "
    "verifying the live site after each one.",
    "The website and the account hub share one hostname but deploy through separate "
    "main-only workflows; merging does not deploy.",
    "Documentation in docs/content and its frontend mirror under src/lib/docs must stay "
    "identical.",
    "Owner UI rule: no big unofficial warning banners for subscription integrations; keep "
    "neutral, actionable states such as connect a login to use this.",
    "The gaming PC only stays on until 12:30 AM Tuesday through Saturday; finish work there "
    "well before the cutoff and never leave runs going past it.",
    "Public PR bodies refer to private issues only as internal tracking numbers and omit "
    "private infrastructure details.",
]

# The owner's next correction, folded into one standing preference.
PREFERENCE = (
    "Subagent models: pick per job by quality. Never any Fable model. Risky or cross-cutting "
    "code and all frontend/UI on Opus; bounded backend code and read-only review on GPT 6.1 "
    "Sol; routine reviews on Sonnet. GPT usage is effectively unlimited; check Claude usage "
    "before starting Opus workers.")

# A new owner rule on a subject the store has no fact about.
UNRELATED_RULE = (
    "CI policy: every pull request runs the full suite on free GitHub standard runners and "
    "merges only when green and reviewed; ask before any paid runner.")
