<script lang="ts">
	/** In-panel Git action editor. One parent owns scrolling and dismissal.
	 * Saved preferences persist; message/force/PR overrides reset on mount.
	 */
	import { ArrowLeft } from 'lucide-svelte';
	import GenerationModelSelect from '$lib/components/settings/GenerationModelSelect.svelte';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import {
		computeShipItSteps,
		type GitActionKind,
		type CommitOptions,
		type PushOptions,
		type PullOptions,
		type CreatePROptions,
		type ShipItOptions
	} from '$lib/stores/derived/git-action-options';

	interface Props {
		action: GitActionKind;
		id?: string;
		currentBranch: string | null;
		defaultBranch: string;
		/** Local branch names offered as PR base targets. */
		baseBranches?: string[];
		openPR?: { number: number } | null;
		hasChanges: boolean;
		unpushed?: number | null;
		/** Worktree sessions delete a worktree on cleanup; plain repos delete a branch. */
		isWorktreeMode?: boolean;
		/** The checkout may move to a fresh branch (not a worktree or branch-bound session). */
		canCommitOnNewBranch?: boolean;
		/** Disables the whole editor (an operation or stream is running). */
		busy?: boolean;
		/** Why submitting is blocked right now (merge conflicts, state still
		 *  loading). Only the submit button disables; options stay editable. */
		submitBlocked?: string | null;
		onClose: () => void;
		onCommit?: (o: CommitOptions) => void;
		onPush?: (o: PushOptions) => void;
		onPull?: (o: PullOptions) => void;
		onCreatePR?: (o: CreatePROptions) => void;
		onShipIt?: (o: ShipItOptions) => void;
	}

	let {
		action,
		id,
		currentBranch,
		defaultBranch,
		baseBranches = [],
		openPR = null,
		hasChanges,
		unpushed = null,
		isWorktreeMode = false,
		canCommitOnNewBranch = true,
		busy = false,
		submitBlocked = null,
		onClose,
		onCommit,
		onPush,
		onPull,
		onCreatePR,
		onShipIt
	}: Props = $props();

	const prefs = $derived($gitActionPreferences);

	// ── Per-invocation local state (reset on every open) ────────────────────────
	let commitMessage = $state('');
	let generationModel = $state('');
	// On the default branch (#1659): commit on a new branch by default, or push
	// to the default branch only as an explicit, labelled choice. The default-
	// branch step is local so it never overwrites the saved after-commit flow.
	const onDefaultBranch = $derived(!!currentBranch && currentBranch === defaultBranch);
	let commitWhere = $state<'new' | 'default'>('new');
	let newBranchName = $state('');
	let defaultStep = $state<'commit' | 'push'>('commit');
	const committingOnNewBranch = $derived(onDefaultBranch && canCommitOnNewBranch && commitWhere === 'new');
	const commitSubmitLabel = $derived(
		!onDefaultBranch ? 'Commit'
			: committingOnNewBranch ? 'Commit on new branch'
			: defaultStep === 'push' ? `Commit + push to ${defaultBranch}` : 'Commit only'
	);
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

	let panelRef = $state<HTMLElement | null>(null);
	const titles = { commit: 'Commit changes', push: 'Push to remote', pull: 'Pull changes', pr: 'Create a pull request', shipit: 'Ship It' };
	const descriptions = {
		commit: 'Review your message and choose what happens after committing.',
		push: 'Send this branch’s commits to its remote.',
		pull: 'Choose how remote changes are brought into this branch.',
		pr: 'Describe the change and choose its target branch.',
		shipit: 'Review the pipeline and its merge and cleanup settings.'
	};
	// Only the parent workspace owns outside-click dismissal. Escape backs out
	// of this editor without closing the Git panel beneath it.
	$effect(() => {
		window.addEventListener('keydown', onKeydown);
		// Editors without a text field focus Back; the chevron that opened them is gone.
		// The optional generation-model picker leads every AI editor, so it never takes initial focus.
		const field = Array.from(panelRef?.querySelectorAll<HTMLElement>('textarea, input, select') ?? []).find((el) => !el.closest('.generation-model'));
		(field ?? panelRef?.querySelector<HTMLElement>('.opt-back'))?.focus({ preventScroll: true });
		return () => window.removeEventListener('keydown', onKeydown);
	});
	function onKeydown(e: KeyboardEvent) {
		if (e.key === 'Escape') { e.stopPropagation(); onClose(); }
	}

	const cannotSubmit = $derived(busy || !!submitBlocked);

	// ── Submit handlers (build the option bag, fire, close) ─────────────────────
	function submitCommit() {
		if (cannotSubmit) return;
		if (onDefaultBranch && !committingOnNewBranch) {
			onCommit?.({
				...(generationModel ? { generationModel } : {}),
				message: commitMessage.trim() || undefined,
				includeUnstaged: prefs.includeUnstaged,
				nextStep: defaultStep,
				pushToDefaultBranch: defaultStep === 'push'
			});
		} else {
			onCommit?.({
				...(generationModel ? { generationModel } : {}),
				message: commitMessage.trim() || undefined,
				includeUnstaged: prefs.includeUnstaged,
				nextStep: prefs.nextStep,
				...(committingOnNewBranch ? { newBranch: newBranchName.trim() } : {})
			});
		}
		onClose();
	}
	function submitPush() {
		if (cannotSubmit) return;
		onPush?.({ force: forcePush });
		onClose();
	}
	function submitPull() {
		if (cannotSubmit) return;
		onPull?.({ strategy: prefs.pullStrategy, autoStash: prefs.autoStash });
		onClose();
	}
	function submitCreatePR() {
		if (cannotSubmit) return;
		onCreatePR?.({
			...(generationModel ? { generationModel } : {}),
			title: prTitle.trim() || undefined,
			body: prBody.trim() || undefined,
			base: prBase || undefined,
			draft: prDraft
		});
		onClose();
	}
	function submitShipIt() {
		if (cannotSubmit) return;
		onShipIt?.({
			...(generationModel ? { generationModel } : {}),
			mergeMethod: prefs.mergeMethod,
			cleanup: prefs.cleanupAfterMerge,
			includeUnstaged: prefs.includeUnstaged
		});
		onClose();
	}

	const shipSteps = $derived(
		computeShipItSteps({ hasChanges, unpushed, hasOpenPR: !!openPR, prNumber: openPR?.number, cleanup: prefs.cleanupAfterMerge })
	);

	// Base-branch options: default branch first, then any other local branches.
	const baseOptions = $derived(
		[defaultBranch, ...baseBranches.filter((b) => b !== defaultBranch)]
	);
</script>

<!-- Ship It merge settings. -->
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
		{#if isWorktreeMode}
			<span class="opt-toggle-label">
				Delete worktree after merge
				<span class="opt-hint">Stops any session still running in it</span>
			</span>
		{:else}
			<span class="opt-toggle-label">Delete branch after merge</span>
		{/if}
		<span class="opt-switch" class:on={prefs.cleanupAfterMerge}></span>
	</button>
{/snippet}

<section class="options-dropdown" {id} bind:this={panelRef} aria-label="{action} options">
	<div class="opt-heading">
		<button type="button" class="opt-back" onclick={onClose} aria-label="Back to Git actions"><ArrowLeft size={16} /> Back</button>
		<div><h3>{titles[action]}</h3><p>{descriptions[action]}</p></div>
	</div>
	{#if busy}<p class="opt-blocked" role="status">This action is currently unavailable. Return to Git to check its status.</p>
	{:else if submitBlocked}<p class="opt-blocked" role="status">{submitBlocked}</p>{/if}
	<fieldset class="opt-fields" disabled={busy}>
	{#if action === 'commit' || action === 'pr' || action === 'shipit'}
		<GenerationModelSelect bind:value={generationModel} disabled={busy} />
	{/if}
	{#if action === 'commit'}
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

		{#if onDefaultBranch}
			<p class="opt-warning" role="note">
				<strong>You’re on {defaultBranch}.</strong>
				{canCommitOnNewBranch
					? 'Commit on a new branch to open a pull request, or push to it directly only if you mean to.'
					: 'This checkout is bound to its branch, so a push from here goes straight to it.'}
			</p>
			{#if canCommitOnNewBranch}
				<fieldset class="opt-radio-group">
					<legend class="opt-label">Where to commit</legend>
					<button type="button" class="opt-radio" class:selected={commitWhere === 'new'} aria-pressed={commitWhere === 'new'} onclick={() => (commitWhere = 'new')}>
						<span class="opt-radio-dot"></span>New branch
					</button>
					<button type="button" class="opt-radio" class:selected={commitWhere === 'default'} aria-pressed={commitWhere === 'default'} onclick={() => (commitWhere = 'default')}>
						<span class="opt-radio-dot"></span>{defaultBranch}
					</button>
				</fieldset>
			{/if}
			{#if committingOnNewBranch}
				<label class="opt-field">
					<span class="opt-label">Branch name</span>
					<input class="opt-input" type="text" bind:value={newBranchName} placeholder="Named from the commit message if empty" spellcheck="false" autocomplete="off" />
				</label>
			{/if}
		{/if}

		{#if onDefaultBranch && !committingOnNewBranch}
			<fieldset class="opt-radio-group">
				<legend class="opt-label">After commit</legend>
				{#each [{ v: 'commit', l: 'Commit only' }, { v: 'push', l: `Commit + push to ${defaultBranch}` }] as opt (opt.v)}
					<button
						type="button"
						class="opt-radio"
						class:selected={defaultStep === opt.v}
						aria-pressed={defaultStep === opt.v}
						onclick={() => (defaultStep = opt.v as 'commit' | 'push')}
					>
						<span class="opt-radio-dot"></span>{opt.l}
					</button>
				{/each}
			</fieldset>
		{:else}
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
		{/if}

		<button type="button" class="opt-submit" class:warn={onDefaultBranch && !committingOnNewBranch && defaultStep === 'push'} disabled={cannotSubmit} onclick={submitCommit}>{commitSubmitLabel}</button>

	{:else if action === 'push'}
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

		<button type="button" class="opt-submit" class:danger={forcePush} disabled={cannotSubmit} onclick={submitPush}>
			{forcePush ? 'Force push' : 'Push'}
		</button>

	{:else if action === 'pull'}
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

		<button type="button" class="opt-submit" disabled={cannotSubmit} onclick={submitPull}>Pull</button>

	{:else if action === 'pr'}
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

		<button type="button" class="opt-submit" disabled={cannotSubmit} onclick={submitCreatePR}>Create PR</button>

	{:else if action === 'shipit'}
		<ol class="opt-steps">
			{#each shipSteps as step (step.id)}
				<li class="opt-step">{step.label}</li>
			{/each}
		</ol>

		{@render mergeMethodFieldset()}
		{@render cleanupToggle()}

		<button type="button" class="opt-submit" disabled={cannotSubmit} onclick={submitShipIt}>Ship It</button>
	{/if}
	</fieldset>
</section>

<style>
	/* A detail view in the parent's flow: one surface and one scroll owner. */
	.options-dropdown { min-width: 0; padding: 16px; }
	.opt-heading { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 18px; }
	.opt-heading > div { min-width: 0; }
	.opt-heading h3 { margin: 0 0 4px; font-size: .875rem; font-weight: 650; color: var(--foreground); }
	.opt-heading p { margin: 0; font-size: .75rem; line-height: 1.5; color: var(--muted-foreground); }
	.opt-back { display: inline-flex; align-items: center; gap: 5px; min-height: 36px; flex: none; padding: 0 9px; border: 1px solid var(--border); border-radius: .5rem; background: transparent; color: var(--foreground); font: inherit; font-size: .75rem; cursor: pointer; }
	.opt-back:hover { background: var(--hover-overlay); }
	.opt-back:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.opt-fields { min-width: 0; margin: 0; padding: 0; border: 0; display: flex; flex-direction: column; gap: 14px; }
	.opt-fields:disabled { opacity: .6; }
	.opt-blocked { margin: 0 0 14px; padding: 10px; border: 1px solid var(--border); border-radius: .5rem; color: var(--muted-foreground); font-size: .75rem; line-height: 1.5; }
	:global(.sheet-layout) .opt-back,
	:global(.sheet-layout) .opt-radio { min-height: 44px; }
	:global(.sheet-layout) .opt-input,
	:global(.sheet-layout) .opt-textarea { font-size: 16px; }

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
		min-height: 40px;
		box-sizing: border-box;
		padding: 10px 12px;
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
		min-height: 88px;
		line-height: 1.5;
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
		min-height: 44px;
		padding: 10px 8px;
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
		min-height: 44px;
		margin-top: 4px;
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
	.opt-warning {
		margin: 0;
		padding: 10px 12px;
		border: 1px solid color-mix(in oklch, var(--warning) 45%, transparent);
		border-radius: .5rem;
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		color: var(--foreground);
		font-size: .75rem;
		line-height: 1.5;
	}
	.opt-warning strong { color: var(--warning); }
	.opt-submit.warn {
		background: var(--warning);
		border-color: var(--warning);
		color: var(--warning-foreground, #000);
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
		.opt-switch, .opt-switch::after, .opt-radio-dot { transition: none; }
	}
</style>
