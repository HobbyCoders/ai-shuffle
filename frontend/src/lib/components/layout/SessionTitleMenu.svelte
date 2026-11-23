<script lang="ts">
	/**
	 * SessionTitleMenu — the bound chat's actions (Rename / Fork / Export / Delete).
	 *
	 * Export (private backup and sharing copy) shows once the chat has messages.
	 * This menu is its only home, on the desktop bar and in the phone app.
	 *
	 * `crumb` (desktop StudioTopBar): the title itself is the trigger and opens
	 * a compact text-row menu under it. Unbound "New Chat" and non-chat pages
	 * pass a null sessionId and render as a plain non-interactive title with
	 * the same visual weight.
	 *
	 * `sheet` (the phone's one-bar AppTopBar): the bar draws the title as
	 * passive text, and this renders only its own chevron button beside it.
	 * The chevron opens a bottom sheet that shows the whole title (the bar can
	 * only fit one ellipsized line) above the same actions; Rename edits the
	 * title in place inside the sheet. Renders nothing for an unbound chat.
	 */
	import { onDestroy } from 'svelte';
	import { obscuresTranscript } from '$lib/components/chat/transcript-reading-area';
	import { ChevronDown, ChevronLeft, GitFork, Pencil, Trash2, X } from 'lucide-svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { floatingMenu } from '$lib/components/ui/floating-menu';
	import { forkSession, getErrorMessage } from '$lib/api/client';
	import { appData, sessions } from '$lib/stores/app-data.svelte';
	import { workerInstalls, refreshWorkerPresence } from '$lib/stores/worker-presence';
	import { forkTargets, type ForkTarget } from './fork-targets';
	import { activeSession } from '$lib/stores/active-session';
	import ChatExportActions from './ChatExportActions.svelte';
	import { notify } from '$lib/stores/notifications';
	import { openHeaderDropdown, toggleDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	interface Props {
		/** Displayed label (session title, or the page name when inert). */
		title: string;
		/** Bound session id. Null/empty → no menu, just the title text. */
		sessionId: string | null;
		/** Desktop breadcrumb menu, or the phone bar's chevron + bottom sheet. */
		variant?: 'crumb' | 'sheet';
		/** Sheet only: the project named above the full title. */
		projectLabel?: string | null;
	}

	let { title, sessionId = null, variant = 'crumb', projectLabel = null }: Props = $props();

	const bound = $derived(!!sessionId);
	const open = $derived($openHeaderDropdown === 'session-title');
	const sheet = $derived(variant === 'sheet');
	/** Exports need this chat's transcript loaded, with something in it. */
	const canExport = $derived(
		!!sessionId && $activeSession.sessionId === sessionId && ($activeSession.messages?.length ?? 0) > 0
	);

	let rootRef = $state<HTMLDivElement | null>(null);
	let chevronRef = $state<HTMLButtonElement | null>(null);
	let triggerRef = $state<HTMLButtonElement | null>(null);
	let menuRef = $state<HTMLDivElement | null>(null);
	let sheetRef = $state<HTMLElement | null>(null);
	const sheetTitleId = $props.id();
	let renaming = $state(false);
	let renameValue = $state('');
	let pendingDelete = $state(false);
	/** The menu shows the workers to fork to instead of the actions. */
	let pickingWorker = $state(false);

	// The owner decides, as on the runner badge; instance_id only counts on a
	// row with no owner.
	const sessionRow = $derived(($sessions ?? []).find((s) => s.id === sessionId) ?? null);
	const targets = $derived(
		forkTargets(
			$workerInstalls,
			sessionRow?.owner_device_id || sessionRow?.instance_id || null,
			sessionRow?.is_remote ?? false
		)
	);

	// Each opening reloads presence: a worker that came online since the list
	// was loaded must be offered, and nothing else may be polling it.
	$effect(() => {
		if (!open) pickingWorker = false;
		else void refreshWorkerPresence().catch(() => {});
	});

	function closeMenu() {
		closeDropdown('session-title');
	}

	function toggleMenu() {
		if (!bound || (renaming && !sheet)) return;
		toggleDropdown('session-title');
	}

	/** Sheet: every way of closing drops an unsaved rename and hands focus back. */
	function closeSheet() {
		const focusInside = !!sheetRef && sheetRef.contains(document.activeElement);
		cancelRename();
		closeMenu();
		if (focusInside) chevronRef?.focus({ preventScroll: true });
	}

	// The sheet's focus trap moves focus to its first action and keeps Tab
	// inside; this drops a half-typed rename when something else (another
	// header panel) closes it.
	$effect(() => {
		if (sheet && !open && renaming) cancelRename();
	});

	// Whenever the chat is not a known ordinary one (the assistant home, or its
	// identity was lost on an owner change): drop the menu and any half-done action.
	$effect(() => {
		if (bound) return;
		if (open) closeMenu();
		if (renaming) cancelRename();
		if (pendingDelete) pendingDelete = false;
	});

	// Register outside-click / Escape on the next frame so the opening click
	// (on the title, inside this root) cannot immediately close the menu again.
	// The sheet is portalled out of this root and closes from its own scrim.
	$effect(() => {
		if (!open) return;
		let raf = requestAnimationFrame(() => {
			if (!sheet) window.addEventListener('click', onDocClick);
		});
		window.addEventListener('keydown', onKeydown);
		return () => {
			cancelAnimationFrame(raf);
			window.removeEventListener('click', onDocClick);
			window.removeEventListener('keydown', onKeydown);
		};
	});

	// The desktop menu floats at the end of <body>, outside the page's tab
	// order: move focus to its first item on open, and Tabbing off either end
	// closes it and returns to the title, as if the menu sat right after it.
	// Swapping to or from the worker list removes the focused row, so focus
	// moves to the first row of the new list.
	$effect(() => {
		if (sheet || !open || !menuRef) return;
		void pickingWorker;
		const menu = menuRef;
		const items = () => [...menu.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)')];
		items()[0]?.focus({ preventScroll: true });
		const onTab = (e: KeyboardEvent) => {
			if (e.key !== 'Tab') return;
			const list = items();
			const edge = e.shiftKey ? list[0] : list[list.length - 1];
			if (document.activeElement !== edge) return;
			e.preventDefault();
			closeMenu();
			triggerRef?.focus({ preventScroll: true });
		};
		menu.addEventListener('keydown', onTab);
		return () => menu.removeEventListener('keydown', onTab);
	});

	function onDocClick(e: MouseEvent) {
		// The menu floats in <body>, outside this root.
		const target = e.target as Node;
		// A row the click itself removed (Back swaps the worker list out).
		if (!target.isConnected) return;
		if (rootRef && !rootRef.contains(target) && !menuRef?.contains(target)) closeMenu();
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.preventDefault();
			e.stopPropagation();
			if (sheet) {
				closeSheet();
				return;
			}
			closeMenuKeepingFocus();
		}
	}

	/** Desktop: a focused row is about to unmount, so hand focus back to the title. */
	function closeMenuKeepingFocus() {
		const focusInside = !!menuRef && menuRef.contains(document.activeElement);
		closeMenu();
		if (focusInside) triggerRef?.focus({ preventScroll: true });
	}

	function focusSelect(node: HTMLInputElement) {
		node.focus();
		node.select();
	}

	function storedTitle(): string {
		if (sessionId && activeSession.current.sessionId === sessionId) {
			return activeSession.current.title || '';
		}
		return title;
	}

	function startRename() {
		// The sheet keeps itself open and edits the title it shows.
		if (!sheet) closeMenu();
		renaming = true;
		renameValue = storedTitle();
	}

	function cancelRename() {
		renaming = false;
		renameValue = '';
	}

	async function commitRename() {
		if (!renaming || !sessionId || !bound) return;
		const next = renameValue.trim();
		const current = storedTitle().trim();
		cancelRename();
		if (sheet) closeSheet();
		if (!next || next === current) return;
		try {
			await appData.renameSession(sessionId, next);
			activeSession.setTitle(sessionId, next);
		} catch {
			notify('Failed to rename session', { severity: 'error' });
		}
	}

	function handleRenameKeydown(event: KeyboardEvent) {
		event.stopPropagation();
		if (sheet && event.key === 'Escape') {
			// Escape leaves the edit, not the sheet.
			event.preventDefault();
			cancelRename();
			return;
		}
		if (event.key === 'Enter') {
			event.preventDefault();
			commitRename();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelRename();
		}
	}

	async function handleFork(target?: ForkTarget) {
		if (!sessionId || !bound) return;
		if (sheet) closeSheet();
		else closeMenu();
		try {
			// Moving the fork pushes its branch and syncs the other worker: say
			// it started, it can take a while.
			if (target) notify(`Forking to ${target.label}…`, { severity: 'info' });
			const result = target ? await forkSession(sessionId, undefined, target.id) : await forkSession(sessionId);
			await appData.loadSessions();
			if (result.move_error) {
				notify(`Chat forked, but it could not move to ${target?.label ?? 'that worker'}: ${result.move_error}`,
					{ severity: 'warning' });
			} else {
				notify(target ? `Chat forked to ${target.label}` : 'Chat forked', { severity: 'success' });
			}
			window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId: result.session_id } }));
		} catch (e) {
			// The server says why (for a chat another worker runs: that worker
			// is unreachable, or could not fork it), so show that.
			notify(getErrorMessage(e, 'Failed to fork session'), { severity: 'error' });
		}
	}

	function startDelete() {
		if (sheet) closeSheet();
		else closeMenu();
		pendingDelete = true;
	}

	async function confirmDelete() {
		if (!sessionId || !bound) return;
		const id = sessionId;
		pendingDelete = false;
		try {
			await appData.deleteSession(id);
			if (activeSession.current.sessionId === id) activeSession.newChat();
		} catch {
			notify('Failed to delete session', { severity: 'error' });
		}
	}

	function cancelDelete() {
		pendingDelete = false;
	}

	onDestroy(() => closeDropdown('session-title'));
</script>

{#if sheet}
	{#if bound}
		<button
			bind:this={chevronRef}
			type="button"
			class="chev-trigger"
			class:open
			onclick={toggleMenu}
			aria-label="Chat actions"
			aria-haspopup="dialog"
			aria-expanded={open}
		>
			<ChevronDown size={18} strokeWidth={1.8} aria-hidden="true" />
		</button>
		{#if open}
			<div class="stm-portal" use:portal>
				<!-- svelte-ignore a11y_click_events_have_key_events -->
				<!-- svelte-ignore a11y_no_static_element_interactions -->
				<div class="stm-scrim" onclick={closeSheet}></div>
				<div bind:this={sheetRef} use:obscuresTranscript class="stm-sheet" role="dialog" aria-modal="true" aria-labelledby={sheetTitleId} tabindex="-1" data-no-drawer-swipe use:focusTrap={{ initial: '.sheet-row' }}>
					<div class="stm-head">
						<h2 id={sheetTitleId}>Chat actions</h2>
						<button type="button" class="stm-close" aria-label="Close chat actions" onclick={closeSheet}><X size={18} aria-hidden="true" /></button>
					</div>
					{#if projectLabel}<p class="stm-project">{projectLabel}</p>{/if}
					{#if renaming}
						<form class="stm-rename" onsubmit={(e) => { e.preventDefault(); void commitRename(); }}>
							<input
								class="stm-input"
								type="text"
								bind:value={renameValue}
								use:focusSelect
								onkeydown={handleRenameKeydown}
								aria-label="Rename session"
							/>
							<div class="stm-rename-actions">
								<button type="button" class="stm-btn" onclick={cancelRename}>Cancel</button>
								<button type="submit" class="stm-btn primary" disabled={!renameValue.trim()}>Save</button>
							</div>
						</form>
					{:else}
						<p class="stm-title">{title}</p>
					{/if}
					{#if pickingWorker}
						<div class="stm-list" role="group" aria-label="Fork to another worker">
							<button type="button" class="sheet-row" onclick={() => (pickingWorker = false)}><ChevronLeft size={17} aria-hidden="true" />Fork to another worker</button>
							{#each targets as target (target.id)}
								<button type="button" class="sheet-row" disabled={!target.online} onclick={() => handleFork(target)}>
									<span class="worker-dot" class:online={target.online} aria-hidden="true"></span><span class="worker-name">{target.label}</span>{#if !target.online}<span class="worker-note">offline</span>{/if}
								</button>
							{/each}
						</div>
					{:else}
						<div class="stm-list" role="group" aria-label="Session actions">
							<button type="button" class="sheet-row" disabled={renaming} onclick={startRename}><Pencil size={17} aria-hidden="true" />Rename session</button>
							<button type="button" class="sheet-row" disabled={renaming} onclick={() => handleFork()}><GitFork size={17} aria-hidden="true" />Fork session</button>
							{#if targets.length}
								<button type="button" class="sheet-row" disabled={renaming} onclick={() => (pickingWorker = true)}><GitFork size={17} aria-hidden="true" />Fork to another worker…</button>
							{/if}
							{#if canExport && sessionId}
								<ChatExportActions {sessionId} sheet disabled={renaming} onStart={closeSheet} />
							{/if}
							<button type="button" class="sheet-row danger" disabled={renaming} onclick={startDelete}><Trash2 size={17} aria-hidden="true" />Delete</button>
						</div>
					{/if}
				</div>
			</div>
		{/if}
	{/if}
{:else}
<div
	bind:this={rootRef}
	class="title-menu"
	class:crumb={variant === 'crumb'}
>
	<div class="title-anchor">
		{#if renaming && bound}
			<input
				class="rename-input"
				type="text"
				bind:value={renameValue}
				use:focusSelect
				onkeydown={handleRenameKeydown}
				onblur={commitRename}
				aria-label="Rename session"
			/>
		{:else if bound}
			<button
				bind:this={triggerRef}
				type="button"
				class="title-trigger"
				class:open
				onclick={toggleMenu}
				title={title}
				aria-haspopup="menu"
				aria-expanded={open}
				aria-current="page"
			>
				<span class="title-text">{title}</span>
				<ChevronDown class="title-caret" size={13} strokeWidth={2} aria-hidden="true" />
			</button>
		{:else}
			<span class="title-trigger" aria-current="page">{title}</span>
		{/if}

		{#if open && bound && !renaming}
			<!-- One floated menu: picking a worker swaps its rows in place. -->
			<div
				bind:this={menuRef}
				class="menu"
				class:picking={pickingWorker}
				role="menu"
				aria-label={pickingWorker ? 'Fork to another worker' : 'Session actions'}
				use:floatingMenu={{ anchor: triggerRef, align: 'start', gap: 4 }}
			>
				{#if pickingWorker}
					<button type="button" class="dropdown-item menu-back" role="menuitem" onclick={() => (pickingWorker = false)}>
						<ChevronLeft size={16} strokeWidth={1.5} aria-hidden="true" />Fork to another worker
					</button>
					<div class="dropdown-divider" role="separator"></div>
					{#each targets as target (target.id)}
						<button type="button" class="dropdown-item worker-item" role="menuitem" disabled={!target.online} onclick={() => handleFork(target)}>
							<span class="worker-dot" class:online={target.online} aria-hidden="true"></span><span class="worker-name">{target.label}</span>{#if !target.online}<span class="worker-note">offline</span>{/if}
						</button>
					{/each}
				{:else}
					<button type="button" class="dropdown-item" role="menuitem" onclick={startRename}>
						<Pencil size={16} strokeWidth={1.5} aria-hidden="true" />Rename session
					</button>
					<button type="button" class="dropdown-item" role="menuitem" onclick={() => handleFork()}>
						<GitFork size={16} strokeWidth={1.5} aria-hidden="true" />Fork session
					</button>
					{#if targets.length}
						<!-- Keeps the menu open: the click swaps it to the worker list. -->
						<button type="button" class="dropdown-item" role="menuitem" onclick={(e) => { e.stopPropagation(); pickingWorker = true; }}>
							<GitFork size={16} strokeWidth={1.5} aria-hidden="true" />Fork to another worker…
						</button>
					{/if}
					{#if canExport && sessionId}
						<div class="dropdown-divider" role="separator"></div>
						<ChatExportActions {sessionId} menu onStart={closeMenuKeepingFocus} />
					{/if}
					<div class="dropdown-divider" role="separator"></div>
					<button type="button" class="dropdown-item danger" role="menuitem" onclick={startDelete}>
						<Trash2 size={16} strokeWidth={1.5} aria-hidden="true" />Delete
					</button>
				{/if}
			</div>
		{/if}
	</div>
</div>
{/if}

<ConfirmDialog
	open={pendingDelete}
	title="Delete session"
	message={`Delete "${title}"?`}
	confirmLabel="Delete"
	danger
	onConfirm={confirmDelete}
	onCancel={cancelDelete}
/>

<script module lang="ts">
	/** The sheet leaves the bar's stacking context so it covers the whole page. */
	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy() { node.remove(); } };
	}
</script>

<style>
	.title-menu {
		min-width: 0;
		max-width: 100%;
	}
	.title-menu.crumb {
		flex: 0 1 auto;
	}
	.title-anchor {
		position: relative;
		min-width: 0;
		max-width: 100%;
	}

	.title-trigger {
		display: block;
		max-width: 100%;
		min-width: 0;
		border: none;
		background: transparent;
		padding: 0;
		margin: 0;
		font-family: inherit;
		color: var(--foreground);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
		text-align: inherit;
	}
	.crumb .title-trigger {
		font-size: 13.5px;
		font-weight: 500;
		letter-spacing: -0.008em;
	}

	button.title-trigger {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		cursor: pointer;
		border-radius: 8px;
		color: var(--muted-foreground);
		transition: color 0.13s ease;
	}
	button.title-trigger .title-text {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	button.title-trigger :global(.title-caret) {
		flex: none;
		opacity: 0.85;
	}
	.crumb button.title-trigger {
		/* The negative inline margins preserve breadcrumb spacing, so include
		   their 12px overhang in the button width instead of clipping every title. */
		max-width: calc(100% + 12px);
		padding: 3px 6px;
		margin: -3px -6px;
	}
	button.title-trigger:hover,
	button.title-trigger.open {
		color: var(--foreground);
	}
	button.title-trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.rename-input {
		display: block;
		width: 100%;
		min-width: 8ch;
		height: 26px;
		padding: 2px 6px;
		border: 1px solid var(--ring);
		border-radius: 6px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		outline: none;
	}

	/* Position, z-index and the viewport clamp come from floatingMenu
	   (it lifts the menu into <body> so the main panel cannot clip it). */
	.menu {
		/* Room for the export rows' notes to wrap once at most. */
		width: 280px;
		max-width: calc(100vw - 16px);
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--menu-border);
		border-radius: var(--menu-radius);
		box-shadow: var(--menu-shadow);
		padding: 4px;
		z-index: 100;
		animation: dropdown-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes dropdown-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 9px;
		width: 100%;
		min-height: var(--menu-row);
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: var(--menu-row-radius);
		color: var(--foreground);
		font-family: inherit;
		font-size: 13px;
		/* Keep a row on one line: the menu sizes to its widest row, not to a
		   short chat title it hangs from. */
		white-space: nowrap;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease, color 120ms ease;
	}
	.dropdown-item:hover {
		background: var(--hover-overlay);
	}
	.dropdown-item:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.dropdown-item :global(svg) {
		flex: none;
		color: var(--muted-foreground);
	}
	.dropdown-item.danger,
	.dropdown-item.danger :global(svg) {
		color: var(--destructive);
	}
	.dropdown-item.danger:hover {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}
	.dropdown-item:disabled,
	.sheet-row:disabled {
		color: var(--muted-foreground);
		cursor: default;
	}
	.dropdown-item:disabled:hover { background: transparent; }
	.menu-back {
		gap: 4px;
		color: var(--muted-foreground);
	}

	/* "Fork to another worker" rows: a presence dot, and why a row is greyed. */
	.worker-dot {
		flex: none;
		width: 7px;
		height: 7px;
		margin-right: 8px;
		border-radius: 50%;
		background: var(--muted-foreground);
		opacity: 0.5;
	}
	.worker-dot.online {
		background: var(--success, oklch(0.7 0.15 150));
		opacity: 1;
	}
	/* The worker list sizes to its names (two long ones may differ only at the
	   end); past the cap a name wraps and the offline note stays in view. */
	.menu.picking {
		width: max-content;
		min-width: 280px;
		max-width: min(440px, calc(100vw - 16px));
	}
	.worker-item {
		white-space: normal;
	}
	.worker-name {
		min-width: 0;
		overflow-wrap: anywhere;
	}
	.worker-note {
		flex: none;
		margin-left: auto;
		padding-left: 12px;
		font-size: 12px;
		color: var(--muted-foreground);
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	@media (max-width: 639px) {
		.dropdown-item { min-height: 36px; }
	}

	/* ── Phone one-bar: chevron + bottom sheet ───────────────────────────── */
	.chev-trigger {
		display: grid;
		place-items: center;
		flex: none;
		width: 44px;
		height: 48px;
		padding: 0;
		border: 0;
		border-radius: 8px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 150ms ease, color 150ms ease;
		-webkit-tap-highlight-color: transparent;
	}
	.chev-trigger :global(svg) { transition: transform 200ms cubic-bezier(0.22, 1, 0.36, 1); }
	.chev-trigger:hover { background: var(--hover-overlay); color: var(--foreground); }
	.chev-trigger:active { background: var(--active-overlay, var(--hover-overlay)); }
	.chev-trigger.open { color: var(--foreground); }
	.chev-trigger.open :global(svg) { transform: rotate(180deg); }
	.chev-trigger:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.stm-scrim { position: fixed; inset: 0; z-index: 140; background: var(--modal-overlay, color-mix(in oklch, var(--background) 60%, transparent)); animation: stm-fade 0.2s ease; }
	.stm-sheet:focus { outline: none; }
	.stm-sheet {
		position: fixed;
		/* Full width on a phone; a centred 560px card in a wide DeX window. */
		left: max(8px, calc(50% - 280px));
		right: max(8px, calc(50% - 280px));
		bottom: calc(12px + env(safe-area-inset-bottom, 0px));
		z-index: 141;
		max-height: calc(var(--app-viewport-height, 100dvh) - 96px);
		overflow-y: auto;
		overscroll-behavior: contain;
		padding: 8px 16px 12px;
		border: 1px solid var(--border-strong);
		border-radius: calc(var(--radius) * 2);
		background: var(--popover);
		color: var(--foreground);
		box-shadow: var(--shadow-l);
		animation: stm-up 0.3s cubic-bezier(0.22, 1, 0.36, 1);
	}
	@keyframes stm-fade { from { opacity: 0; } }
	@keyframes stm-up { from { transform: translateY(calc(100% + 40px)); } }
	.stm-head { display: flex; align-items: center; justify-content: space-between; }
	.stm-head h2 { margin: 0; font-size: 16px; font-weight: 600; }
	.stm-close { display: grid; place-items: center; width: 44px; height: 44px; margin-right: -8px; border: 0; border-radius: var(--radius); background: transparent; color: var(--muted-foreground); cursor: pointer; }
	.stm-close:hover { background: var(--hover-overlay); color: var(--foreground); }
	.stm-project { margin: 0; font-size: 12px; color: var(--muted-foreground); }
	/* The whole title the bar had to cut. */
	.stm-title { margin: 4px 0 14px; font-size: 15px; font-weight: 550; line-height: 1.45; letter-spacing: -0.01em; overflow-wrap: anywhere; }
	.stm-rename { margin: 8px 0 14px; }
	.stm-input {
		box-sizing: border-box;
		width: 100%;
		height: 48px;
		padding: 0 12px;
		border: 1px solid var(--ring);
		border-radius: var(--radius);
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 16px;
		outline: none;
	}
	.stm-rename-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
	.stm-btn { min-width: 72px; min-height: 44px; padding: 0 14px; border: 1px solid var(--border-strong); border-radius: var(--radius); background: var(--card); color: var(--foreground); font: inherit; font-size: 13.5px; cursor: pointer; }
	.stm-btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.stm-btn.primary { border-color: transparent; background: var(--primary); color: var(--primary-foreground); font-weight: 600; }
	.stm-btn.primary:hover:not(:disabled) { background: color-mix(in oklab, var(--primary) 88%, var(--foreground)); }
	.stm-btn:disabled { opacity: 0.5; cursor: not-allowed; }
	.stm-list { display: flex; flex-direction: column; overflow: hidden; border: 1px solid var(--border-subtle, var(--border)); border-radius: var(--radius); background: var(--card); }
	.sheet-row {
		display: flex;
		align-items: center;
		gap: 12px;
		min-height: 52px;
		padding: 0 14px;
		border: 0;
		border-top: 1px solid var(--border-subtle, var(--border));
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 14px;
		text-align: left;
		cursor: pointer;
	}
	.sheet-row:first-child { border-top: 0; }
	.sheet-row :global(svg) { color: var(--muted-foreground); flex: none; }
	.sheet-row:hover:not(:disabled) { background: var(--hover-overlay); }
	.sheet-row:disabled { opacity: 0.45; cursor: not-allowed; }
	.sheet-row.danger, .sheet-row.danger :global(svg) { color: var(--destructive); }
	.sheet-row:focus-visible, .stm-close:focus-visible, .stm-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	@media (prefers-reduced-motion: reduce) {
		button.title-trigger,
		.dropdown-item,
		.chev-trigger,
		.chev-trigger :global(svg) { transition: none; }
		.menu, .stm-scrim, .stm-sheet { animation: none; }
	}
</style>
