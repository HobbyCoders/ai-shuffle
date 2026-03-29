<script lang="ts">
	/**
	 * HookListItem - Individual hook item in the hooks list
	 * Uses fp-list-item design system classes from card-design-system.css
	 */
	import { Play, Trash2, ToggleLeft, ToggleRight, Loader2, AlertCircle, ChevronRight } from 'lucide-svelte';
	import type { Hook } from '$lib/api/client';

	interface Props {
		hook: Hook;
		testing?: boolean;
		saving?: boolean;
		onToggle: () => void;
		onEdit: () => void;
		onDelete: () => void;
		onTest: () => void;
	}

	let { hook, testing = false, saving = false, onToggle, onEdit, onDelete, onTest }: Props = $props();

	const eventColors: Record<string, string> = {
		PreToolUse: 'var(--info)',
		PostToolUse: 'var(--info)',
		Stop: 'var(--success)',
		SessionStart: 'var(--primary)',
		SessionEnd: 'var(--primary)',
		UserPromptSubmit: 'var(--warning)',
		PreCompact: 'var(--muted-foreground)',
		SubagentStop: 'var(--accent-foreground)',
		Notification: 'var(--warning)'
	};

	const typeColors: Record<string, string> = {
		bash: 'var(--primary)',
		webhook: 'var(--info)',
		message: 'var(--success)'
	};

	const eventColor = $derived(eventColors[hook.event] || 'var(--muted-foreground)');
	const indicatorColor = $derived(typeColors[hook.hook_type] || 'var(--primary)');
</script>

<div
	class="fp-list-item"
	class:hook-disabled={!hook.enabled}
	role="button"
	tabindex="0"
	onclick={onEdit}
	onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onEdit(); } }}
>
	<div class="fp-item-indicator" style="background: {indicatorColor};"></div>

	<div class="fp-item-content">
		<div class="fp-item-header">
			<span class="fp-item-name">{hook.name}</span>
			<span class="fp-item-badge" style="background: color-mix(in oklch, {eventColor} 15%, transparent); color: {eventColor};">
				{hook.event}
			</span>
			{#if hook.tool_matcher}
				<span class="fp-item-badge hook-matcher-badge" title="Tool matcher pattern">
					{hook.tool_matcher}
				</span>
			{/if}
		</div>
		<div class="fp-item-meta">
			<span class="fp-meta-chip">{hook.hook_type}</span>
			{#if hook.hook_type === 'bash' && hook.command}
				<span class="fp-meta-sep">/</span>
				<span class="fp-meta-chip hook-command-chip">
					<code>{hook.command.substring(0, 50)}{hook.command.length > 50 ? '...' : ''}</code>
				</span>
			{:else if hook.hook_type === 'webhook' && hook.url}
				<span class="fp-meta-sep">/</span>
				<span class="fp-meta-chip">{hook.url.substring(0, 50)}{hook.url.length > 50 ? '...' : ''}</span>
			{:else if hook.hook_type === 'message' && hook.message_template}
				<span class="fp-meta-sep">/</span>
				<span class="fp-meta-chip">{hook.message_template.substring(0, 50)}{hook.message_template.length > 50 ? '...' : ''}</span>
			{/if}
			{#if hook.failure_count > 0}
				<span class="fp-meta-sep">/</span>
				<span class="fp-meta-chip hook-failure-chip">
					<AlertCircle size={11} />
					{hook.failure_count} failure{hook.failure_count > 1 ? 's' : ''}
				</span>
			{/if}
		</div>
	</div>

	<!-- svelte-ignore a11y_no_static_element_interactions -->
	<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}>
		<button
			class="fp-action-btn"
			title="Test hook"
			onclick={onTest}
			disabled={testing || saving}
		>
			{#if testing}
				<Loader2 size={14} class="animate-spin" />
			{:else}
				<Play size={14} />
			{/if}
		</button>
		<button
			class="fp-action-btn fp-action-btn--destructive"
			title="Delete hook"
			onclick={onDelete}
			disabled={saving}
		>
			<Trash2 size={14} />
		</button>
		<button
			class="hook-toggle-btn"
			title={hook.enabled ? 'Disable hook' : 'Enable hook'}
			onclick={onToggle}
			disabled={saving}
		>
			{#if saving}
				<Loader2 size={18} class="animate-spin" />
			{:else if hook.enabled}
				<ToggleRight size={18} class="enabled" />
			{:else}
				<ToggleLeft size={18} class="disabled" />
			{/if}
		</button>
	</div>

	<div class="fp-item-chevron"><ChevronRight size={16} /></div>
</div>

<style>
	.hook-disabled {
		opacity: 0.55;
	}

	/* Matcher badge uses mono font */
	.hook-matcher-badge {
		font-family: var(--font-mono);
	}

	/* Command chip inherits mono */
	.hook-command-chip code {
		font-family: var(--font-mono);
		font-size: inherit;
	}

	/* Failure chip warning color */
	.hook-failure-chip {
		color: var(--warning) !important;
	}

	.hook-failure-chip :global(svg) {
		flex-shrink: 0;
	}

	/* Toggle button — special wider action */
	.hook-toggle-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 28px;
		border-radius: var(--radius-sm, calc(var(--radius, 0.75rem) * 0.5));
		background: transparent;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.hook-toggle-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.hook-toggle-btn :global(.enabled) {
		color: var(--success);
	}

	.hook-toggle-btn :global(.disabled) {
		color: var(--muted-foreground);
	}

	/* Spinner animation */
	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
