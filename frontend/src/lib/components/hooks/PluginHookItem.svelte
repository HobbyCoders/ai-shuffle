<script lang="ts">
	/**
	 * PluginHookItem - Read-only display of plugin-contributed hooks
	 */
	import { Puzzle, FileCode } from 'lucide-svelte';
	import type { PluginHookInfo } from '$lib/api/client';

	interface Props {
		hook: PluginHookInfo;
	}

	let { hook }: Props = $props();

	const eventColors: Record<string, string> = {
		PreToolUse: 'var(--info)',
		PostToolUse: 'var(--info)',
		Stop: 'var(--success)',
		SessionStart: 'var(--primary)',
		SessionEnd: 'var(--primary)',
		UserPromptSubmit: 'var(--warning)',
		PreCompact: 'var(--muted-foreground)',
		SubagentStop: 'var(--info)',
		Notification: 'var(--warning)'
	};

	const eventColor = $derived(eventColors[hook.event] || 'var(--muted-foreground)');
</script>

<div class="plugin-hook-item">
	<div class="hook-icon">
		<Puzzle size={16} />
	</div>

	<div class="hook-info">
		<div class="hook-header">
			<span class="plugin-name">{hook.plugin_name}</span>
			<span class="hook-event" style="--event-color: {eventColor}">
				{hook.event}
			</span>
			{#if hook.tool_matcher}
				<span class="hook-matcher" title="Tool matcher pattern">
					{hook.tool_matcher}
				</span>
			{/if}
		</div>
		{#if hook.description}
			<div class="hook-description">
				{hook.description}
			</div>
		{/if}
		<div class="hook-meta">
			<span class="hook-type">
				<FileCode size={12} />
				{hook.hook_type}
			</span>
		</div>
	</div>

	<div class="read-only-badge">
		Plugin
	</div>
</div>

<style>
	.plugin-hook-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 12px;
		background: var(--secondary);
		border: 1px dashed var(--border);
		border-radius: var(--radius, 0.75rem);
	}

	.hook-icon {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		border-radius: calc(var(--radius, 0.75rem) * 0.5);
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		flex-shrink: 0;
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

	.plugin-name {
		font-weight: 500;
		color: var(--foreground);
		font-size: 0.875rem;
	}

	.hook-event {
		padding: 2px 6px;
		font-size: 0.6875rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: color-mix(in oklch, var(--event-color) 15%, transparent);
		color: var(--event-color);
	}

	.hook-matcher {
		padding: 2px 6px;
		font-size: 0.6875rem;
		font-family: monospace;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: var(--muted);
		color: var(--muted-foreground);
	}

	.hook-description {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		margin-bottom: 6px;
	}

	.hook-meta {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.hook-type {
		display: flex;
		align-items: center;
		gap: 4px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.read-only-badge {
		padding: 2px 8px;
		font-size: 0.6875rem;
		font-weight: 500;
		border-radius: calc(var(--radius, 0.75rem) * 0.33);
		background: var(--muted);
		color: var(--muted-foreground);
		flex-shrink: 0;
	}
</style>
