<script lang="ts">
	/**
	 * BranchSelector - Branch checkout pill for the context pill bar above the
	 * chat input.
	 *
	 * Shows the project + current branch and opens a dropdown (upward, since the
	 * pill bar sits just above the input near the bottom of the viewport) for
	 * switching branches. In worktree mode the branch is fixed, so the pill is a
	 * read-only display of the worktree's branch.
	 *
	 * Reads project/branch context from the shared git + active-session stores,
	 * so it works wherever it is rendered (no projectId prop needed).
	 */
	import { onDestroy } from 'svelte';
	import { GitBranch, ChevronDown, Check, Plus, Loader2, Lock, FolderClosed } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { git, gitStatus, branches as branchStore, gitMode } from '$lib/stores/git.svelte';
	import { checkoutBranchWithToast } from '$lib/stores/gitCheckoutAction';
	import { openHeaderDropdown, openDropdown, closeDropdown } from '$lib/stores/header-dropdowns';

	interface Props {
		/** Project name shown before the branch (combined pill). */
		projectName?: string | null;
		/** Open the "create branch" modal (owned by the parent pill bar). */
		onClickNewBranch?: () => void;
		/** Use a stacked, full-width row and inline menu inside the mobile sheet. */
		sheetMode?: boolean;
	}

	let { projectName = null, onClickNewBranch, sheetMode = false }: Props = $props();

	// Worktree mode locks the branch to the session's worktree branch.
	const isWorktreeMode = $derived(!!($activeSession.worktreeId || $activeSession.worktreeBranch) || $gitMode === 'worktree');
	const worktreeBranch = $derived(
		$activeSession.selectedWorktreeBranch || $activeSession.worktreeBranch || null
	);
	const isStreaming = $derived($activeSession.isStreaming);

	// The sheet uses a distinct shared id: its CSS-hidden composer counterpart
	// remains mounted on phones, so a plain `branch` id would render two menus.
	// Distinct ids still preserve single-open coordination with the profile row.
	const dropdownId = $derived(sheetMode ? 'sheet-branch' as const : 'branch' as const);
	const showDropdown = $derived($openHeaderDropdown === dropdownId);
	let dropdownRef = $state<HTMLDivElement | null>(null);
	let checkingOut = $state(false);
	let branchSearch = $state('');
	let searchInputRef = $state<HTMLInputElement | null>(null);

	// Current branch: the worktree's branch when locked, otherwise the repo's
	// checked-out branch. In worktree mode fall back to the loaded git branch
	// only for a LIVE worktree (its git state IS the worktree's). For a
	// cleaned-up worktree the git store holds the MAIN repo, so never fall back
	// to it — show nothing rather than main's branch until it's recreated.
	const currentBranch = $derived(
		isWorktreeMode
			? (worktreeBranch ??
					($activeSession.worktreeDeleted ? null : $gitStatus?.current_branch) ??
					null)
			: ($gitStatus?.current_branch ?? null)
	);

	// The branch text shown in the pill.
	const displayBranch = $derived(currentBranch);

	const branchLocked = $derived(isWorktreeMode || isStreaming || checkingOut);

	// Local branches for the dropdown (exclude remotes).
	const localBranches = $derived($branchStore.filter((b) => !b.remote));

	// Remote branches without a local counterpart (display the short name).
	const remoteBranches = $derived.by(() => {
		const localNames = new Set(localBranches.map((b) => b.name));
		return $branchStore
			.filter((b) => b.remote)
			.map((b) => ({ ...b, shortName: b.name.replace(/^[^/]+\//, '') }))
			.filter((b) => !localNames.has(b.shortName));
	});

	const filteredLocalBranches = $derived(
		branchSearch
			? localBranches.filter((b) => b.name.toLowerCase().includes(branchSearch.toLowerCase()))
			: localBranches
	);
	const filteredRemoteBranches = $derived(
		branchSearch
			? remoteBranches.filter(
					(b) =>
						b.shortName.toLowerCase().includes(branchSearch.toLowerCase()) ||
						b.name.toLowerCase().includes(branchSearch.toLowerCase())
				)
			: remoteBranches
	);
	const showSearch = $derived(localBranches.length + remoteBranches.length > 5);

	function closeBranchDropdown() {
		closeDropdown(dropdownId);
	}

	function toggleDropdown(event: MouseEvent) {
		event.stopPropagation();
		if (branchLocked) return;
		if (showDropdown) {
			closeBranchDropdown();
		} else {
			openDropdown(dropdownId);
			branchSearch = '';
			git.loadBranches();
			requestAnimationFrame(() => searchInputRef?.focus());
		}
	}

	async function checkoutBranch(branchName: string) {
		if (branchName === currentBranch) {
			closeBranchDropdown();
			return;
		}
		checkingOut = true;
		closeBranchDropdown();
		try {
			await checkoutBranchWithToast(branchName, () => git.refreshDiffStats());
		} finally {
			checkingOut = false;
		}
	}

	function handleNewBranch() {
		closeBranchDropdown();
		onClickNewBranch?.();
	}

	function handleClickOutside(e: MouseEvent) {
		if (showDropdown && dropdownRef && !dropdownRef.contains(e.target as Node)) {
			closeBranchDropdown();
		}
	}

	function handleKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape' && showDropdown) closeBranchDropdown();
	}

	onDestroy(closeBranchDropdown);
</script>

<svelte:window onclick={handleClickOutside} onkeydown={handleKeydown} />

<div class="branch-selector" class:sheet-mode={sheetMode} bind:this={dropdownRef}>
	<button
		type="button"
		class="pill branch-btn"
		class:locked={branchLocked}
		onclick={toggleDropdown}
		disabled={branchLocked}
		title={isWorktreeMode
			? `Worktree branch: ${currentBranch ?? 'unknown'}`
			: isStreaming
				? 'Branch locked during streaming'
				: currentBranch
					? `Branch: ${currentBranch}`
					: 'Select branch'}
	>
		{#if projectName}
			<FolderClosed size={13} strokeWidth={1.5} />
			<span class="pill-label project-name">{projectName}</span>
			{#if displayBranch}<span class="separator">/</span>{/if}
		{/if}
		<GitBranch size={13} strokeWidth={1.5} />
		{#if displayBranch}
			<span class="pill-label branch-name">{displayBranch}</span>
		{/if}
		{#if checkingOut}
			<Loader2 size={11} strokeWidth={2} class="spin" />
		{:else if isWorktreeMode}
			<Lock size={10} strokeWidth={1.5} />
		{:else if isStreaming}
			<Lock size={10} strokeWidth={1.5} />
		{:else}
			<ChevronDown size={11} strokeWidth={1.5} />
		{/if}
	</button>

	{#if showDropdown}
		<div class="branch-dropdown" role="menu" aria-label="Branch selection">
			{#if showSearch}
				<div class="dropdown-search">
					<input
						bind:this={searchInputRef}
						bind:value={branchSearch}
						type="text"
						class="branch-search-input"
						placeholder="Filter branches..."
						onclick={(e) => e.stopPropagation()}
						onkeydown={(e) => {
							// Prevent Enter from bubbling to the chat form's submit handler
							// (this input is rendered inside the ChatInput <form>).
							if (e.key === 'Enter') {
								e.preventDefault();
								e.stopPropagation();
							} else if (e.key === 'Escape' && branchSearch) {
								branchSearch = '';
								e.stopPropagation();
							}
						}}
					/>
				</div>
			{/if}

			{#if filteredLocalBranches.length > 0}
				<div class="dropdown-section-label">Branches</div>
				{#each filteredLocalBranches as branch (branch.name)}
					<button
						type="button"
						class="dropdown-item"
						class:active={branch.name === currentBranch}
						onclick={() => checkoutBranch(branch.name)}
					>
						<GitBranch size={13} strokeWidth={1.5} />
						<span class="dropdown-item-name">{branch.name}</span>
						{#if branch.name === currentBranch}
							<Check size={14} strokeWidth={2} />
						{/if}
					</button>
				{/each}
			{:else if !branchSearch}
				<p class="dropdown-empty">No local branches</p>
			{/if}

			{#if filteredRemoteBranches.length > 0}
				<div class="dropdown-section-label">Remote</div>
				{#each filteredRemoteBranches as branch (branch.name)}
					<button
						type="button"
						class="dropdown-item remote-item"
						onclick={() => checkoutBranch(branch.shortName)}
						title={branch.name}
					>
						<GitBranch size={13} strokeWidth={1.5} />
						<span class="dropdown-item-name">{branch.shortName}</span>
						<span class="remote-tag">origin</span>
					</button>
				{/each}
			{/if}

			{#if branchSearch && filteredLocalBranches.length === 0 && filteredRemoteBranches.length === 0}
				<p class="dropdown-empty">No matching branches</p>
			{/if}

			<div class="dropdown-divider"></div>
			<button type="button" class="dropdown-item new-branch-item" onclick={handleNewBranch}>
				<Plus size={13} strokeWidth={1.5} />
				<span class="dropdown-item-name">New branch...</span>
			</button>
		</div>
	{/if}
</div>

<style>
	.branch-selector {
		position: relative;
		min-width: 0;
		flex-shrink: 1;
	}

	.branch-selector.sheet-mode {
		position: relative;
		width: 100%;
		flex-shrink: 1;
	}

	.sheet-mode .pill {
		width: 100%;
		height: 52px;
		padding: 0 14px;
		border-color: var(--border-subtle);
		border-radius: 14px;
		background: color-mix(in oklch, var(--muted) 55%, var(--card));
	}

	.sheet-mode .project-name,
	.sheet-mode .branch-name {
		max-width: none;
	}

	.sheet-mode .branch-name {
		flex: 1;
	}

	.sheet-mode .branch-dropdown {
		position: static;
		width: 100%;
		min-width: 0;
		max-width: none;
		max-height: 45dvh;
		margin-top: 6px;
	}

	/* Pill button — the composer island's `.pill` metrics (mockup): 26px tall,
	   fully rounded, `--scrim` fill on a hairline border. */
	.pill {
		display: flex;
		align-items: center;
		gap: 6px;
		height: 26px;
		max-width: 100%;
		min-width: 0;
		padding: 0 9px;
		background: var(--scrim);
		border: 1px solid var(--border);
		border-radius: 999px;
		color: var(--muted-foreground);
		font-size: 12px;
		cursor: pointer;
		transition: border-color 0.13s ease, color 0.13s ease, background-color 0.13s ease;
		white-space: nowrap;
	}

	.pill:hover:not(:disabled) {
		border-color: var(--border-strong);
		color: var(--foreground);
	}

	.pill:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	.pill :global(svg:first-child) {
		color: var(--subtle-foreground);
	}

	.pill:disabled {
		cursor: default;
	}

	.pill.locked {
		opacity: 0.85;
	}

	.pill :global(svg) {
		flex-shrink: 0;
	}

	.pill-label {
		line-height: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		min-width: 0;
	}

	.project-name {
		max-width: 120px;
	}

	/* A git ref reads as code, so it takes the mockup's `.branchtxt` treatment. */
	.branch-name {
		max-width: min(180px, 38vw);
		font-family: var(--font-mono);
		font-size: 11px;
	}

	.separator {
		color: var(--muted-foreground);
		opacity: 0.4;
		flex-shrink: 0;
	}

	/* Dropdown — opens UPWARD (the pill bar is just above the input). */
	.branch-dropdown {
		position: absolute;
		bottom: calc(100% + 6px);
		left: 0;
		min-width: 220px;
		max-width: min(320px, calc(100vw - 24px));
		max-height: 320px;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		padding: 4px;
		z-index: 100;
		animation: dropdown-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes dropdown-in {
		from {
			opacity: 0;
			transform: translateY(4px);
		}
		to {
			opacity: 1;
			transform: translateY(0);
		}
	}

	.dropdown-search {
		padding: 4px;
	}

	.branch-search-input {
		width: 100%;
		height: 28px;
		padding: 0 8px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.75rem;
		outline: none;
		transition: border-color 150ms ease;
	}

	.branch-search-input:focus {
		border-color: var(--primary);
	}

	.branch-search-input::placeholder {
		color: var(--muted-foreground);
	}

	.dropdown-section-label {
		padding: 6px 10px 2px;
		font-size: 0.625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 7px 10px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.58);
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
		text-align: left;
		transition: background-color 120ms ease;
	}

	.dropdown-item:hover {
		background: var(--hover-overlay);
	}

	.dropdown-item.active {
		font-weight: 500;
	}

	.dropdown-item :global(svg) {
		color: var(--muted-foreground);
		flex-shrink: 0;
	}

	.dropdown-item.active :global(svg:last-child) {
		color: var(--success);
	}

	.dropdown-item-name {
		flex: 1;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border);
		margin: 4px 0;
	}

	.dropdown-empty {
		padding: 12px;
		text-align: center;
		color: var(--muted-foreground);
		font-size: 0.75rem;
		margin: 0;
	}

	.new-branch-item {
		color: var(--primary) !important;
	}

	.new-branch-item :global(svg) {
		color: var(--primary) !important;
	}

	.remote-item :global(svg) {
		color: var(--muted-foreground);
		opacity: 0.6;
	}

	.remote-tag {
		font-size: 0.5625rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted-foreground) 12%, transparent);
		padding: 1px 5px;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		flex-shrink: 0;
	}

	:global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from {
			transform: rotate(0deg);
		}
		to {
			transform: rotate(360deg);
		}
	}

	/* Mobile: keep the dropdown within the viewport. */
	@media (max-width: 1080px) {
		.branch-name {
			max-width: min(120px, 30vw);
		}

		.branch-selector {
			position: static;
		}

		.branch-dropdown {
			left: 12px;
			right: 12px;
			min-width: 0;
			max-width: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.branch-dropdown,
		:global(.spin) {
			animation: none;
		}
	}
</style>
