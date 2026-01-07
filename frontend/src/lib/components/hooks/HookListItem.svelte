<script lang="ts">
	/**
	 * HookListItem - Individual hook item in the hooks list
	 */
	import { Terminal, Globe, Play, Settings, Trash2, ToggleLeft, ToggleRight, Loader2, AlertCircle, CheckCircle } from 'lucide-svelte';
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

	const eventColor = $derived(eventColors[hook.event] || 'var(--muted-foreground)');
</script>

<div class="hook-item" class:disabled={!hook.enabled}>
	<div class="hook-icon" class:bash={hook.hook_type === 'bash'} class:webhook={hook.hook_type === 'webhook'}>
		{#if hook.hook_type === 'bash'}
			<Terminal size={16} />
		{:else}
			<Globe size={16} />
		{/if}
	</div>

	<div class="hook-info">
		<div class="hook-header">
			<span class="hook-name">{hook.name}</span>
			<span class="hook-event" style="--event-color: {eventColor}">
				{hook.event}
			</span>
			{#if hook.tool_matcher}
				<span class="hook-matcher" title="Tool matcher pattern">
					{hook.tool_matcher}
				</span>
			{/if}
		</div>
		<div class="hook-detail">
			{#if hook.hook_type === 'bash' && hook.command}
				<code class="hook-command">{hook.command.substring(0, 60)}{hook.command.length > 60 ? '...' : ''}</code>
			{:else if hook.hook_type === 'webhook' && hook.url}
				<span class="hook-url">{hook.url.substring(0, 60)}{hook.url.length > 60 ? '...' : ''}</span>
			{/if}
		</div>
		{#if hook.failure_count > 0}
			<div class="hook-warning">
				<AlertCircle size={12} />
				<span>{hook.failure_count} recent failure{hook.failure_count > 1 ? 's' : ''}</span>
			</div>
		{/if}
	</div>

	<div class="hook-actions">
		<button
			class="action-btn"
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
			class="action-btn"
			title="Edit hook"
			onclick={onEdit}
			disabled={saving}
		>
			<Settings size={14} />
		</button>
		<button
			class="action-btn delete"
			title="Delete hook"
			onclick={onDelete}
			disabled={saving}
		>
			<Trash2 size={14} />
		</button>
		<button
			class="toggle-btn"
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
</div>

<style>
	.hook-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 8px;
		transition: all 150ms ease;
	}

	.hook-item:hover {
		border-color: var(--ring);
	}

	.hook-item.disabled {
		opacity: 0.6;
	}

	.hook-icon {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: 6px;
		flex-shrink: 0;
	}

	.hook-icon.bash {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
	}

	.hook-icon.webhook {
		background: color-mix(in oklch, var(--info) 20%, transparent);
		color: var(--info);
	}

	.hook-info {
		flex: 1;
		min-width: 0;
	}

	.hook-header {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
		margin-bottom: 4px;
	}

	.hook-name {
		font-weight: 500;
		color: var(--foreground);
		font-size: 0.875rem;
	}

	.hook-event {
		padding: 2px 6px;
		font-size: 0.6875rem;
		font-weight: 500;
		border-radius: 4px;
		background: color-mix(in oklch, var(--event-color) 15%, transparent);
		color: var(--event-color);
	}

	.hook-matcher {
		padding: 2px 6px;
		font-size: 0.6875rem;
		font-family: monospace;
		border-radius: 4px;
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.hook-detail {
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.hook-command {
		font-family: monospace;
		font-size: 0.6875rem;
		padding: 2px 4px;
		background: var(--muted);
		border-radius: 3px;
	}

	.hook-url {
		word-break: break-all;
	}

	.hook-warning {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: 6px;
		font-size: 0.6875rem;
		color: var(--warning);
	}

	.hook-actions {
		display: flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
	}

	.action-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border-radius: 6px;
		background: transparent;
		border: none;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 150ms ease;
	}

	.action-btn:hover:not(:disabled) {
		background: var(--accent);
		color: var(--foreground);
	}

	.action-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.action-btn.delete:hover:not(:disabled) {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.toggle-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 28px;
		border-radius: 6px;
		background: transparent;
		border: none;
		cursor: pointer;
		transition: all 150ms ease;
	}

	.toggle-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.toggle-btn :global(.enabled) {
		color: var(--success);
	}

	.toggle-btn :global(.disabled) {
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
