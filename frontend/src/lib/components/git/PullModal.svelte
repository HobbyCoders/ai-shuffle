<script lang="ts">
	/**
	 * PullModal - Pull latest changes from remote
	 *
	 * Shows behind count, strategy selection (merge/rebase),
	 * auto-stash toggle for uncommitted changes, and diverged state info.
	 * When diverged, offers force push as a secondary option for rebase recovery.
	 */
	import {
		ArrowDownToLine, ArrowUpFromLine, GitBranch, GitMerge, X, AlertTriangle, Package
	} from 'lucide-svelte';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';

	type PullStrategy = 'merge' | 'rebase';

	interface Props {
		open: boolean;
		branchName: string;
		commitsBehind: number;
		commitsAhead: number;
		hasChanges: boolean;
		isDiverged: boolean;
		onClose: () => void;
		onPull: (strategy: PullStrategy, autoStash: boolean) => void;
		onForcePush: () => void;
	}

	let { open, branchName, commitsBehind, commitsAhead, hasChanges, isDiverged, onClose, onPull, onForcePush }: Props = $props();

	let strategy = $state<PullStrategy>('rebase');
	let autoStash = $state(false);

	const branchDisplay = $derived(
		branchName.length > 30 ? branchName.slice(0, 27) + '...' : branchName
	);

	const commitWord = $derived(commitsBehind === 1 ? 'commit' : 'commits');
	const canPull = $derived(!hasChanges || autoStash);

	// Load saved preferences when modal opens
	$effect(() => {
		if (open) {
			const prefs = gitActionPreferences.current;
			strategy = prefs.pullStrategy;
			autoStash = prefs.autoStash;
		}
	});

	function handlePull() {
		// Save user's selections for next time
		gitActionPreferences.setPullStrategy(strategy);
		gitActionPreferences.setAutoStash(autoStash);
		onClose();
		onPull(strategy, autoStash && hasChanges);
	}

	function handleForcePush() {
		onClose();
		onForcePush();
	}

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div class="modal-backdrop" role="dialog" aria-modal="true">
		<button class="modal-backdrop-close" onclick={onClose} tabindex="-1" aria-label="Close dialog"></button>
		<div class="modal">
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon" class:diverged={isDiverged}>
				<ArrowDownToLine size={20} strokeWidth={1.5} />
			</div>

			<!-- Title -->
			<h2 class="modal-title">{isDiverged ? 'Sync with upstream' : 'Pull latest changes'}</h2>

			<!-- Info rows -->
			<div class="info-rows">
				<div class="info-row">
					<span class="info-label">Branch</span>
					<span class="info-value">
						<GitBranch size={13} strokeWidth={1.5} />
						{branchDisplay}
					</span>
				</div>
				<div class="info-row">
					<span class="info-label">Behind</span>
					<span class="info-value">
						{commitsBehind} {commitWord} behind upstream
					</span>
				</div>
				{#if isDiverged}
					<div class="info-row">
						<span class="info-label">Ahead</span>
						<span class="info-value">
							{commitsAhead} {commitsAhead === 1 ? 'commit' : 'commits'} ahead of upstream
						</span>
					</div>
				{/if}
			</div>

			<!-- Diverged info box -->
			{#if isDiverged}
				<div class="info-box">
					<GitBranch size={15} strokeWidth={1.5} />
					<span>Your branch has diverged from upstream. Pull will integrate remote changes with your local commits.</span>
				</div>
			{/if}

			<!-- Warning for uncommitted changes with auto-stash option -->
			{#if hasChanges}
				<div class="warning-box">
					<AlertTriangle size={15} strokeWidth={1.5} />
					<span>You have uncommitted changes.</span>
				</div>
				<!-- Auto-stash toggle -->
				<label class="auto-stash-toggle" class:active={autoStash}>
					<input type="checkbox" bind:checked={autoStash} />
					<span class="toggle-track">
						<span class="toggle-thumb"></span>
					</span>
					<Package size={14} strokeWidth={1.5} />
					<span class="toggle-label">Auto-stash changes before pull</span>
				</label>
			{/if}

			<!-- Strategy selection -->
			<div class="field">
				<span class="field-label">Strategy</span>
				<div class="radio-group">
					<label class="radio-option" class:selected={strategy === 'rebase'}>
						<input type="radio" name="pullStrategy" value="rebase" bind:group={strategy} />
						<span class="radio-dot"></span>
						<GitBranch size={16} strokeWidth={1.5} />
						<span class="radio-text">Rebase</span>
						{#if strategy === 'rebase'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>

					<label class="radio-option" class:selected={strategy === 'merge'}>
						<input type="radio" name="pullStrategy" value="merge" bind:group={strategy} />
						<span class="radio-dot"></span>
						<GitMerge size={16} strokeWidth={1.5} />
						<span class="radio-text">Merge</span>
						{#if strategy === 'merge'}
							<span class="check-mark">&#10003;</span>
						{/if}
					</label>
				</div>
			</div>

			<!-- Primary action: Pull -->
			<button class="continue-btn" onclick={handlePull} disabled={!canPull}>
				{#if hasChanges && autoStash}
					Stash & Pull {commitsBehind} {commitWord}
				{:else}
					Pull {commitsBehind} {commitWord}
				{/if}
			</button>

			<!-- Secondary action: Force push (only for diverged state, for rebase recovery) -->
			{#if isDiverged && !hasChanges}
				<div class="divider-text">
					<span>or</span>
				</div>
				<button class="force-push-btn" onclick={handleForcePush}>
					<ArrowUpFromLine size={14} strokeWidth={1.5} />
					Force Push (rebase recovery)
				</button>
				<p class="force-push-hint">Only use this if you intentionally rebased and need to overwrite remote.</p>
			{/if}
		</div>
	</div>
{/if}

<style>
	.modal-backdrop {
		position: fixed;
		inset: 0;
		z-index: 100;
		display: flex;
		align-items: center;
		justify-content: center;
		animation: fade-in 150ms ease;
	}

	.modal-backdrop-close {
		position: absolute;
		inset: 0;
		width: 100%;
		height: 100%;
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(4px);
		border: none;
		cursor: default;
		padding: 0;
	}

	@keyframes fade-in {
		from { opacity: 0; }
		to { opacity: 1; }
	}

	.modal {
		position: relative;
		width: 90%;
		max-width: 440px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 16px;
		padding: 28px;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 14px;
		animation: modal-in 200ms ease;
	}

	@keyframes modal-in {
		from { opacity: 0; transform: scale(0.97) translateY(4px); }
		to { opacity: 1; transform: scale(1) translateY(0); }
	}

	.close-btn {
		position: absolute;
		top: 16px;
		right: 16px;
		width: 28px;
		height: 28px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.close-btn:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.modal-icon {
		width: 44px;
		height: 44px;
		border-radius: 12px;
		background: color-mix(in oklch, var(--primary) 12%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--primary);
	}

	.modal-icon.diverged {
		background: color-mix(in oklch, var(--warning, #e8a735) 12%, transparent);
		color: var(--warning, #e8a735);
	}

	.modal-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		line-height: 1.2;
	}

	/* Info rows */
	.info-rows {
		display: flex;
		flex-direction: column;
	}

	.info-row {
		display: flex;
		justify-content: space-between;
		align-items: center;
		padding: 8px 0;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
	}

	.info-label {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.info-value {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		color: var(--foreground);
		font-weight: 500;
	}

	.info-value :global(svg) {
		flex-shrink: 0;
		opacity: 0.6;
	}

	/* Info box (diverged state) */
	.info-box {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.info-box :global(svg) {
		flex-shrink: 0;
		color: var(--primary);
	}

	/* Warning box */
	.warning-box {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		border-radius: 8px;
		background: color-mix(in oklch, var(--warning, #f59e0b) 10%, transparent);
		color: var(--warning, #f59e0b);
		font-size: 0.8125rem;
		line-height: 1.4;
	}

	.warning-box :global(svg) {
		flex-shrink: 0;
	}

	/* Auto-stash toggle */
	.auto-stash-toggle {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 8px 12px;
		border-radius: 8px;
		cursor: pointer;
		transition: background-color 150ms ease;
	}

	.auto-stash-toggle:hover {
		background: var(--hover-overlay);
	}

	.auto-stash-toggle input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.toggle-track {
		position: relative;
		width: 32px;
		height: 18px;
		border-radius: 9px;
		background: color-mix(in oklch, var(--muted-foreground) 30%, transparent);
		transition: background-color 150ms ease;
		flex-shrink: 0;
	}

	.auto-stash-toggle.active .toggle-track {
		background: var(--primary);
	}

	.toggle-thumb {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 14px;
		height: 14px;
		border-radius: 50%;
		background: var(--card);
		transition: transform 150ms ease;
	}

	.auto-stash-toggle.active .toggle-thumb {
		transform: translateX(14px);
	}

	.auto-stash-toggle :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.auto-stash-toggle.active :global(svg) {
		color: var(--primary);
	}

	.toggle-label {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
	}

	.auto-stash-toggle.active .toggle-label {
		color: var(--foreground);
		font-weight: 500;
	}

	/* Field */
	.field {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.field-label {
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	/* Radio group */
	.radio-group {
		display: flex;
		flex-direction: column;
		gap: 0;
	}

	.radio-option {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 14px;
		cursor: pointer;
		transition: all 150ms ease;
		border-radius: 8px;
		position: relative;
	}

	.radio-option:hover {
		background: var(--hover-overlay);
	}

	.radio-option input {
		position: absolute;
		opacity: 0;
		pointer-events: none;
	}

	.radio-dot {
		display: none;
	}

	.radio-option :global(svg) {
		flex-shrink: 0;
		color: var(--muted-foreground);
	}

	.radio-option.selected :global(svg) {
		color: var(--foreground);
	}

	.radio-text {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		flex: 1;
	}

	.radio-option.selected .radio-text {
		color: var(--foreground);
		font-weight: 500;
	}

	.check-mark {
		font-size: 0.875rem;
		color: var(--foreground);
		margin-left: auto;
	}

	/* Continue button */
	.continue-btn {
		width: 100%;
		padding: 10px 20px;
		margin-top: 4px;
		font-size: 0.875rem;
		font-weight: 500;
		border-radius: 10px;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
		background: var(--foreground);
		color: var(--background);
	}

	.continue-btn:hover:not(:disabled) {
		opacity: 0.9;
	}

	.continue-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Divider text */
	.divider-text {
		display: flex;
		align-items: center;
		gap: 12px;
		margin: 2px 0;
	}

	.divider-text::before,
	.divider-text::after {
		content: '';
		flex: 1;
		height: 1px;
		background: var(--border);
	}

	.divider-text span {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}

	/* Force push secondary button */
	.force-push-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		width: 100%;
		padding: 9px 20px;
		font-size: 0.8125rem;
		font-weight: 500;
		border-radius: 10px;
		border: 1px solid var(--warning, #e8a735);
		background: transparent;
		color: var(--warning, #e8a735);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.force-push-btn:hover {
		background: color-mix(in oklch, var(--warning, #e8a735) 10%, transparent);
	}

	.force-push-hint {
		margin: 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-align: center;
		line-height: 1.3;
	}
</style>
