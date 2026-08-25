<script lang="ts">
	/**
	 * SessionTitleMenu — the bound-chat session title in the header breadcrumb
	 * (desktop StudioTopBar) or mobile AppTopBar. The title itself is the trigger;
	 * clicking it opens a compact text-row menu (Rename / Fork / Delete).
	 *
	 * Unbound "New Chat" and non-chat pages pass a null sessionId and render as a
	 * plain non-interactive title with the same visual weight.
	 */
	import { onDestroy } from 'svelte';
	import { ChevronDown } from 'lucide-svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import { forkSession } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { toast } from '$lib/stores/toast';
	import { openHeaderDropdown, toggleDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	interface Props {
		/** Displayed label (session title, or the page name when inert). */
		title: string;
		/** Bound session id. Null/empty → no menu, just the title text. */
		sessionId: string | null;
		/** Desktop breadcrumb vs centered mobile bar trigger chrome. */
		variant?: 'crumb' | 'mobile';
	}

	let { title, sessionId = null, variant = 'crumb' }: Props = $props();

	const bound = $derived(!!sessionId);
	const open = $derived($openHeaderDropdown === 'session-title');

	let rootRef = $state<HTMLDivElement | null>(null);
	let renaming = $state(false);
	let renameValue = $state('');
	let pendingDelete = $state(false);

	function closeMenu() {
		closeDropdown('session-title');
	}

	function toggleMenu() {
		if (!bound || renaming) return;
		toggleDropdown('session-title');
	}

	// Register outside-click / Escape on the next frame so the opening click
	// (on the title, inside this root) cannot immediately close the menu again.
	$effect(() => {
		if (!open) return;
		let raf = requestAnimationFrame(() => {
			window.addEventListener('click', onDocClick);
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
			closeMenu();
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
		closeMenu();
		renaming = true;
		renameValue = storedTitle();
	}

	function cancelRename() {
		renaming = false;
		renameValue = '';
	}

	async function commitRename() {
		if (!renaming || !sessionId) return;
		const next = renameValue.trim();
		const current = storedTitle().trim();
		cancelRename();
		if (!next || next === current) return;
		try {
			await appData.renameSession(sessionId, next);
			activeSession.setTitle(sessionId, next);
		} catch {
			toast.push('Failed to rename session', { tone: 'error' });
		}
	}

	function handleRenameKeydown(event: KeyboardEvent) {
		event.stopPropagation();
		if (event.key === 'Enter') {
			event.preventDefault();
			commitRename();
		} else if (event.key === 'Escape') {
			event.preventDefault();
			cancelRename();
		}
	}

	async function handleFork() {
		if (!sessionId) return;
		closeMenu();
		try {
			const result = await forkSession(sessionId);
			await appData.loadSessions();
			toast.push('Session forked', { tone: 'success' });
			window.dispatchEvent(new CustomEvent('open-session', { detail: { sessionId: result.session_id } }));
		} catch {
			toast.push('Failed to fork session', { tone: 'error' });
		}
	}

	function startDelete() {
		closeMenu();
		pendingDelete = true;
	}

	async function confirmDelete() {
		if (!sessionId) return;
		const id = sessionId;
		pendingDelete = false;
		try {
			await appData.deleteSession(id);
			if (activeSession.current.sessionId === id) activeSession.newChat();
		} catch {
			toast.push('Failed to delete session', { tone: 'error' });
		}
	}

	function cancelDelete() {
		pendingDelete = false;
	}

	onDestroy(() => closeDropdown('session-title'));
</script>

<div
	bind:this={rootRef}
	class="title-menu"
	class:crumb={variant === 'crumb'}
	class:mobile={variant === 'mobile'}
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

<ConfirmDialog
	open={pendingDelete}
	title="Delete session"
	message={`Delete "${title}"?`}
	confirmLabel="Delete"
	danger
	onConfirm={confirmDelete}
	onCancel={cancelDelete}
/>

<style>
	.title-menu {
		min-width: 0;
		max-width: 100%;
	}
	.title-menu.crumb {
		flex: 0 1 auto;
	}
	.title-menu.mobile {
		flex: 1 1 auto;
		display: flex;
		justify-content: center;
		min-width: 0;
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
	.mobile .title-trigger {
		font-size: 14px;
		font-weight: 600;
		letter-spacing: -0.01em;
		text-align: center;
		padding: 0 4px;
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
	.mobile button.title-trigger {
		padding: 3px 8px;
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
	.mobile .rename-input {
		height: 28px;
		font-size: 14px;
		font-weight: 600;
		text-align: center;
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

	@media (prefers-reduced-motion: reduce) {
		button.title-trigger,
		.dropdown-item { transition: none; }
		.menu { animation: none; }
	}
</style>
