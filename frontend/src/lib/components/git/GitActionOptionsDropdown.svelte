<script lang="ts">
	/**
	 * GitActionOptionsDropdown — one compact popover shell for every git-action
	 * split button (Commit / Push / Pull / Create PR / Finish / Ship It).
	 *
	 * The chevron on each split button toggles this panel; the main button always
	 * fires the action directly with saved defaults and never opens the panel.
	 * Styled after GitHeaderActions' .branch-dropdown, including the mobile
	 * position:fixed escape hatch so it isn't clipped by the header's horizontal
	 * scroll rail.
	 *
	 * Persisted options (include-unstaged, next-step, pull strategy, auto-stash,
	 * merge method, cleanup) read/write through gitActionPreferences and stick
	 * across invocations. Per-invocation options (commit message override, force
	 * push, PR title/description/base/draft) live only in local state and reset
	 * every time the panel opens (the parent remounts it on open).
	 */
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import {
		computeShipItSteps,
		type GitActionKind,
		type CommitOptions,
		type PushOptions,
		type PullOptions,
		type CreatePROptions,
		type FinishOptions,
		type ShipItOptions
	} from '$lib/stores/derived/git-action-options';

	interface Props {
		action: GitActionKind;
		currentBranch: string | null;
		defaultBranch: string;
		/** Local branch names offered as PR base targets. */
		baseBranches?: string[];
		openPR?: { number: number } | null;
		hasChanges: boolean;
		/** Worktree sessions delete a worktree on cleanup; plain repos delete a branch. */
		isWorktreeMode?: boolean;
		busy?: boolean;
		onClose: () => void;
		onCommit?: (o: CommitOptions) => void;
		onPush?: (o: PushOptions) => void;
		onPull?: (o: PullOptions) => void;
		onCreatePR?: (o: CreatePROptions) => void;
		onFinish?: (o: FinishOptions) => void;
		onShipIt?: (o: ShipItOptions) => void;
	}

	let {
		action,
		currentBranch,
		defaultBranch,
		baseBranches = [],
		openPR = null,
		hasChanges,
		isWorktreeMode = false,
		busy = false,
		onClose,
		onCommit,
		onPush,
		onPull,
		onCreatePR,
		onFinish,
		onShipIt
	}: Props = $props();

	const prefs = $derived($gitActionPreferences);

	// ── Per-invocation local state (reset on every open) ────────────────────────
	let commitMessage = $state('');
	let forcePush = $state(false);
	let prTitle = $state('');
	let prBody = $state('');
	// Empty until the effect seeds it with the repo default branch on open; kept
	// as the user's explicit choice thereafter. Seeding in an effect (rather than
	// the initializer) avoids reactively capturing the prop's initial value.
	let prBase = $state('');
	let prDraft = $state(false);
	$effect(() => {
		if (prBase === '') prBase = defaultBranch;
	});

	let panelRef = $state<HTMLDivElement | null>(null);

	// Register outside-click / Escape closing on the next frame so the click that
	// opened this panel (on the chevron, which sits outside the panel) doesn't
	// immediately close it again. Bubble phase (not capture) means the chevron's
	// own onclick runs first when it's used to close — so a close-then-reopen
	// double toggle can't happen.
	$effect(() => {
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
		if (panelRef && !panelRef.contains(e.target as Node)) onClose();
	}
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
	}

	// ── Submit handlers (build the option bag, fire, close) ─────────────────────
	function submitCommit() {
		onCommit?.({
			message: commitMessage.trim() || undefined,
			includeUnstaged: prefs.includeUnstaged,
			nextStep: prefs.nextStep
		});
		onClose();
	}
	function submitPush() {
		onPush?.({ force: forcePush });
		onClose();
	}
	function submitPull() {
		onPull?.({ strategy: prefs.pullStrategy, autoStash: prefs.autoStash });
		onClose();
	}
	function submitCreatePR() {
		onCreatePR?.({
			title: prTitle.trim() || undefined,
			body: prBody.trim() || undefined,
			base: prBase || undefined,
			draft: prDraft
		});
		onClose();
	}
	function submitFinish() {
		onFinish?.({ mergeMethod: prefs.mergeMethod, cleanup: prefs.cleanupAfterMerge });
		onClose();
	}
	function submitShipIt() {
		onShipIt?.({
			mergeMethod: prefs.mergeMethod,
			cleanup: prefs.cleanupAfterMerge,
			includeUnstaged: prefs.includeUnstaged
		});
		onClose();
	}

	const shipSteps = $derived(
		computeShipItSteps({ hasChanges, hasOpenPR: !!openPR, cleanup: prefs.cleanupAfterMerge })
	);

	// Base-branch options: default branch first, then any other local branches.
	const baseOptions = $derived(
		[defaultBranch, ...baseBranches.filter((b) => b !== defaultBranch)]
	);
</script>

<!-- Shared between the Finish and Ship It branches (identical merge controls). -->
{#snippet mergeMethodFieldset()}
	<fieldset class="opt-radio-group">
		<legend class="opt-label">Merge method</legend>
		{#each [{ v: 'squash', l: 'Squash' }, { v: 'merge', l: 'Merge commit' }, { v: 'rebase', l: 'Rebase' }] as opt (opt.v)}
			<button
				type="button"
				class="opt-radio"
				class:selected={prefs.mergeMethod === opt.v}
				aria-pressed={prefs.mergeMethod === opt.v}
				onclick={() => gitActionPreferences.setMergeMethod(opt.v as 'squash' | 'merge' | 'rebase')}
			>
				<span class="opt-radio-dot"></span>{opt.l}
			</button>
		{/each}
	</fieldset>
{/snippet}

{#snippet cleanupToggle()}
	<button
		type="button"
		class="opt-toggle"
		role="switch"
		aria-checked={prefs.cleanupAfterMerge}
		onclick={() => gitActionPreferences.setCleanupAfterMerge(!prefs.cleanupAfterMerge)}
	>
		<span class="opt-toggle-label">{isWorktreeMode ? 'Delete worktree after merge' : 'Delete branch after merge'}</span>
		<span class="opt-switch" class:on={prefs.cleanupAfterMerge}></span>
	</button>
{/snippet}

<div class="options-dropdown" bind:this={panelRef} role="dialog" aria-label="{action} options">
	{#if action === 'commit'}
		<div class="opt-title">Commit options</div>
		<label class="opt-field">
			<span class="opt-label">Message</span>
			<textarea
				class="opt-textarea"
				rows="2"
				bind:value={commitMessage}
				placeholder="AI-generated if empty"
			></textarea>
		</label>

		<button
			type="button"
			class="opt-toggle"
			role="switch"
			aria-checked={prefs.includeUnstaged}
			onclick={() => gitActionPreferences.setIncludeUnstaged(!prefs.includeUnstaged)}
		>
			<span class="opt-toggle-label">Include unstaged changes</span>
			<span class="opt-switch" class:on={prefs.includeUnstaged}></span>
		</button>

		<fieldset class="opt-radio-group">
			<legend class="opt-label">After commit</legend>
			{#each [{ v: 'commit', l: 'Commit only' }, { v: 'push', l: 'Commit + push' }, { v: 'pr', l: 'Commit + push + PR' }] as opt (opt.v)}
				<button
					type="button"
					class="opt-radio"
					class:selected={prefs.nextStep === opt.v}
					aria-pressed={prefs.nextStep === opt.v}
					onclick={() => gitActionPreferences.setNextStep(opt.v as 'commit' | 'push' | 'pr')}
				>
					<span class="opt-radio-dot"></span>{opt.l}
				</button>
			{/each}
		</fieldset>

		<button type="button" class="opt-submit" disabled={busy} onclick={submitCommit}>Commit</button>

	{:else if action === 'push'}
		<div class="opt-title">Push options</div>
		<p class="opt-readonly">Pushing <strong>{currentBranch ?? 'current branch'}</strong> to remote.</p>

		<button
			type="button"
			class="opt-toggle destructive"
			role="switch"
			aria-checked={forcePush}
			onclick={() => (forcePush = !forcePush)}
		>
			<span class="opt-toggle-label">
				Force push
				<span class="opt-hint">Overwrites remote history</span>
			</span>
			<span class="opt-switch danger" class:on={forcePush}></span>
		</button>

		<button type="button" class="opt-submit" class:danger={forcePush} disabled={busy} onclick={submitPush}>
			{forcePush ? 'Force push' : 'Push'}
		</button>

	{:else if action === 'pull'}
		<div class="opt-title">Pull options</div>
		<fieldset class="opt-radio-group">
			<legend class="opt-label">Strategy</legend>
			{#each [{ v: 'rebase', l: 'Rebase' }, { v: 'merge', l: 'Merge' }] as opt (opt.v)}
				<button
					type="button"
					class="opt-radio"
					class:selected={prefs.pullStrategy === opt.v}
					aria-pressed={prefs.pullStrategy === opt.v}
					onclick={() => gitActionPreferences.setPullStrategy(opt.v as 'merge' | 'rebase')}
				>
					<span class="opt-radio-dot"></span>{opt.l}
				</button>
			{/each}
		</fieldset>

		<button
			type="button"
			class="opt-toggle"
			role="switch"
			aria-checked={prefs.autoStash}
			onclick={() => gitActionPreferences.setAutoStash(!prefs.autoStash)}
		>
			<span class="opt-toggle-label">Auto-stash local changes</span>
			<span class="opt-switch" class:on={prefs.autoStash}></span>
		</button>

		<button type="button" class="opt-submit" disabled={busy} onclick={submitPull}>Pull</button>

	{:else if action === 'pr'}
		<div class="opt-title">Pull request options</div>
		<label class="opt-field">
			<span class="opt-label">Title</span>
			<input class="opt-input" type="text" bind:value={prTitle} placeholder="AI-generated if empty" />
		</label>
		<label class="opt-field">
			<span class="opt-label">Description</span>
			<textarea class="opt-textarea" rows="2" bind:value={prBody} placeholder="AI-generated if empty"></textarea>
		</label>
		<label class="opt-field">
			<span class="opt-label">Base branch</span>
			<select class="opt-input opt-select" bind:value={prBase}>
				{#each baseOptions as b (b)}
					<option value={b}>{b}</option>
				{/each}
			</select>
		</label>

		<button
			type="button"
			class="opt-toggle"
			role="switch"
			aria-checked={prDraft}
			onclick={() => (prDraft = !prDraft)}
		>
			<span class="opt-toggle-label">Open as draft</span>
			<span class="opt-switch" class:on={prDraft}></span>
		</button>

		<button type="button" class="opt-submit" disabled={busy} onclick={submitCreatePR}>Create PR</button>

	{:else if action === 'finish'}
		<div class="opt-title">Merge options</div>
		{@render mergeMethodFieldset()}
		{@render cleanupToggle()}

		<button type="button" class="opt-submit" disabled={busy} onclick={submitFinish}>
			{openPR ? `Merge PR #${openPR.number}` : 'Merge PR'}
		</button>

	{:else if action === 'shipit'}
		<div class="opt-title">Ship It</div>
		<ol class="opt-steps">
			{#each shipSteps as step (step.id)}
				<li class="opt-step">{step.label}</li>
			{/each}
		</ol>

		{@render mergeMethodFieldset()}
		{@render cleanupToggle()}

		<button type="button" class="opt-submit" disabled={busy} onclick={submitShipIt}>Ship It</button>
	{/if}
</div>

<style>
	/* Panel — mirrors GitHeaderActions' .branch-dropdown. */
	.options-dropdown {
		position: absolute;
		top: calc(100% + 4px);
		right: 0;
		min-width: 240px;
		max-width: 300px;
		max-height: 70vh;
		overflow-y: auto;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
		box-shadow: var(--shadow-l);
		padding: 10px;
		z-index: 200;
		display: flex;
		flex-direction: column;
		gap: 8px;
		animation: opt-in 120ms cubic-bezier(0.16, 1, 0.3, 1);
	}

	@keyframes opt-in {
		from { opacity: 0; transform: translateY(-4px); }
		to { opacity: 1; transform: translateY(0); }
	}

	.opt-title {
		font-size: 0.6875rem;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		color: var(--muted-foreground);
	}

	.opt-field {
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.opt-label {
		font-size: 0.6875rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.opt-input,
	.opt-textarea,
	.opt-select {
		width: 100%;
		padding: 6px 8px;
		background: var(--input);
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.75rem;
		font-family: inherit;
		outline: none;
		transition: border-color 150ms ease;
	}

	.opt-textarea {
		resize: vertical;
		min-height: 40px;
		line-height: 1.4;
	}

	.opt-input:focus,
	.opt-textarea:focus,
	.opt-select:focus {
		border-color: var(--primary);
	}

	.opt-input::placeholder,
	.opt-textarea::placeholder {
		color: var(--muted-foreground);
		opacity: 0.7;
	}

	.opt-select {
		cursor: pointer;
		appearance: none;
		background-image: linear-gradient(45deg, transparent 50%, var(--muted-foreground) 50%),
			linear-gradient(135deg, var(--muted-foreground) 50%, transparent 50%);
		background-position: right 10px center, right 6px center;
		background-size: 4px 4px, 4px 4px;
		background-repeat: no-repeat;
		padding-right: 22px;
	}

	.opt-readonly {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}
	.opt-readonly strong {
		color: var(--foreground);
		font-weight: 600;
	}

	/* Toggle rows */
	.opt-toggle {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
		width: 100%;
		padding: 6px 4px;
		background: transparent;
		border: none;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		cursor: pointer;
		text-align: left;
	}
	.opt-toggle:hover {
		background: var(--hover-overlay);
	}

	.opt-toggle-label {
		display: flex;
		flex-direction: column;
		gap: 1px;
		font-size: 0.75rem;
		color: var(--foreground);
	}
	.opt-toggle.destructive .opt-toggle-label {
		color: var(--foreground);
	}

	.opt-hint {
		font-size: 0.625rem;
		color: var(--muted-foreground);
	}

	.opt-switch {
		position: relative;
		flex-shrink: 0;
		width: 30px;
		height: 17px;
		border-radius: 999px;
		background: var(--border);
		transition: background 150ms ease;
	}
	.opt-switch::after {
		content: '';
		position: absolute;
		top: 2px;
		left: 2px;
		width: 13px;
		height: 13px;
		border-radius: 50%;
		background: var(--card);
		box-shadow: 0 1px 2px rgba(0, 0, 0, 0.3);
		transition: transform 150ms ease;
	}
	.opt-switch.on {
		background: var(--primary);
	}
	.opt-switch.on::after {
		transform: translateX(13px);
	}
	.opt-switch.danger.on {
		background: var(--destructive);
	}

	/* Radio pills */
	.opt-radio-group {
		display: flex;
		flex-direction: column;
		gap: 3px;
		margin: 0;
		padding: 0;
		border: none;
	}
	.opt-radio-group legend {
		padding: 0 0 2px;
	}

	.opt-radio {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 6px 8px;
		background: transparent;
		border: 1px solid transparent;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.75rem;
		cursor: pointer;
		text-align: left;
		transition: background 120ms ease, border-color 120ms ease;
	}
	.opt-radio:hover {
		background: var(--hover-overlay);
	}
	.opt-radio.selected {
		border-color: color-mix(in oklch, var(--primary) 45%, transparent);
		background: color-mix(in oklch, var(--primary) 10%, transparent);
	}

	.opt-radio-dot {
		flex-shrink: 0;
		width: 12px;
		height: 12px;
		border-radius: 50%;
		border: 1.5px solid var(--muted-foreground);
		transition: border-color 120ms ease, box-shadow 120ms ease;
	}
	.opt-radio.selected .opt-radio-dot {
		border-color: var(--primary);
		box-shadow: inset 0 0 0 3px var(--primary);
	}

	/* Ship It step preview */
	.opt-steps {
		list-style: none;
		counter-reset: ship-step;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.opt-step {
		counter-increment: ship-step;
		display: flex;
		align-items: center;
		gap: 8px;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}
	.opt-step::before {
		content: counter(ship-step);
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 16px;
		height: 16px;
		flex-shrink: 0;
		border-radius: 50%;
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		font-size: 0.5625rem;
		font-weight: 700;
		font-variant-numeric: tabular-nums;
	}

	/* Primary submit */
	.opt-submit {
		height: 30px;
		margin-top: 2px;
		background: var(--primary);
		border: 1px solid var(--primary);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--primary-foreground);
		font-size: 0.75rem;
		font-weight: 600;
		cursor: pointer;
		transition: filter 150ms ease;
	}
	.opt-submit:hover:not(:disabled) {
		filter: brightness(1.08);
	}
	.opt-submit:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.opt-submit.danger {
		background: var(--destructive);
		border-color: var(--destructive);
		color: #fff;
	}

	.opt-submit:focus-visible,
	.opt-toggle:focus-visible,
	.opt-radio:focus-visible {
		outline: 2px solid var(--primary);
		outline-offset: 1px;
	}

	@media (prefers-reduced-motion: reduce) {
		.options-dropdown { animation: none; }
		.opt-switch, .opt-switch::after, .opt-radio-dot { transition: none; }
	}
</style>
