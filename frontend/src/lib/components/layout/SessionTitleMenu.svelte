<script lang="ts">
	/**
	 * SessionTitleMenu — the bound chat's actions (Rename / Fork / Delete).
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
	 *
	 * The assistant's home chat counts as unbound in both variants: it is built
	 * in, so it has no Rename / Fork / Delete.
	 */
	import { onDestroy } from 'svelte';
	import { ChevronDown, GitFork, Pencil, Trash2, X } from 'lucide-svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { forkSession } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { assistantState, isAssistantHome } from '$lib/stores/assistant';
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

	// The assistant's home chat is built in: it cannot be renamed, forked or
	// deleted, so it takes the unbound path (plain title, no chevron) everywhere.
	const bound = $derived(!!sessionId && !isAssistantHome(sessionId, $assistantState));
	const open = $derived($openHeaderDropdown === 'session-title');
	const sheet = $derived(variant === 'sheet');

	let rootRef = $state<HTMLDivElement | null>(null);
	let chevronRef = $state<HTMLButtonElement | null>(null);
	let sheetRef = $state<HTMLElement | null>(null);
	const sheetTitleId = $props.id();
	let renaming = $state(false);
	let renameValue = $state('');
	let pendingDelete = $state(false);

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

	// The chat can turn out to be the assistant's home after the menu opened
	// (its state loads late): drop the menu and any half-done action.
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

	function onDocClick(e: MouseEvent) {
		if (rootRef && !rootRef.contains(e.target as Node)) closeMenu();
	}

	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && open) {
			e.stopPropagation();
			if (sheet) closeSheet();
			else closeMenu();
		}
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

	async function handleFork() {
		if (!sessionId || !bound) return;
		if (sheet) closeSheet();
		else closeMenu();
		try {
			const result = await forkSession(sessionId);
			await appData.loadSessions();
			notify('Session forked', { severity: 'success' });
			window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId: result.session_id } }));
		} catch {
			notify('Failed to fork session', { severity: 'error' });
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
				<div bind:this={sheetRef} class="stm-sheet" role="dialog" aria-modal="true" aria-labelledby={sheetTitleId} tabindex="-1" data-no-drawer-swipe use:focusTrap={{ initial: '.sheet-row' }}>
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
					<div class="stm-list" role="group" aria-label="Session actions">
						<button type="button" class="sheet-row" disabled={renaming} onclick={startRename}><Pencil size={17} aria-hidden="true" />Rename session</button>
						<button type="button" class="sheet-row" disabled={renaming} onclick={handleFork}><GitFork size={17} aria-hidden="true" />Fork session</button>
						<button type="button" class="sheet-row danger" disabled={renaming} onclick={startDelete}><Trash2 size={17} aria-hidden="true" />Delete</button>
					</div>
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
			<div class="menu" role="menu" aria-label="Session actions">
				<button type="button" class="dropdown-item" role="menuitem" onclick={startRename}>
					Rename session
				</button>
				<button type="button" class="dropdown-item" role="menuitem" onclick={handleFork}>
					Fork session
				</button>
				<div class="dropdown-divider" role="separator"></div>
				<button type="button" class="dropdown-item danger" role="menuitem" onclick={startDelete}>
					Delete
				</button>
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

	.menu {
		position: absolute;
		top: calc(100% + 4px);
		left: 0;
		right: auto;
		min-width: 180px;
		max-width: calc(100vw - 24px);
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
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
		width: 100%;
		min-height: 32px;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
		color: var(--foreground);
		font-family: inherit;
		font-size: 0.8125rem;
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
	.dropdown-item.danger {
		color: var(--destructive);
	}
	.dropdown-item.danger:hover {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
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
	.stm-btn.primary:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, var(--foreground)); }
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
