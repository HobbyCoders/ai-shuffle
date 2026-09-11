"""Monitor builtin: fluid owns the shape, the host injects the spawner."""

import asyncio

import pytest

from fluid.tools.builtins import register_builtins
from fluid.tools.builtins.monitor import (
    BUDGET_EXTRA_KEY,
    SPEC,
    describe,
    handler,
    spawn_warning,
    unbounded_reason,
)
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
)


def test_monitor_registered_bare_named():
    registry = ToolRegistry()
    register_builtins(registry)
    names = {spec.name for spec in registry.list_tools(loaded_only=False)}
    assert "Monitor" in names


def test_spec_shape():
    assert SPEC.name == "Monitor"
    assert SPEC.risk == "write"
    props = SPEC.input_schema["properties"]
    assert set(SPEC.input_schema["required"]) == {"command"}
    assert "cwd" in props
    assert props["serves"]["type"] == "boolean"
    assert "stay up" in props["serves"]["description"]


@pytest.mark.asyncio
async def test_no_spawner_reports_unavailable():
    out = await handler({"command": "make build"}, ToolCtx())
    assert out.is_error
    assert "not available" in out.text


@pytest.mark.asyncio
async def test_receipt_result_notifies_session_lifecycle():
    seen = []

    async def spawn(input, ctx):
        return BackgroundTaskReceipt(
            task_id="mon-1", worker_session_id=None, description="ci check"
        )

    async def started(receipt):
        seen.append(receipt)

    ctx = ToolCtx(extra={"monitor_spawn": spawn, "background_task_started": started})
    out = await handler({"command": "npm run build"}, ctx)
    assert not out.is_error
    assert out.background_task == seen[0]
    assert out.background_task.task_id == "mon-1"


@pytest.mark.asyncio
async def test_receipt_commands_the_park():
    # Sessions read a neutral "started" acknowledgement as permission to keep
    # working the turn, then sleep-poll the task they just handed off. The
    # receipt tells them to stop instead.
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="mon-park")

    out = await handler({"command": "make ci"}, ToolCtx(extra={"monitor_spawn": spawn}))
    assert "mon-park" in out.text
    assert "Then end the turn" in out.text
    assert "Do not sleep or poll waiting for it" in out.text
    # Round 7: aligned with Task's receipt, which conditions the park, and the
    # completion notification is explicitly not something to re-read as polling.
    assert "unless you have genuinely disjoint work" in out.text
    assert "one park can cover several" in out.text


@pytest.mark.asyncio
async def test_mapping_without_text_falls_back_to_the_park_receipt():
    async def spawn(input, ctx):
        return {"task_id": "mon-park-2"}

    out = await handler({"command": "make ci"}, ToolCtx(extra={"monitor_spawn": spawn}))
    assert "mon-park-2" in out.text
    assert "Then end the turn" in out.text


def test_description_carries_no_copyable_poll_loop():
    # Every shell poll recipe here was a recipe a session copied into a
    # FOREGROUND Bash call instead of running it under Monitor. A named
    # BLOCKING wait subcommand is the opposite of that — it is what replaces
    # the loop — so `gh pr checks` is deliberately NOT on this list.
    text = describe(frozenset({"Bash", "Monitor"}))
    for snippet in ("sleep 0.5", "sleep 30", "until grep", "; done"):
        assert snippet not in text, f"description still shows {snippet!r}"
    assert "never in Bash" in text
    assert "end the turn" in text.lower()


def test_description_names_the_canonical_ci_wait_and_service_mode():
    text = describe(frozenset({"Bash", "Monitor"}))
    # The audited CI park was a hand-written watch loop the model wrote
    # because the description only alluded to "the tool's own blocking wait".
    assert "gh pr checks <PR> --watch --fail-fast" in text
    # Concrete cases for "prefer Monitor over blocking Bash".
    for case in ("type-check", "build", "test suite"):
        assert case in text
    assert "serves: true" in text


@pytest.mark.parametrize("command", [
    "while true; do sleep 30; done",
    "while :; do gh pr checks 12; sleep 30; done",
    "until gh pr checks 12; do sleep 30; done",
    "for((;;)); do echo x; done",
    "tail -f server.log",
    "inotifywait -m ./src",
    "watch -n 5 kubectl get pods",
    "docker compose logs -f web",
])
def test_lint_flags_unbounded_commands(command):
    assert unbounded_reason(command) is not None
    warning = spawn_warning(command)
    assert warning is not None and "no time budget" in warning


@pytest.mark.parametrize("command", [
    # Its own blocking wait — the recommended shape.
    "gh pr checks 12 --watch --fail-fast",
    "npm run build",
    "vite build",
    "npx vite build --mode production",
    "pnpm exec vite optimize",
    "vite --help",
    "vite --version",
    "pytest -q",
    # Loop-shaped but bounded, each by a different kind of budget.
    "timeout 900 bash -c 'while true; do sleep 30; done'",
    "deadline=$(($(date +%s)+600)); while true; do sleep 5; done",
    "for i in $(seq 1 60); do sleep 10; done",
    "for ((i=0; i<60; i++)); do sleep 10; done",
    "i=0; while [ $i -lt 60 ]; do i=$((i+1)); sleep 10; done",
    "curl --max-time 30 http://localhost:8010/",
])
def test_lint_leaves_bounded_commands_alone(command):
    assert unbounded_reason(command) is None
    assert spawn_warning(command) is None


def test_lint_flags_the_match_all_glob():
    # The audited CI watcher: `*""*` survives quote removal as `**`, so the
    # "still pending" arm matched every status and the break arm was dead.
    command = (
        'while true; do s=$(gh pr view 12 --json state -q .state); '
        'case $s in *pending*|*""*) sleep 30;; *) break;; esac; done'
    )
    warning = spawn_warning(command)
    assert warning is not None
    assert "matches EVERY string" in warning
    assert "no time budget" in warning


@pytest.mark.parametrize("command", [
    "npm run dev",
    "vite",
    "npx vite --host 0.0.0.0",
    "vite preview",
    "pnpm dev -- --port 8010",
    "uvicorn app.main:app --port 8010",
    "python manage.py runserver 0.0.0.0:8000",
    "cloudflared tunnel run demo",
])
def test_lint_points_a_server_at_serves(command):
    warning = spawn_warning(command)
    assert warning is not None and "serves: true" in warning
    # …and says nothing once the model has actually declared it.
    assert spawn_warning(command, serves=True) is None


def test_lint_is_silent_for_a_declared_service():
    assert spawn_warning("tail -f server.log", serves=True) is None


@pytest.mark.asyncio
async def test_warning_is_stapled_to_the_receipt():
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="mon-unbounded")

    out = await handler(
        {"command": "while true; do sleep 30; done"},
        ToolCtx(extra={"monitor_spawn": spawn}),
    )
    # Warn, never refuse: the spawn still happened and the receipt still has
    # its task id — the warning rides along with it.
    assert not out.is_error
    assert out.background_task.task_id == "mon-unbounded"
    assert "mon-unbounded" in out.text
    assert "no time budget" in out.text


@pytest.mark.asyncio
async def test_serves_marks_the_receipt_and_changes_the_park_instruction():
    seen = []

    async def spawn(input, ctx):
        # A host that knows nothing about `serves` — fluid owns the shape, so
        # the flag must land on the receipt anyway.
        return BackgroundTaskReceipt(task_id="mon-serve")

    async def started(receipt):
        seen.append(receipt)

    out = await handler(
        {"command": "npm run dev", "serves": True},
        ToolCtx(extra={"monitor_spawn": spawn, "background_task_started": started}),
    )
    assert out.background_task.serves is True
    assert seen[0].serves is True
    assert "NOT parked on it" in out.text
    assert "Do not sleep or poll waiting on it" in out.text


@pytest.mark.asyncio
async def test_serves_from_a_mapping_result():
    async def spawn(input, ctx):
        return {"task_id": "mon-serve-2"}

    out = await handler(
        {"command": "uvicorn app:app", "serves": True},
        ToolCtx(extra={"monitor_spawn": spawn}),
    )
    assert out.background_task.serves is True
    assert "mon-serve-2" in out.text


@pytest.mark.asyncio
async def test_mapping_and_outcome_results_normalized():
    async def spawn_mapping(input, ctx):
        return {"task_id": "mon-2", "text": "watching logs"}

    out = await handler(
        {"command": "tail -f app.log"},
        ToolCtx(extra={"monitor_spawn": spawn_mapping}),
    )
    assert not out.is_error
    assert out.background_task is not None
    assert out.background_task.task_id == "mon-2"
    # The host's own receipt text is preserved verbatim as the head; the
    # spawn-time lint (this command is a `tail -f`) only appends to it.
    assert out.text.startswith("watching logs")

    receipt = BackgroundTaskReceipt(task_id="mon-3")

    async def spawn_receipt(input, ctx):
        return ToolOutcome(text=f"ok {receipt.task_id}", background_task=receipt)

    out2 = await handler(
        {"command": "cargo build"}, ToolCtx(extra={"monitor_spawn": spawn_receipt})
    )
    assert out2.text == "ok mon-3" and out2.background_task is receipt


@pytest.mark.asyncio
async def test_spawn_error_is_surfaced_without_receipt():
    async def spawn(input, ctx):
        raise RuntimeError("spawn failed: no such file")

    # Registry.dispatch converts handler exceptions into error outcomes, so
    # exercise the same path the engine uses.
    registry = ToolRegistry()
    register_builtins(registry)
    out = await registry.dispatch("Monitor", {"command": "x"}, ToolCtx(extra={"monitor_spawn": spawn}))
    assert out.is_error
    assert out.background_task is None
    assert "spawn failed" in out.text


@pytest.mark.asyncio
async def test_spawn_crosses_parent_cancellation_boundary():
    spawned = asyncio.Event()
    release = asyncio.Event()
    registered = asyncio.Event()
    cancelled = asyncio.Event()

    async def spawn(input, ctx):
        spawned.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return BackgroundTaskReceipt(task_id="mon-detached")

    async def started(receipt):
        registered.set()

    tracked = set()
    call = asyncio.create_task(handler(
        {"command": "long-ci-run"},
        ToolCtx(extra={
            "monitor_spawn": spawn,
            "background_task_started": started,
            "background_spawn_tasks": tracked,
        }),
    ))
    await spawned.wait()
    call.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.wait_for(registered.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not cancelled.is_set()
    assert not tracked


# ─── the host's budget: one number, three places it has to agree ─────────────
#
# The host appends "It carries a 1200s time budget: if it is still running
# then, it is stopped and you are woken with a timeout" to the receipt. Fluid's
# warning then said the command "will hold the park until the session ends and
# you will never be woken". Both sentences arrived in one receipt in six
# audited sessions; models believed the alarming half and burned TaskStop +
# relaunch cycles on a task that was working, one of them leaving a ghost task
# behind.


def test_warning_without_a_budget_keeps_the_never_woken_wording():
    warning = spawn_warning("while true; do sleep 30; done")
    assert warning is not None
    assert "no time budget" in warning
    assert "holds the park until the session ends" in warning


def test_warning_with_a_budget_promises_the_wake_the_host_promises():
    warning = spawn_warning("while true; do sleep 30; done", budget_seconds=1200)
    assert warning is not None
    assert "1200s budget" in warning
    assert "woken with a timeout" in warning
    # The half models acted on, gone.
    assert "never be woken" not in warning
    assert "until the session ends" not in warning
    # And it says outright not to do the thing they did.
    assert "Do not stop and relaunch it" in warning


def test_budget_reaches_the_warning_through_the_tool_context():
    async def spawn(input, ctx):
        return BackgroundTaskReceipt(task_id="mon-budget")

    out = asyncio.run(
        handler(
            {"command": "tail -f build.log"},
            ToolCtx(extra={"monitor_spawn": spawn, BUDGET_EXTRA_KEY: 900}),
        )
    )
    assert "900s budget" in out.text
    assert "never be woken" not in out.text


@pytest.mark.parametrize("value", [None, 0, -5, "", "abc", {}])
def test_a_missing_or_junk_budget_falls_back_to_the_budget_less_wording(value):
    warning = spawn_warning("tail -f build.log", budget_seconds=value)
    assert warning is not None and "no time budget" in warning
    assert "is stopped at the host's" not in warning


def test_description_names_the_budget_when_the_host_declares_one():
    # A session that only read "cap the total wait" wrote a 3600s watcher
    # against a 1200s budget and lost the twenty minutes it ran.
    surface = frozenset({"Bash", "Monitor"})
    assert "1200" in describe(surface, budget_seconds=1200)
    assert "1200" not in describe(surface)
    assert "seconds" in describe(surface, budget_seconds=1200)


def test_description_warns_about_the_other_exit_code_destroyers():
    text = describe(frozenset({"Bash", "Monitor"}))
    assert "|| true" in text
    assert "; next-command" in text
    assert "last and unmasked" in text


def test_description_says_to_let_ci_register_before_watching():
    # Both audited `gh pr checks --watch` launches died seconds after a push
    # with "no checks reported", and the instant clean exit read as success.
    text = describe(frozenset({"Bash", "Monitor"}))
    assert "no checks reported" in text
    assert "gh pr checks <PR> --watch --fail-fast" in text
    assert "never as success" in text


# ─── iteration caps the detector used to miss ────────────────────────────────


@pytest.mark.parametrize("command", [
    # Count-and-break, the mirror image of the `-lt` shape already handled.
    'i=0; while true; do [ $i -ge 60 ] && break; i=$((i+1)); sleep 10; done',
    'n=0; while true; do n=$((n+1)); if [ $n -gt 30 ]; then break; fi; sleep 5; done',
    # Arithmetic spelling of the same cap.
    'i=0; while true; do ((i++)); ((i>=60)) && break; sleep 10; done',
    # A bare bail-out inside the body is a bound too.
    'while true; do gh pr checks 12 && break; sleep 20; done',
    'until false; do make -q && exit 0; sleep 15; done',
])
def test_lint_recognises_a_capped_loop(command):
    assert unbounded_reason(command) is None
    assert spawn_warning(command) is None


def test_a_break_does_not_excuse_a_follow():
    # `break` bounds a LOOP body; it says nothing about `tail -f`, which has no
    # body to break out of.
    command = "tail -f build.log; break"
    assert unbounded_reason(command) == "`tail -f`"


def test_a_dead_break_arm_earns_no_credit():
    # The audited park: `*""*` collapses to `**` and matches every status, so
    # the arm holding the break never runs. A break the glob has killed must
    # not read as a bound.
    command = (
        'while true; do s=$(gh pr view 12 --json state -q .state); '
        'case $s in *pending*|*""*) sleep 30;; *) break;; esac; done'
    )
    assert unbounded_reason(command) is not None


def test_refinement_carries_the_budget_into_the_registered_description():
    from fluid.tools.builtins import refine_builtin_descriptions

    registry = ToolRegistry()
    register_builtins(registry)
    refine_builtin_descriptions(registry, monitor_budget_seconds=1200)
    spec = registry.get("Monitor")
    assert spec is not None and "1200 seconds" in spec.description
    # Other builtins are untouched by it.
    assert "1200 seconds" not in registry.get("Bash").description

    plain = ToolRegistry()
    register_builtins(plain)
    refine_builtin_descriptions(plain)
    assert "1200" not in plain.get("Monitor").description
