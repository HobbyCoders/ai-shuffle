<script lang="ts">
	/**
	 * PluginHookItem - Read-only display of a plugin-contributed hook.
	 * Emits a Studio-v2 .set-row; primitives come from project-tabs.css (.ptab),
	 * so this is only used inside a `.ptab` wrapper (ProjectHooksTab).
	 */
	import { FileCode } from 'lucide-svelte';
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

<div class="set-row">
	<div class="lab">
		<h4>
			{hook.plugin_name}
			<span class="badge" style="background: color-mix(in oklch, {eventColor} 15%, transparent); color: {eventColor};">{hook.event}</span>
			{#if hook.tool_matcher}
				<span class="badge mono" title="Tool matcher pattern">{hook.tool_matcher}</span>
			{/if}
			<span class="badge muted">Plugin</span>
		</h4>
		<p>
			<span class="ptype"><FileCode size={11} /> {hook.hook_type}</span>{#if hook.description} · {hook.description}{/if}
		</p>
	</div>
</div>

<style>
	.ptype {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		vertical-align: middle;
	}
</style>
