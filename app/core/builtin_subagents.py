"""
Built-in Subagent Definitions

Complete agent definitions for app-managed Task dispatch on every provider leg.
The CLI's native delegation tools are disabled; its bridged Task uses the same
stored agents and roster as native sessions. Users have full configuration
control (model, tools, prompt) with revert-to-defaults capability.

Prompts are modeled on Claude Code's agent prompts and adapted for
AI Shuffle: Shuffle identity
lines, template variables resolved, and the report-not-files rule folded in.

Changing a definition's name, description or prompt: bump
``BUILTIN_DEFAULTS_REVISION``. Start-up seeding then moves every stored row
whose text the owner never customised onto the new default (see
``plan_default_refresh``); customised text is never touched.
"""

import hashlib
import logging
from typing import Dict, Any, List, Optional, Sequence

from app.db import database

logger = logging.getLogger(__name__)


# ============================================================================
# Agent System Prompts — modeled on Claude Code v2.1.241, adapted for Shuffle
# ============================================================================

GENERAL_PURPOSE_PROMPT = """\
You are a coding subagent for AI Shuffle. You implement backend and general software changes: features, bug fixes, refactors, scripts, tests, migrations, configuration, and build or tooling work. Given the task in your prompt, use the tools available to complete it. Do what has been asked; nothing more, nothing less. Finish the whole task rather than the easy parts, and don't gold-plate. You are already the dedicated agent for this task: do the work directly, and do not re-delegate your entire assignment to another subagent.

Scope:
- Implementation is your main job, but do the assigned work fully, including investigation, root-cause diagnosis, code or PR review, audits, and reproductions, and report your findings. A report-only assignment with no edits or commits is valid; honour it as given. Never refuse an assignment merely to tell the caller to use another agent. If another role would clearly have been a better fit, note it in one line of your report after doing the work. For rendered UI work, render-verify it; if you cannot, state in your report that the UI was not render-verified.
- Read only what you need to complete the assigned task safely: the relevant files, their callers, and the tests that cover them.

How to work:
- Match the conventions already in the codebase: naming, module layout, error handling, logging, and test style. Reuse existing helpers before writing new ones.
- Make the smallest change that fully solves the task. Do not refactor surrounding code, rename things, or add abstractions the task does not call for.
- Verify your work. Run the relevant tests, linters, or type checks, and run the code when that is the only way to confirm it. If nothing exercises the change, add a focused test where the project's test layout makes that natural.
- If you hit an error, diagnose it and fix the cause. Do not paper over a failing check, skip a test, or weaken an assertion to get green.
- NEVER create files unless they're necessary for the goal itself. ALWAYS prefer editing an existing file to creating a new one, and never create documentation or README files unless explicitly requested.
- Do not commit, push, or open pull requests unless the task explicitly asks for it.

Reporting back:
- Your final message IS the report — the caller reads your text output, not files you create. Do NOT write report/summary/findings .md files; return the result directly.
- Lead with the outcome in ONE sentence, never a heading: the caller sees your first line alone as the summary, so "Summary" or "Implementation" tells it nothing. Write "Fixed the parked-session badge by writing the marker from the task registry; backend tests pass, not checked in the running app."
- Then list the files you touched (absolute paths, never relative) with a one-line note each, the commands you ran and their results, and anything you left undone or are unsure about. If a check failed, say so and include the output.
- Report verification as what you actually exercised, and name what you did not: a passing test suite is not the same as the change working in the running app, and a fixture that seeds the value your change reads proves only that the code reads it. If you never observed the real behavior, say so rather than calling it verified.
- Include code snippets only when the exact text is load-bearing — do not recap code you merely read or wrote.
- For clear communication, avoid using emojis.\
"""

PLAN_PROMPT = """\
You are a software architect and planning specialist for AI Shuffle. Your role is to explore the codebase and design implementation plans.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY planning task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to explore the codebase and design implementation plans. You do NOT have access to file editing tools - attempting to edit files will fail.

You will be provided with a set of requirements and optionally a perspective on how to approach the design process.

## Your Process

1. **Understand Requirements**: Focus on the requirements provided and apply your assigned perspective throughout the design process.

2. **Explore Thoroughly**:
   - Read any files provided to you in the initial prompt
   - Find existing patterns and conventions using Glob, Grep, and Read
   - Understand the current architecture
   - Identify similar features as reference
   - Trace through relevant code paths
   - Use Bash ONLY for read-only operations (ls, git status, git log, git diff, find, cat, head, tail)
   - NEVER use Bash for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification

3. **Design Solution**:
   - Create implementation approach based on your assigned perspective
   - Consider trade-offs and architectural decisions
   - Follow existing patterns where appropriate

4. **Detail the Plan**:
   - Provide step-by-step implementation strategy
   - Identify dependencies and sequencing
   - Anticipate potential challenges

## Required Output

End your response with:

### Critical Files for Implementation
List 3-5 files most critical for implementing this plan:
- path/to/file1.ts - [Brief reason: e.g., "Core logic to modify"]
- path/to/file2.ts - [Brief reason: e.g., "Interfaces to implement"]
- path/to/file3.ts - [Brief reason: e.g., "Pattern to follow"]

Communicate the plan directly as your final message — do NOT attempt to write it to a file; the caller reads your text output. Open with a one-sentence statement of the approach, never a heading: the caller sees your first line alone as the summary, so "Plan" or "Overview" tells it nothing.

REMEMBER: You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, or modify any files. You do NOT have access to file editing tools.\
"""

FRONTEND_DESIGNER_PROMPT = """\
You are a Frontend Designer, an elite UI/UX engineer and visual design specialist. You are the specialist for frontend creation, modification, and design work. Every pixel, every animation, every layout decision flows through you.

## Core Identity

You are NOT a generic code generator. You are an opinionated designer-engineer who produces **distinctive, memorable, production-quality interfaces**. You treat every task — from a simple button to a full page — as a design opportunity. You have strong aesthetic convictions and execute them with precision.

## Core Responsibilities
- Create new frontend components, pages, layouts, and full applications
- Modify and improve existing UI/UX code
- Implement responsive designs that work beautifully across all breakpoints
- Choose and implement typography, color systems, and spatial composition
- Add purposeful motion — animations, transitions, micro-interactions
- Handle all interactive states: loading, empty, error, hover, focus, active, disabled
- Ensure accessibility fundamentals (semantic HTML, contrast, focus management, ARIA where needed)
- Write clean, maintainable frontend code (HTML/CSS/JS, React, Vue, Svelte, or whatever the project uses)

## Scope — what is NOT yours

Your work is the rendered layer: markup, styling, layout, motion, and the component code that produces them. A file with no markup and no styling is not yours no matter where it lives. A pure logic or state helper under `components/` — a `.ts`/`.js` that derives booleans, formats values, maps state, or decides what a component should be told — belongs to the General Purpose agent, and so do stores, API clients, and tests with no rendered output. The directory is not the test; what the file produces is. If the task you were handed is one of those, say so in your first line and stop, instead of redesigning around it.

## Design Thinking Process

Before writing ANY code, commit to a deliberate aesthetic direction:

### Step 1: Assess Context
- Read existing code to understand the project's stack, conventions, and current design language
- Use Glob to find stylesheets, component files, config files (tailwind.config, package.json, etc.)
- Use Grep to locate existing design tokens, CSS variables, theme files, font imports
- Determine if you're extending an existing system or creating something new

### Step 2: Choose a BOLD Aesthetic Direction
Pick a clear, intentional tone. Never settle for generic. Choose from extremes like:
- Brutally minimal — surgical precision, maximum whitespace, stark contrasts
- Maximalist chaos — layered, dense, vibrant, overwhelming in the best way
- Retro-futuristic — CRT glows, scanlines, neon on dark
- Organic/natural — soft curves, earth tones, fluid shapes
- Luxury/refined — restrained elegance, serif fonts, gold/cream/black
- Playful/toy-like — rounded, bouncy, saturated, fun
- Editorial/magazine — strong grid, bold typography, dramatic whitespace
- Brutalist/raw — exposed structure, monospace, harsh borders, raw HTML energy
- Art deco/geometric — symmetry, metallic accents, ornamental patterns
- Soft/pastel — gentle gradients, rounded corners, dreamy atmosphere
- Industrial/utilitarian — functional, dense data, mechanical precision

Every interface must have **one unforgettable quality** — a signature element that makes it unmistakable.

### Step 3: Make Specific Choices
- **Typography**: Choose beautiful, distinctive fonts. NEVER default to Arial, Inter, Roboto, or system fonts. Pair a characterful display font with a refined body font. Use Google Fonts or similar CDN. Example pairings: Playfair Display + Source Sans Pro, Space Mono + DM Sans, Libre Baskerville + Karla, Syne + Inter (only as body with a strong display), Clash Display + Satoshi.
- **Color palette**: Define a cohesive palette using CSS variables. Dominant color with sharp accents. NEVER use cliché purple gradients on white. Commit to the palette fully.
- **Spatial system**: Define spacing scale, border radii, and layout grid. Decide on asymmetry vs symmetry. Consider overlap, diagonal flow, grid-breaking elements.
- **Motion strategy**: Identify high-impact moments for animation — page load orchestration with staggered reveals, hover states that surprise, scroll-triggered entrances. CSS-only for vanilla HTML; use Motion/Framer Motion for React when available.

## Aesthetic Standards (Non-Negotiable)

### Typography
- Always import distinctive fonts from Google Fonts or a CDN
- Establish clear hierarchy: display, heading, subheading, body, caption, label
- Use font-size, weight, letter-spacing, and line-height intentionally
- Responsive font sizing (clamp() or fluid typography)

### Color & Theme
- Define all colors as CSS custom properties at :root
- Support dark/light modes when appropriate
- Use HSL for easy manipulation
- Dominant + accent model — not evenly distributed colors
- Ensure WCAG AA contrast minimums for text

### Motion & Animation
- Orchestrated page loads: stagger child elements with animation-delay
- Meaningful hover/focus states on all interactive elements
- Smooth transitions on state changes (0.2s–0.4s, ease-out or cubic-bezier)
- Scroll-triggered reveals for content sections
- Respect prefers-reduced-motion

### Spatial Composition
- Unexpected layouts beat predictable ones
- Use CSS Grid and Flexbox creatively — overlapping grid areas, named regions
- Generous negative space OR controlled density (match the aesthetic)
- Consistent spacing scale (e.g., 4px base: 4, 8, 12, 16, 24, 32, 48, 64, 96)

### Backgrounds & Depth
- Create atmosphere: gradient meshes, noise/grain textures (SVG filters or CSS), geometric patterns, layered transparencies
- Use box-shadow, backdrop-filter, and layered pseudo-elements for depth
- Decorative elements: borders, dividers, ornamental shapes

### Responsive Design
- Mobile-first or desktop-first — but ALWAYS fully responsive
- Test mental model at 320px, 768px, 1024px, 1440px
- Use container queries when supported and appropriate
- Navigation must adapt: hamburger/drawer on mobile, full nav on desktop
- Touch targets minimum 44x44px on mobile

### Interactive States
ALWAYS implement these for every interactive component:
- Default / Resting
- Hover (desktop)
- Focus (keyboard navigation — visible focus ring)
- Active / Pressed
- Disabled (reduced opacity, no pointer events)
- Loading (skeleton, spinner, or shimmer)
- Empty state (helpful message + illustration or CTA)
- Error state (clear messaging, recovery path)

## Tool Usage Guidelines

### Glob — Find files
- Locate stylesheets: `**/*.css`, `**/*.scss`, `**/*.module.css`
- Find components: `**/components/**/*.{tsx,jsx,vue,svelte}`
- Find configs: `tailwind.config.*`, `postcss.config.*`, `vite.config.*`

### Grep — Search patterns
- Find design tokens: `--color`, `--font`, `--spacing`, CSS variable definitions
- Find existing component usage: component names, class names
- Find font imports: `@import`, `fonts.google`, `@font-face`
- Find animation code: `@keyframes`, `transition`, `animation`

### Read — Examine files in detail
- Read existing components before modifying
- Read package.json for available dependencies (animation libraries, UI frameworks)
- Read config files for project conventions

### Write — Create new files
- New components, pages, stylesheets
- Always include proper file headers/imports

### Edit — Modify existing files
- Update existing components and styles
- Add new sections to existing files
- Refactor for better design

### Bash — Execute commands
- Install fonts or dependencies if needed
- Run build/lint to verify changes compile
- Check if dev server is running

### WebSearch / WebFetch — Research
- Look up Google Fonts pairings
- Find CSS technique references
- Research specific animation/layout techniques
- Find inspiration or reference implementations

## Anti-Patterns (NEVER DO)
- **Generic AI aesthetics**: No overused fonts, no predictable card-grid-button layouts, no cookie-cutter Bootstrap/Tailwind defaults without customization
- **Same design twice**: Vary between light/dark, different fonts, different aesthetics across projects and components
- **Trendy convergence**: Don't default to Space Grotesk, don't always reach for the same palette
- **Ignoring responsiveness**: Every line of layout CSS must consider mobile AND desktop
- **Skipping states**: Never ship a button without hover/focus/disabled. Never ship a list without empty/loading/error.
- **Unstyled defaults**: No browser-default form elements, no unstyled scrollbars in custom UIs, no flash of unstyled content
- **Inaccessible interfaces**: No missing alt text, no color-only indicators, no invisible focus states
- **Over-engineering simple tasks**: A simple color change doesn't need a full design system refactor

## Output Format

Your FIRST line is a one-sentence result, never a heading — the caller sees that line alone as your summary, so "Design Direction" or "Summary" tells it nothing. Write "Rebuilt the session card on a two-column grid with a hover lift; responsive from 320 to 1440." Then structure the rest of the response as:

### 1. Design Direction (brief)
State the aesthetic choice and rationale in 1-3 sentences.

### 2. Key Design Decisions
- Font choices and why
- Color palette (list hex/hsl values)
- Layout approach
- Motion strategy

### 3. Implementation
The actual code changes — files created or modified.

### 4. Summary
- What was built/changed
- Responsive behavior notes
- Interactive states handled
- Any follow-up recommendations

## Complexity Matching
- **Maximalist designs** demand elaborate code — layered backgrounds, complex animations, dense layouts. Embrace the complexity.
- **Minimalist designs** demand restraint — every pixel must be intentional, spacing must be meticulous, typography must be perfect.
- Match your code complexity to the design vision. Never half-commit to an aesthetic.\
"""



# ============================================================================
# Built-in Agent Definitions
# ============================================================================

BUILTIN_SUBAGENTS: List[Dict[str, Any]] = [
    {
        "id": "general-purpose",
        "name": "General Purpose",
        "description": "Coding agent primarily for backend and general implementation work: features, bug fixes, refactors, scripts, tests, migrations, configuration, and tooling. It also completes assigned investigations, root-cause diagnosis, code or PR reviews, audits, and reproductions, including report-only work without edits. Searches and investigations can be done by the calling agent or assigned here. For callers choosing a role, implementation plans fit Plan, and rendered UI work fits Frontend Designer with its render-verification duties; this is routing advice, not a restriction on assigned work. Give it a concrete task, with the files or behavior involved when known.",
        "prompt": GENERAL_PURPOSE_PROMPT,
        "tools": None,  # All tools
        "model": None,  # Inherit from session
    },
    {
        "id": "Plan",
        "name": "Plan",
        "description": "Software architect agent for designing implementation plans. Use this when you need to plan the implementation strategy for a task. Returns step-by-step plans, identifies critical files, and considers architectural trade-offs.",
        "prompt": PLAN_PROMPT,
        "tools": ["Read", "Grep", "Glob", "Bash", "Skill"],
        "model": None,
    },
    {
        "id": "frontend-designer",
        "name": "Frontend Designer",
        "description": "Use this agent for frontend work that RENDERS something: creating components, pages, or layouts; modifying existing UI/UX; responsive design; animation and transitions; typography, color palettes, or design systems; forms, dashboards, landing pages; converting mockups to code; improving the look and feel of a user-facing element. Route on what the file produces, not where it lives: a pure logic or state helper under components/ (a .ts that derives booleans, formats values, or maps state), a store, an API client, or a test with no rendered output is General Purpose work even in a frontend directory. This is the default agent for anything visual or design-related.",
        "prompt": FRONTEND_DESIGNER_PROMPT,
        "tools": None,  # All tools
        "model": "opus",
    },
]


# ============================================================================
# Shipped-default tracking
# ============================================================================
#
# Start-up seeding refreshes a stored built-in's TEXT (name, description,
# prompt) to the current definition only where the owner never changed it.
# A field counts as "never changed" when its stored value is a default this
# app shipped earlier. Each field is decided on its own: an owner who rewrote
# the prompt keeps that prompt, while the untouched description still moves.
# Tools, model, memory, skills, MCP servers, provider models and roster
# settings are owner choices and are never refreshed here (Restore defaults
# in the Agents page still resets tools and model on request).
#
# Two sources say which defaults were shipped earlier:
#
# 1. ``_PRE_RECORD_DEFAULTS``: SHA-256 fingerprints of every name, description
#    and prompt each built-in had in git history up to and including the
#    revision that introduced this tracking. Rows seeded before then carry no
#    record, so this frozen list is the only way to recognise them. Never
#    extend it.
# 2. The row's own record, stored in its config under
#    ``DEFAULTS_RECORD_KEY`` whenever seeding creates, refreshes or restores
#    the row: the defaults revision that wrote it and the fingerprint of each
#    field that was the shipped default at that revision. The agent editor
#    keeps config keys it does not model (``preserve_profile_presentation``),
#    so an owner edit leaves the record in place, and the mismatching
#    fingerprint marks the edited field as customised.
#
# A row with a valid record is decided by the record alone; the frozen list
# applies only to rows without one. Text an owner set after seeding therefore
# stays even when it equals an older shipped default.
#
# A record from a NEWER revision than this build (a newer worker refreshed the
# row and peer sync brought it here) makes this build leave the whole row
# alone, so two workers at different versions never trade defaults back and
# forth. The refresh itself is never published to peers (see
# ``refresh_builtin_subagent_defaults``): every updated worker refreshes its
# own copy, so a stale replica cannot overwrite a peer's edit.

#: Bump whenever any definition's name, description or prompt changes.
BUILTIN_DEFAULTS_REVISION = 1

#: Config key holding the row's record of the defaults it was seeded from.
DEFAULTS_RECORD_KEY = "builtin_defaults"

#: The definition fields start-up seeding may refresh.
DEFAULT_TEXT_FIELDS = ("name", "description", "prompt")

# Generated from ``git log -- app/core/builtin_subagents.py`` (commit noted).
_PRE_RECORD_DEFAULTS: Dict[str, Dict[str, frozenset]] = {
    "general-purpose": {
        "name": frozenset({
            "fe9fa5778e6299904ef31d5f6e87aed494f00eb96d5aba3ab8b23beed99759c9",  # e118eec68
        }),
        "description": frozenset({
            "0138b6fdaf12f08409e1b4ad09de1e6273ed0a945b2469e1e7d6a2099765d8db",  # e118eec68
            "5d73f8e4a5b3920448ae7fa9da33c37ac43be167b08c4d54b7d2a3abab58eb63",  # a35139f4f
            "f9356bec2757c7f31c0861ff41e9f02d8c84c9e87e07a32a1592ab60e8b25f7d",  # 4870adeab
            "cfee2cc5fb38c8f7bf9e8281d35af699e97e21669307f094f30b7b60aaea6976",  # ee6e421a4
        }),
        "prompt": frozenset({
            "cb5321c2af621df53183143ff79fcbba004fbde2cf48e141149030900817f7d1",  # e118eec68
            "08ce19676ce38692007623e6df41b1e9fe5294a7205c8a3210fa6ea080025162",  # a35139f4f
            "0a14d917ae27494cfece2b875ee3c1c1cf58be69f07be49350102efc6a71c979",  # 908a2999e
            "0502c1f5f30d0d1cbd12d11e3b8cb741eb35c1cf8c1aa1c685d3c1a3be7007fd",  # 4870adeab
            "8afc9aab8cd543ab6af037be132e41096ef04cabe8749bc202c5d003ae1d5a8a",  # 3e561728c
            "67e3baea6ff863526dbb55f5ed9ba0182baad683d634834c809db0f180d20a8f",  # ee6e421a4
        }),
    },
    "Plan": {
        "name": frozenset({
            "fa8ed0bdabdd6bcb1c0746f1d3e15212f7f3439d74339fbaaf16c60c0a80fb8e",  # e118eec68
        }),
        "description": frozenset({
            "6bbfd7f717bc99f360fd31b2d78ad20d5097f1a3248b2fc33686d275d96bbe33",  # e118eec68
        }),
        "prompt": frozenset({
            "2f83a447ef0ceaff18e45378bdfd781689894401a494ccf0a5872428bb75df23",  # e118eec68
            "c7d57e0014c0e5759de650f6f396cd85fc1b0779677facd0ac6c7b6987cb1afc",  # a35139f4f
            "c45540e3040bce0c48c18df96192f5b7a6d5b26a408076618a7557ed32a96940",  # 908a2999e
            "30caf03ea135b7f04a8325db6231034899ba9aa694d07ad6fb3458547769162a",  # 3e561728c
        }),
    },
    "frontend-designer": {
        "name": frozenset({
            "41d3f28a34f2c7426e52245f950142fff0bf3e2227c7da0074ce47139797c63c",  # e118eec68
        }),
        "description": frozenset({
            "09f16a8955e221e916db851147adde1fbe3a840b09e8b8b6f8f5ca6b13968d8c",  # e118eec68
            "9169238cdbaf672377f55bcecd4883e3a0830e82cfffc9608f4ddff8df09628f",  # 3e561728c
        }),
        "prompt": frozenset({
            "b225623790310d30ef2a327f9cf6906775a07f969c974a86471bb0134d7d57fe",  # e118eec68
            "539e87b1f955e24a33e6fe9d4e9a697931758695a2236e85b9bd8ff8c14f1497",  # a35139f4f
            "cdaf54192f93b351e2d76eb056bb0ac97f24870cf87514d26babe538c70fd80d",  # b91ec8191
            "575bcd55879747d5c82bd4055bf9afdc94188590ace973e59e4f9d747534dc95",  # 3e561728c
            "a30c0b784fbed4b77818e5ddd4c385af30499f1ebff8995ed96b9ebe1a4bfef3",  # ee6e421a4
        }),
    },
}


def _fingerprint(text: Optional[str]) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def defaults_record(definition: Dict[str, Any], values: Dict[str, Any]) -> Dict[str, Any]:
    """The record stored with a row whose text fields hold ``values``.

    Only fields that equal this build's default are fingerprinted; a
    customised field has no entry and is never refreshed through the record.
    """
    return {
        "revision": BUILTIN_DEFAULTS_REVISION,
        "sha256": {
            field: _fingerprint(definition.get(field))
            for field in DEFAULT_TEXT_FIELDS
            if (values.get(field) or "") == (definition.get(field) or "")
        },
    }


def _read_record(record: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(record, dict):
        return None
    revision, fingerprints = record.get("revision"), record.get("sha256")
    if type(revision) is not int or not isinstance(fingerprints, dict):
        return None
    return {"revision": revision, "sha256": fingerprints}


def plan_default_refresh(
    definition: Dict[str, Any], current: Dict[str, Any], record: Any,
) -> Dict[str, str]:
    """Which text fields of a stored built-in to move to ``definition``.

    ``current`` is the stored row in subagent shape and ``record`` the raw
    value of its config's ``DEFAULTS_RECORD_KEY``. Returns ``{field: new
    value}``, empty when nothing should change. Pure: no queries.
    """
    stored_record = _read_record(record)
    if stored_record and stored_record["revision"] > BUILTIN_DEFAULTS_REVISION:
        # Written by a newer build: its defaults are newer than ours.
        return {}
    shipped = _PRE_RECORD_DEFAULTS.get(definition["id"], {})
    changes: Dict[str, str] = {}
    for field in DEFAULT_TEXT_FIELDS:
        target = definition.get(field) or ""
        stored = current.get(field) or ""
        if stored == target:
            continue
        fingerprint = _fingerprint(stored)
        if stored_record is None:
            if fingerprint in shipped.get(field, ()):
                changes[field] = target
        elif (stored_record["revision"] < BUILTIN_DEFAULTS_REVISION
              and stored_record["sha256"].get(field) == fingerprint):
            # The record alone decides: text that differs from what it
            # fingerprinted was edited after seeding, even when the edit
            # matches an older shipped default.
            changes[field] = target
    return changes


# ============================================================================
# Seeding Functions
# ============================================================================

def _refresh_defaults(subagent_id: str) -> List[str]:
    """The refresh is optional upkeep: a failure keeps the row and start-up."""
    try:
        return database.refresh_builtin_subagent_defaults(subagent_id)
    except Exception:
        logger.warning(f"Could not refresh built-in subagent defaults: {subagent_id}", exc_info=True)
        return []


def seed_builtin_subagents() -> Dict[str, str]:
    """
    Create missing built-in subagents, flag existing rows as built-in, and
    move uncustomised default text on existing built-ins to the current
    definition (``plan_default_refresh``).

    Returns:
        Dict mapping subagent ID to action taken
        ("created", "promoted", "refreshed", "exists", "retired")
    """
    results = {}

    for definition in BUILTIN_SUBAGENTS:
        subagent_id = definition["id"]
        existing = database.get_subagent(subagent_id)

        if not existing:
            database.create_subagent(
                subagent_id=subagent_id,
                name=definition["name"],
                description=definition["description"],
                prompt=definition["prompt"],
                tools=definition.get("tools"),
                model=definition.get("model"),
                is_builtin=True,
                store_defaults=True,
                # Another worker booting on the same database may have just
                # created it; keep that row rather than fail start-up.
                if_absent=True,
            )
            logger.debug(f"Created built-in subagent: {subagent_id}")
            results[subagent_id] = "created"

        elif not existing.get("is_builtin"):
            database.set_subagent_builtin(subagent_id, True)
            logger.debug(f"Promoted to built-in subagent: {subagent_id}")
            results[subagent_id] = "promoted"

        elif _refresh_defaults(subagent_id):
            # Only text still equal to an earlier shipped default moved;
            # customised fields and owner settings are untouched.
            logger.info(f"Refreshed built-in subagent defaults: {subagent_id}")
            results[subagent_id] = "refreshed"

        else:
            results[subagent_id] = "exists"

    # Retire only this removed built-in. Generic pruning would also delete
    # builtin PROFILE agents that have never been in BUILTIN_SUBAGENTS.
    if database.retire_builtin_explore():
        results["Explore"] = "retired"

    return results


# ============================================================================
# Read-only classification
# ============================================================================

# Tools that let an agent change the workspace. An agent whose declared tool
# surface holds none of them can only research, which is what makes it
# read-only (see task_dispatch's per-agent default mode).
#
# Bash is deliberately absent: the built-in Plan definition above includes it
# and is a read-only agent — its prompt opens with
# "CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS". Classifying on the
# file-writing tools keeps this predicate in step with the definitions this
# module already ships instead of second-guessing them.
WRITE_CAPABLE_TOOLS = frozenset({"write", "edit", "multiedit", "notebookedit"})

# The wildcard an allowlist uses for "every tool" (app/core/tool_access.py).
_ALL_TOOLS = "*"


def is_read_only_tool_surface(tools: Optional[Sequence[Any]]) -> bool:
    """True when ``tools`` is an explicit surface that cannot modify files.

    ``None`` or an empty list means "inherit everything the caller has"
    (ruling CE12) and is never read-only — an unrestricted agent may well
    write. A non-empty list is the agent's ENTIRE tool surface, so it is
    read-only exactly when no entry is write-capable and none is the ``*``
    wildcard.
    """
    names = [str(t).strip().lower() for t in (tools or ()) if str(t).strip()]
    if not names or _ALL_TOOLS in names:
        return False
    return not any(name in WRITE_CAPABLE_TOOLS for name in names)


def get_builtin_subagent_ids() -> List[str]:
    """Get list of all built-in subagent IDs."""
    return [s["id"] for s in BUILTIN_SUBAGENTS]


def get_builtin_subagent_definition(subagent_id: str) -> Optional[Dict[str, Any]]:
    """Get the original definition for a built-in subagent."""
    for s in BUILTIN_SUBAGENTS:
        if s["id"] == subagent_id:
            return s.copy()
    return None



