<script lang="ts">
	/**
	 * RunsOnPicker — the worker word in the new-session sentence ("Runs on
	 * <worker> in …", NewSessionSentence). The trigger is the worker's name as
	 * a plain underlined word (no online dot: presence shows in the menu rows),
	 * and the menu drops down centred under it.
	 *
	 * Where a NEW session runs: this worker (the default) or another approved
	 * worker. It is the same choice and the same rows as the runner chip
	 * (ExecutionModeBar / WorkerMenuList): it sets `deviceIntent`, which the
	 * first send carries as `target_instance_id`. A session without a project
	 * can run on any online worker; with a project, workers that cannot run it
	 * are greyed with the reason (the cluster git-state eligibility map, fail
	 * open).
	 */
	import { tick, untrack } from 'svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { getClusterGitState, type Install } from '$lib/api/installs';
	import { workerInstalls, refreshWorkerPresence, watchWorkerPresence, presenceOf } from '$lib/stores/worker-presence';
	import { peerRunners, checkoutEligibility, isBlocked, runnerTitle } from './execution-mode-eligibility';
	import { workerRowNote } from '$lib/components/chat/runner-badge-menu';
	import WorkerMenuList, { type WorkerMenuRow } from '$lib/components/ui/WorkerMenuList.svelte';
	import { floatingMenu } from '$lib/components/ui/floating-menu';

	const LOCAL_ROW_ID = '__local__';

	let open = $state(false);
	let triggerRef = $state<HTMLButtonElement | null>(null);
	let wrapRef = $state<HTMLDivElement | null>(null);
	let loading = $state(false);
	let failed = $state(false);
	let eligibility = $state<Record<string, boolean> | null>(null);

	const installs = $derived<Install[]>(failed ? [] : ($workerInstalls ?? []));
	const peers = $derived(peerRunners(installs));
	const selfName = $derived(installs.find((i) => i.is_self)?.device_name ?? 'This worker');
	const intent = $derived($activeSession.deviceIntent);
	const worktreeOn = $derived($activeSession.worktreeIntent === 'worktree');
	const hasProject = $derived(!!($activeSession.project || $activeSession.projectV2Id));
	const label = $derived(intent ? intent.name : selfName);
	const online = $derived.by((): boolean | null => (intent ? presenceOf(installs, [intent.id]) : true));

	// Keep presence fresh while the menu is open or a peer is picked.
	const watching = $derived(open || !!intent);
	$effect(() => {
		if (!watching) return;
		return watchWorkerPresence();
	});
	$effect(() => {
		if ($workerInstalls) untrack(() => { failed = false; });
	});
	// The worker list names this worker; load it once so the chip says so.
	$effect(() => {
		if (!$workerInstalls) void refreshWorkerPresence().catch(() => {});
	});

	async function load() {
		loading = true;
		const projectId = $activeSession.project;
		const projectV2Id = $activeSession.projectV2Id;
		const gitState = (projectId || projectV2Id)
			? getClusterGitState(projectV2Id ? null : projectId || null, projectV2Id || null).catch(() => null)
			: Promise.resolve(null);
		try {
			await refreshWorkerPresence();
			failed = false;
		} catch {
			failed = true;
		} finally {
			loading = false;
		}
		eligibility = checkoutEligibility(await gitState);
	}

	const rows = $derived<WorkerMenuRow[]>([
		{ id: LOCAL_ROW_ID, label: selfName, online: true, disabled: false, title: 'Run this session on this worker', checked: !intent },
		...peers.map((install): WorkerMenuRow => ({
			id: install.id,
			label: install.device_name,
			online: install.online,
			disabled: isBlocked(install, worktreeOn, hasProject ? eligibility : null),
			title: runnerTitle(install, worktreeOn, hasProject ? eligibility : null),
			note: workerRowNote(install, hasProject ? eligibility : null),
			checked: intent?.id === install.id
		}))
	]);

	let menuRef = $state<HTMLDivElement | null>(null);

	/** The menu's rows (workers, then Retry), in order. */
	function menuItems(): HTMLButtonElement[] {
		return Array.from(menuRef?.querySelectorAll<HTMLButtonElement>('[role^="menuitem"]:not(:disabled)') ?? []);
	}

	/** Roving tabindex: only the focused row is in the Tab order. */
	function focusItem(target: HTMLButtonElement | undefined) {
		if (!target) return;
		for (const el of menuItems()) el.tabIndex = el === target ? 0 : -1;
		target.focus();
	}

	async function focusOnOpen() {
		await tick();
		const list = menuItems();
		focusItem(list.find((el) => el.getAttribute('aria-checked') === 'true') ?? list[0]);
	}

	function toggle() {
		open = !open;
		if (open) {
			void load();
			void focusOnOpen();
		}
	}

	function closeMenu(focusTrigger: boolean) {
		open = false;
		if (focusTrigger) triggerRef?.focus();
	}

	/** Arrow keys and Home/End move between rows; Tab leaves and closes. */
	function onMenuKeydown(e: KeyboardEvent) {
		const list = menuItems();
		if (!list.length) return;
		const at = list.indexOf(document.activeElement as HTMLButtonElement);
		if (e.key === 'ArrowDown') {
			e.preventDefault();
			focusItem(list[(at + 1) % list.length]);
		} else if (e.key === 'ArrowUp') {
			e.preventDefault();
			focusItem(list[(at - 1 + list.length) % list.length]);
		} else if (e.key === 'Home') {
			e.preventDefault();
			focusItem(list[0]);
		} else if (e.key === 'End') {
			e.preventDefault();
			focusItem(list[list.length - 1]);
		} else if (e.key === 'Tab') {
			closeMenu(false);
		}
	}

	// Rows arrive after the open (the worker list loads): keep exactly one of
	// them in the Tab order once they render.
	$effect(() => {
		void rows;
		if (!open) return;
		void tick().then(() => {
			const list = menuItems();
			// A freshly rendered button starts in the Tab order (tabIndex 0).
			if (list.length && list.filter((el) => el.tabIndex === 0).length !== 1) {
				const pick = list.find((el) => el === document.activeElement) ?? list.find((el) => el.getAttribute('aria-checked') === 'true') ?? list[0];
				for (const el of list) el.tabIndex = el === pick ? 0 : -1;
			}
		});
	});

	function select(row: WorkerMenuRow) {
		if (row.id === LOCAL_ROW_ID) {
			activeSession.setDeviceIntent(null);
		} else {
			const install = peers.find((i) => i.id === row.id);
			if (!install || isBlocked(install, worktreeOn, hasProject ? eligibility : null)) return;
			activeSession.setDeviceIntent({ id: install.id, name: install.device_name });
		}
		open = false;
	}

	function onWindowClick(e: MouseEvent) {
		const target = e.target as Node;
		if (open && wrapRef && !wrapRef.contains(target) && !menuRef?.contains(target)) open = false;
	}
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) { e.preventDefault(); closeMenu(true); }
	}
</script>

<svelte:window onclick={onWindowClick} onkeydown={onKeydown} />

<div class="runs-on-wrap" bind:this={wrapRef}>
	<button
		bind:this={triggerRef}
		type="button"
		class="sentence-tok"
		class:open
		onclick={toggle}
		aria-haspopup="menu"
		aria-expanded={open}
		aria-label="Runs on {label}{online === false ? ' (offline)' : ''}"
		title={intent ? `This session will run on ${intent.name}` : 'This session will run on this worker'}
	>{label}</button>
	{#if open}
		<!-- Floats to <body>: the hero is its own stacking context, so an
		     in-place menu painted under the composer below it. -->
		<div
			class="runs-on-menu"
			role="menu"
			tabindex="-1"
			aria-label="Where this session runs"
			bind:this={menuRef}
			onkeydown={onMenuKeydown}
			use:floatingMenu={{ anchor: triggerRef, align: 'center' }}
		>
			<WorkerMenuList
				items={rows}
				role="menuitemradio"
				loading={loading && peers.length === 0 && !failed}
				error={failed ? 'Could not load workers. Try again.' : null}
				onRetry={load}
				emptyText={!loading && peers.length === 0 ? 'No other workers available.' : null}
				onSelect={select}
			/>
		</div>
	{/if}
</div>

<style>
	/* One inline word in the sentence; the look is `.sentence-tok` (app.css). */
	/* inline-flex: the markup's own whitespace never shows beside the word. */
	.runs-on-wrap { display: inline-flex; }
	.runs-on-menu:focus { outline: none; }
	/* Position, z-index and the viewport cap come from floatingMenu. */
	.runs-on-menu {
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		padding: 6px;
		background: var(--popover);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 10px) * 0.83);
		box-shadow: var(--shadow-l);
		animation: runs-on-in 150ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.runs-on-menu:global([data-placement='top']) { animation-name: runs-on-in-up; }
	@keyframes runs-on-in { from { opacity: 0; transform: translateY(-4px) scale(0.98); } }
	@keyframes runs-on-in-up { from { opacity: 0; transform: translateY(4px) scale(0.98); } }
	@media (prefers-reduced-motion: reduce) {
		.runs-on-menu { animation: none; }
	}
</style>
