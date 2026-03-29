<script lang="ts">
	/**
	 * PluginHookItem - Read-only display of plugin-contributed hooks
	 * Uses fp-list-item design system classes from card-design-system.css
	 */
	import { FileCode, ChevronRight } from 'lucide-svelte';
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

<div class="fp-list-item plugin-hook-row">
	<div class="fp-item-indicator" style="background: var(--muted-foreground);"></div>

	<div class="fp-item-content">
		<div class="fp-item-header">
			<span class="fp-item-name">{hook.plugin_name}</span>
			<span class="fp-item-badge" style="background: color-mix(in oklch, {eventColor} 15%, transparent); color: {eventColor};">
				{hook.event}
			</span>
			{#if hook.tool_matcher}
				<span class="fp-item-badge plugin-matcher-badge" title="Tool matcher pattern">
					{hook.tool_matcher}
				</span>
			{/if}
			<span class="fp-item-badge">Plugin</span>
		</div>
		<div class="fp-item-meta">
			<span class="fp-meta-chip">
				<FileCode size={11} />
				{hook.hook_type}
			</span>
			{#if hook.description}
				<span class="fp-meta-sep">/</span>
				<span class="fp-meta-chip">{hook.description}</span>
			{/if}
		</div>
	</div>

	<div class="fp-item-chevron"><ChevronRight size={16} /></div>
</div>

<style>
	/* Plugin rows are non-interactive — reduce cursor and hover */
	.plugin-hook-row {
		cursor: default;
	}

	.plugin-hook-row:hover {
		background: transparent;
	}

	.plugin-matcher-badge {
		font-family: var(--font-mono);
	}
</style>
