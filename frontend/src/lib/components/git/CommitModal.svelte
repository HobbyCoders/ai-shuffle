<script lang="ts">
	/**
	 * CommitModal - Dual-mode commit/PR modal
	 *
	 * mode='commit': Shows branch info, change stats, include-unstaged toggle,
	 * commit message textarea, and "Next steps" radio group.
	 *
	 * mode='pr-only': Shows branch info, commits ahead count, PR title input,
	 * description textarea, and "Create pull request" button.
	 *
	 * In both modes, leaving text fields blank triggers AI generation.
	 */
	import {
		GitCommitHorizontal, GitPullRequest, GitBranch, X,
		ToggleLeft, ToggleRight,
		CircleDot, ArrowUp, Github
	} from 'lucide-svelte';
	import { diffStats, gitStatus } from '$lib/stores/git';

	type CommitAction = 'commit' | 'push' | 'pr';
	type ModalMode = 'commit' | 'pr-only';

	interface Props {
		open: boolean;
		projectId: string;
		mode?: ModalMode;
		defaultBranch?: string;
		needsPull?: boolean;
		onClose: () => void;
		onStartCommit: (action: CommitAction, message: string, includeUnstaged: boolean) => void;
		onCreatePR?: (title: string, body: string) => void;
	}

	let { open, projectId, mode = 'commit', defaultBranch = 'main', needsPull = false, onClose, onStartCommit, onCreatePR }: Props = $props();

	let commitMessage = $state('');
	let prTitle = $state('');
	let prBody = $state('');
	let includeUnstaged = $state(true);
	let nextStep = $state<CommitAction>('push');

	const currentBranch = $derived($gitStatus?.current_branch ?? 'unknown');
	const fileCount = $derived($diffStats?.files_changed ?? 0);
	const additions = $derived($diffStats?.additions ?? 0);
	const deletions = $derived($diffStats?.deletions ?? 0);
	const commitsAhead = $derived($gitStatus?.ahead ?? 0);
	const isOnDefaultBranch = $derived(currentBranch === defaultBranch);

	// Truncate branch name for display
	const branchDisplay = $derived(
		currentBranch.length > 30
			? currentBranch.slice(0, 27) + '...'
			: currentBranch
	);

	// Reset state when modal opens
	$effect(() => {
		if (open) {
			commitMessage = '';
			prTitle = '';
			prBody = '';
			includeUnstaged = true;
			nextStep = needsPull ? 'commit' : 'push';
		}
	});

	function handleContinue() {
		if (mode === 'pr-only') {
			onClose();
			onCreatePR?.(prTitle.trim(), prBody.trim());
		} else {
			const message = commitMessage.trim();
			onClose();
			onStartCommit(nextStep, message, includeUnstaged);
		}
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
	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<!-- svelte-ignore a11y_click_events_have_key_events -->
	<div class="modal-backdrop" onclick={onClose}>
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<!-- svelte-ignore a11y_click_events_have_key_events -->
		<div class="modal" onclick={(e) => e.stopPropagation()}>
			<!-- Close button -->
			<button class="close-btn" onclick={onClose} aria-label="Close">
				<X size={16} strokeWidth={1.5} />
			</button>

			<!-- Icon -->
			<div class="modal-icon">
				{#if mode === 'pr-only'}
					<GitPullRequest size={20} strokeWidth={1.5} />
				{:else}
					<GitCommitHorizontal size={20} strokeWidth={1.5} />
				{/if}
			</div>

			<!-- Title -->
			<h2 class="modal-title">
				{mode === 'pr-only' ? 'Create a pull request' : 'Commit your changes'}
			</h2>

			<!-- Info rows -->
			<div class="info-rows">
				<div class="info-row">
					<span class="info-label">Branch</span>
					<span class="info-value">
						<GitBranch size={13} strokeWidth={1.5} />
						{branchDisplay}
					</span>
				</div>
				{#if mode === 'pr-only'}
					<div class="info-row">
						<span class="info-label">Commits</span>
						<span class="info-value">
							<span>{commitsAhead} ahead of {defaultBranch}</span>
						</span>
					</div>
				{:else}
					<div class="info-row">
						<span class="info-label">Changes</span>
						<span class="info-value">
							<span>{fileCount}</span>
							{#if additions > 0}
								<span class="stat-add">+{additions}</span>
							{/if}
							{#if deletions > 0}
								<span class="stat-del">-{deletions}</span>
							{/if}
						</span>
					</div>
				{/if}
			</div>

			{#if mode === 'pr-only'}
				<!-- PR title -->
				<div class="field">
					<label class="field-label" for="pr-title">PR title</label>
					<input
						id="pr-title"
						type="text"
						class="input"
						placeholder="Leave blank to auto-generate with AI"
						bind:value={prTitle}
					/>
				</div>

				<!-- PR description -->
				<div class="field">
					<label class="field-label" for="pr-body">Description</label>
					<textarea
						id="pr-body"
						class="textarea"
						placeholder="Leave blank to auto-generate with AI"
						bind:value={prBody}
						rows="4"
					></textarea>
				</div>
			{:else}
				<!-- Include unstaged toggle -->
				<button class="toggle-row" onclick={() => includeUnstaged = !includeUnstaged}>
					{#if includeUnstaged}
						<ToggleRight size={22} />
					{:else}
						<ToggleLeft size={22} />
					{/if}
					<span>Include unstaged</span>
				</button>

				<!-- Commit message -->
				<div class="field">
					<label class="field-label" for="commit-message">Commit message</label>
					<textarea
						id="commit-message"
						class="textarea"
						placeholder="Leave blank to auto-generate with AI"
						bind:value={commitMessage}
						rows="3"
					></textarea>
				</div>

				<!-- Next steps -->
				<div class="field">
					<span class="field-label">Next steps</span>
					<div class="radio-group">
						<label class="radio-option" class:selected={nextStep === 'commit'}>
							<input type="radio" name="nextStep" value="commit" bind:group={nextStep} />
							<span class="radio-dot"></span>
							<CircleDot size={16} strokeWidth={1.5} />
							<span class="radio-text">Commit</span>
							{#if nextStep === 'commit'}
								<span class="check-mark">&#10003;</span>
							{/if}
						</label>

						<label class="radio-option" class:selected={nextStep === 'push'} class:disabled={needsPull}>
							<input type="radio" name="nextStep" value="push" bind:group={nextStep} disabled={needsPull} />
							<span class="radio-dot"></span>
							<ArrowUp size={16} strokeWidth={1.5} />
							<span class="radio-text">Commit and push</span>
							{#if needsPull}
								<span class="radio-hint">Pull required first</span>
							{:else if nextStep === 'push'}
								<span class="check-mark">&#10003;</span>
							{/if}
						</label>

						<label class="radio-option" class:selected={nextStep === 'pr'} class:disabled={isOnDefaultBranch || needsPull}>
							<input type="radio" name="nextStep" value="pr" bind:group={nextStep} disabled={isOnDefaultBranch || needsPull} />
							<span class="radio-dot"></span>
							<Github size={16} strokeWidth={1.5} />
							<span class="radio-text">Commit, push, and create PR</span>
							{#if needsPull}
								<span class="radio-hint">Pull required first</span>
							{:else if isOnDefaultBranch}
								<span class="radio-hint">Not available on {defaultBranch}</span>
							{:else if nextStep === 'pr'}
								<span class="check-mark">&#10003;</span>
							{/if}
						</label>
					</div>
				</div>
			{/if}

			<!-- Action -->
			<button class="continue-btn" onclick={handleContinue}>
				{mode === 'pr-only' ? 'Create pull request' : 'Continue'}
			</button>
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
		background: color-mix(in oklch, var(--background) 80%, transparent);
		backdrop-filter: blur(4px);
		animation: fade-in 150ms ease;
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
		background: color-mix(in oklch, var(--foreground) 8%, transparent);
		display: flex;
		align-items: center;
		justify-content: center;
		color: var(--foreground);
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

	.stat-add {
		color: var(--success);
		font-weight: 600;
	}

	.stat-del {
		color: var(--destructive);
		font-weight: 600;
	}

	/* Toggle */
	.toggle-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 4px 0;
		background: none;
		border: none;
		color: var(--foreground);
		font-size: 0.8125rem;
		cursor: pointer;
	}

	.toggle-row :global(svg) {
		color: var(--muted-foreground);
		transition: color 150ms ease;
	}

	.toggle-row:has(+ .field) :global(svg:first-child) {
		color: var(--primary);
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

	.input {
		width: 100%;
		padding: 10px 14px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-family: inherit;
		outline: none;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.input::placeholder {
		color: var(--muted-foreground);
		opacity: 0.7;
	}

	.input:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 15%, transparent);
	}

	.textarea {
		width: 100%;
		padding: 10px 14px;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--foreground);
		font-size: 0.8125rem;
		font-family: inherit;
		outline: none;
		resize: vertical;
		min-height: 60px;
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.textarea::placeholder {
		color: var(--muted-foreground);
		opacity: 0.7;
	}

	.textarea:focus {
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 15%, transparent);
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

	.radio-option.disabled {
		opacity: 0.4;
		cursor: not-allowed;
	}

	.radio-option.disabled:hover {
		background: transparent;
	}

	.radio-hint {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
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

	.continue-btn:hover {
		opacity: 0.9;
	}
</style>
