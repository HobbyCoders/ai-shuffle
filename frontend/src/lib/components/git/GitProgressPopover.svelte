<script lang="ts">
	/**
	 * GitProgressPopover — compact step-list panel for the active git operation.
	 *
	 * Auto-shows while an operation is active (Commit / Push / Pull / Create PR
	 * chains and the server-polled Finish / Ship It operations). Each step
	 * carries a spinner / check / warning / error icon. On success the store
	 * collapses it back to idle after a couple of seconds. On error (or a pinned
	 * success that needs attention) it stays open until Dismiss, with the files
	 * the outcome names (a stopped pull's conflicts) and its follow-up buttons
	 * ("Pull first", "Stash & pull", "Resolve with agent"). This is where a Git
	 * panel action reports back; the notification bell only keeps history.
	 *
	 * Styled after GitHeaderActions' .branch-dropdown, including its mobile
	 * position:fixed escape hatch so it isn't clipped by the header's horizontal
	 * scroll rail.
	 */
	import { Loader2, Check, X, AlertTriangle, Minus, Circle, FileWarning } from 'lucide-svelte';
	import { gitOperationState, gitOperations } from '$lib/stores/gitOperations.svelte';

	const state = $derived($gitOperationState);
	const visible = $derived(state.active);
	const settled = $derived(state.status === 'error' || (state.status === 'done' && state.pinned));
	const uid = $props.id();
	// Older snapshots (and test fixtures) may carry no follow-ups at all.
	const actions = $derived(state.actions ?? []);
</script>

{#if visible}
	<div class="git-progress" class:error={state.status === 'error'} role="status" aria-live="polite">
		<div class="gp-header">
			<span class="gp-title">{state.title ?? 'Working…'}</span>
			{#if state.status === 'running'}
				<Loader2 size={14} strokeWidth={2} class="spin gp-head-icon running" />
			{:else if state.status === 'done'}
				<Check size={14} strokeWidth={2.5} class="gp-head-icon done" />
			{:else if state.status === 'error'}
				<X size={14} strokeWidth={2.5} class="gp-head-icon errored" />
			{/if}
		</div>

		{#if state.steps.length > 0}
			<ul class="gp-steps">
				{#each state.steps as step (step.id)}
					<li class="gp-step" class:is-error={step.status === 'error'}>
						<span class="gp-icon">
							{#if step.status === 'running'}
								<Loader2 size={13} strokeWidth={2} class="spin running" />
							{:else if step.status === 'done'}
								<Check size={13} strokeWidth={2.5} class="done" />
							{:else if step.status === 'error'}
								<X size={13} strokeWidth={2.5} class="errored" />
							{:else if step.status === 'warning'}
								<AlertTriangle size={13} strokeWidth={2} class="warned" />
							{:else if step.status === 'skipped'}
								<Minus size={13} strokeWidth={2} class="skipped" />
							{:else}
								<Circle size={9} strokeWidth={2} class="pending" />
							{/if}
						</span>
						<span class="gp-label">
							{step.label}
							{#if step.detail}
								<span class="gp-detail">{step.detail}</span>
							{/if}
							{#if step.status === 'error' && step.error}
								<span class="gp-step-error">{step.error}</span>
							{/if}
						</span>
					</li>
				{/each}
			</ul>
		{:else if state.status === 'running'}
			<p class="gp-empty">Starting…</p>
		{/if}

		{#if state.status === 'error'}
			{#if state.error && !state.steps.some((s) => s.status === 'error' && s.error === state.error)}
				<p class="gp-error-msg">{state.error}</p>
			{/if}
		{/if}

		{#if settled}
			{#if state.files && state.files.paths.length > 0}
				<div class="gp-files">
					<span class="gp-files-label" id="{uid}-files">{state.files.label}</span>
					<ul aria-labelledby="{uid}-files">
						{#each state.files.paths as path (path)}
							<li><FileWarning size={12} strokeWidth={1.8} class="warned" aria-hidden="true" /><span class="gp-path" title={path}>{path}</span></li>
						{/each}
					</ul>
				</div>
			{/if}
			<div class="gp-foot">
				{#each actions as action (action.id)}
					<button type="button" class="gp-action" class:primary={action.primary}
						disabled={!!action.disabledReason}
						aria-describedby={action.disabledReason ? `${uid}-${action.id}-why` : undefined}
						onclick={() => action.onClick()}>
						{action.label}
					</button>
				{/each}
				<button type="button" class="gp-dismiss" onclick={() => gitOperations.dismiss()}>
					Dismiss
				</button>
			</div>
			{#each actions as action (action.id)}
				{#if action.disabledReason}
					<p class="gp-why" id="{uid}-{action.id}-why">{action.label}: {action.disabledReason}</p>
				{/if}
			{/each}
		{/if}
	</div>
{/if}

<style>
	/* In-flow operation feedback, never another positioned overlay. */
	.git-progress { margin: 12px 14px; padding: 12px; border: 1px solid var(--border); border-radius: .625rem; background: color-mix(in oklch, var(--muted) 30%, transparent); }

	.git-progress.error {
		border-color: color-mix(in oklab, var(--destructive) 55%, var(--border));
	}

	.gp-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 6px;
	}

	.gp-title {
		font-size: 0.75rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.gp-steps {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.gp-step {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		font-size: 0.75rem;
		line-height: 1.35;
		color: var(--muted-foreground);
	}

	.gp-step.is-error {
		color: var(--foreground);
	}

	.gp-icon {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 14px;
		height: 16px;
		flex-shrink: 0;
	}

	.gp-label {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	.gp-detail {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		opacity: 0.85;
		word-break: break-word;
	}

	.gp-step-error {
		font-size: 0.6875rem;
		color: var(--destructive);
		word-break: break-word;
	}

	.gp-empty {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.gp-error-msg {
		margin: 8px 0 0;
		font-size: 0.75rem;
		color: var(--destructive);
		word-break: break-word;
	}

	.gp-files {
		margin-top: 8px;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.gp-files-label {
		font-size: 0.6875rem;
		font-weight: 600;
		color: var(--muted-foreground);
	}

	.gp-files ul {
		list-style: none;
		margin: 0;
		padding: 6px 8px;
		max-height: 9rem;
		overflow-y: auto;
		display: flex;
		flex-direction: column;
		gap: 3px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: color-mix(in oklch, var(--background) 60%, transparent);
	}

	.gp-files li {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}

	.gp-path {
		min-width: 0;
		font-family: var(--font-mono);
		font-size: 0.6875rem;
		color: var(--foreground);
		overflow-wrap: anywhere;
	}

	.gp-foot {
		display: flex;
		flex-wrap: wrap;
		justify-content: flex-end;
		gap: 6px;
		margin-top: 8px;
	}

	.gp-action {
		min-height: 36px;
		padding: 0 12px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--foreground);
		font-size: 0.6875rem;
		font-weight: 600;
		cursor: pointer;
		transition: background 150ms ease, border-color 150ms ease, opacity 150ms ease;
	}

	.gp-action.primary {
		background: var(--primary);
		border-color: var(--primary);
		color: var(--primary-foreground);
	}

	.gp-action:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.gp-action.primary:hover:not(:disabled) {
		background: color-mix(in oklab, var(--primary) 88%, var(--foreground));
	}

	.gp-action:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.gp-action:focus-visible,
	.gp-dismiss:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.gp-why {
		margin: 6px 0 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		text-align: right;
	}

	.gp-dismiss {
		min-height: 36px;
		padding: 0 10px;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.gp-dismiss:hover {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	/* Icon colors */
	:global(.sheet-layout) .gp-dismiss,
	:global(.sheet-layout) .gp-action { min-height: 44px; }

	.git-progress :global(.running) { color: var(--primary); }
	.git-progress :global(.done) { color: var(--success); }
	.git-progress :global(.errored) { color: var(--destructive); }
	.git-progress :global(.warned) { color: var(--warning, #e8a735); }
	.git-progress :global(.skipped) { color: var(--muted-foreground); opacity: 0.6; }
	.git-progress :global(.pending) { color: var(--muted-foreground); opacity: 0.5; }

	.git-progress :global(svg) {
		flex-shrink: 0;
	}

	:global(.spin) {
		animation: gp-spin 1s linear infinite;
	}

	@keyframes gp-spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}

	@media (prefers-reduced-motion: reduce) {
		.git-progress { animation: none; }
		:global(.spin) { animation: none; }
	}
</style>
