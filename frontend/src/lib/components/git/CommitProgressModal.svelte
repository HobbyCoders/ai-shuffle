<script lang="ts">
	/**
	 * CommitProgressModal - Step-by-step progress for commit/push/PR
	 *
	 * Shows a spinner on the current step, checkmarks on completed steps,
	 * and updates the title dynamically.
	 */
	import { Check, Loader2, X, AlertCircle } from 'lucide-svelte';

	type CommitAction = 'commit' | 'push' | 'pr' | 'finish' | 'pull';
	type ModalMode = 'commit' | 'pr-only' | 'finish' | 'pull' | 'push';

	interface ProgressStep {
		label: string;
		sublabel?: string;
	}

	interface Props {
		open: boolean;
		action: CommitAction;
		mode?: ModalMode;
		currentStep: number; // 1-indexed, step currently in progress
		completed: boolean;
		error: string | null;
		branchName?: string;
		isGenerating?: boolean;
		/** Whether cleanup steps are included (finish mode) */
		hasCleanup?: boolean;
		onClose: () => void;
	}

	let { open, action, mode = 'commit', currentStep, completed, error, branchName = '', isGenerating = false, hasCleanup = false, onClose }: Props = $props();

	const steps = $derived.by((): ProgressStep[] => {
		if (mode === 'push') {
			return [{ label: branchName ? `Pushing to ${branchName}` : 'Pushing to remote' }];
		}

		if (mode === 'pull') {
			return [{ label: 'Pulling latest changes' }];
		}

		if (mode === 'finish') {
			const result: ProgressStep[] = [];
			result.push({ label: 'Merging pull request' });
			if (hasCleanup) {
				result.push({ label: 'Cleaning up worktree and branch' });
			}
			return result;
		}

		if (mode === 'pr-only') {
			const result: ProgressStep[] = [];
			if (isGenerating) {
				result.push({ label: 'Generating PR description' });
			}
			result.push({
				label: branchName ? `Pushing to branch ${branchName}` : 'Pushing to branch'
			});
			result.push({ label: 'Creating a pull request' });
			return result;
		}

		const result: ProgressStep[] = [];
		if (isGenerating) {
			result.push({ label: 'Generating commit message' });
		}
		result.push({ label: 'Committing changes' });

		if (action === 'push' || action === 'pr') {
			result.push({
				label: branchName ? `Pushing to branch ${branchName}` : 'Pushing to branch'
			});
		}

		return result;
	});

	const title = $derived.by(() => {
		if (error) return 'Error';
		if (completed) return 'Done';

		if (mode === 'push') {
			return 'Pushing changes';
		}

		if (mode === 'pull') {
			return 'Pulling changes';
		}

		if (mode === 'finish') {
			if (currentStep === 1) return 'Merging PR';
			if (currentStep === 2) return 'Cleaning up';
			return 'Processing';
		}

		if (mode === 'pr-only') {
			if (isGenerating && currentStep <= 1) return 'Generating description';
			const pushStep = isGenerating ? 2 : 1;
			const prStep = isGenerating ? 3 : 2;
			if (currentStep === pushStep) return 'Pushing changes';
			if (currentStep === prStep) return 'Creating a PR';
			return 'Processing';
		}

		if (isGenerating && currentStep <= 1) return 'Generating message';
		const commitStep = isGenerating ? 2 : 1;
		if (currentStep <= commitStep) return 'Committing changes';
		if (currentStep === commitStep + 1 && (action === 'push' || action === 'pr')) return 'Pushing changes';
		return 'Processing';
	});

	const subtitle = $derived(
		error ? error : (completed ? 'All steps completed successfully.' : 'Hold tight, this may take a few moments...')
	);

	function handleKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape' && (completed || error)) {
			e.preventDefault();
			onClose();
		}
	}
</script>

<svelte:window onkeydown={handleKeydown} />

{#if open}
	<div class="modal-backdrop">
		<div class="modal">
			<!-- Close button - only when done or error -->
			{#if completed || error}
				<button class="close-btn" onclick={onClose} aria-label="Close">
					<X size={16} strokeWidth={1.5} />
				</button>
			{/if}

			<!-- Icon -->
			<div class="modal-icon" class:error-icon={!!error} class:success-icon={completed && !error}>
				{#if error}
					<AlertCircle size={20} strokeWidth={1.5} />
				{:else if completed}
					<Check size={20} strokeWidth={2} />
				{:else}
					<Loader2 size={20} strokeWidth={1.5} class="spin" />
				{/if}
			</div>

			<!-- Title -->
			<h2 class="modal-title">{title}</h2>
			<p class="modal-subtitle">{subtitle}</p>

			<!-- Steps -->
			<div class="steps">
				{#each steps as step, i}
					{@const stepNum = i + 1}
					{@const isActive = !completed && !error && currentStep === stepNum}
					{@const isComplete = completed || currentStep > stepNum}
					{@const isPending = !isActive && !isComplete}
					<div
						class="step"
						class:active={isActive}
						class:complete={isComplete}
						class:pending={isPending}
					>
						<div class="step-indicator">
							{#if isComplete}
								<Check size={14} strokeWidth={2.5} />
							{:else if isActive}
								<Loader2 size={14} strokeWidth={2} class="spin" />
							{:else}
								<div class="step-dot"></div>
							{/if}
						</div>
						<div class="step-content">
							<span class="step-label">{step.label}</span>
							{#if step.sublabel}
								<span class="step-sublabel">{step.sublabel}</span>
							{/if}
						</div>
					</div>
				{/each}
			</div>
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
		max-width: 400px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 16px;
		padding: 28px;
		box-shadow: var(--shadow-l);
		display: flex;
		flex-direction: column;
		gap: 12px;
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

	.modal-icon.error-icon {
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
		color: var(--destructive);
	}

	.modal-icon.success-icon {
		background: color-mix(in oklch, var(--success) 12%, transparent);
		color: var(--success);
	}

	.modal-title {
		font-size: 1.125rem;
		font-weight: 600;
		color: var(--foreground);
		margin: 0;
		line-height: 1.2;
	}

	.modal-subtitle {
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		margin: 0;
		line-height: 1.4;
	}

	/* Steps */
	.steps {
		display: flex;
		flex-direction: column;
		gap: 4px;
		margin-top: 4px;
	}

	.step {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 8px 0;
		transition: opacity 150ms ease;
	}

	.step.pending {
		opacity: 0.4;
	}

	.step.active,
	.step.complete {
		opacity: 1;
	}

	.step-indicator {
		width: 22px;
		height: 22px;
		border-radius: 50%;
		display: flex;
		align-items: center;
		justify-content: center;
		flex-shrink: 0;
		margin-top: 1px;
	}

	.step.pending .step-indicator {
		border: 1.5px solid var(--border);
	}

	.step.active .step-indicator {
		color: var(--foreground);
	}

	.step.complete .step-indicator {
		color: var(--success);
	}

	.step-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: var(--muted-foreground);
		opacity: 0.5;
	}

	.step-content {
		display: flex;
		flex-direction: column;
		gap: 2px;
		min-width: 0;
	}

	.step-label {
		font-size: 0.8125rem;
		color: var(--foreground);
		line-height: 1.4;
	}

	.step.pending .step-label {
		color: var(--muted-foreground);
	}

	.step-sublabel {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	/* Spin animation */
	:global(.spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
