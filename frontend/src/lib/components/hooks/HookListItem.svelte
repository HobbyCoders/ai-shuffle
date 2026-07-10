<script lang="ts">
	/**
	 * HookListItem - Individual hook row in the project Hooks tab.
	 * Emits a Studio-v2 .set-row; primitives come from project-tabs.css (.ptab),
	 * so this is only used inside a `.ptab` wrapper (ProjectHooksTab).
	 */
	import { Play, Trash2, Loader2, AlertCircle } from 'lucide-svelte';
	import type { Hook } from '$lib/api/client';
	import RowActionMenu from '$lib/components/ui/RowActionMenu.svelte';

	interface Props {
		hook: Hook;
		testing?: boolean;
		saving?: boolean;
		/** Touch/narrow only: whether this row's ⋮ action tray is expanded. */
		menuOpen?: boolean;
		/** Toggle this row's action tray (parent owns one-open-at-a-time state). */
		onToggleMenu?: (e: MouseEvent | KeyboardEvent) => void;
		onToggle: () => void;
		onDelete: () => void;
		onTest: () => void;
	}

	let {
		hook,
		testing = false,
		saving = false,
		menuOpen = false,
		onToggleMenu = () => {},
		onToggle,
		onDelete,
		onTest
	}: Props = $props();

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

	const detail = $derived(
		hook.hook_type === 'bash' && hook.command
			? hook.command
			: hook.hook_type === 'webhook' && hook.url
				? hook.url
				: hook.hook_type === 'message' && hook.message_template
					? hook.message_template
					: ''
	);
	const detailShort = $derived(detail.length > 60 ? detail.slice(0, 60) + '…' : detail);
</script>

<div class="set-row" class:hook-off={!hook.enabled}>
	<div class="lab">
		<h4>
			{hook.name}
			<span class="badge" style="background: color-mix(in oklch, {eventColor} 15%, transparent); color: {eventColor};">{hook.event}</span>
			{#if hook.tool_matcher}
				<span class="badge mono" title="Tool matcher pattern">{hook.tool_matcher}</span>
			{/if}
		</h4>
		<p>
			<span class="htype">{hook.hook_type}</span>{#if detailShort} · <span class="hcmd">{detailShort}</span>{/if}{#if hook.failure_count > 0} · <span class="hfail"><AlertCircle size={11} /> {hook.failure_count} failure{hook.failure_count > 1 ? 's' : ''}</span>{/if}
		</p>
	</div>

	<div class="ctl">
		<RowActionMenu open={menuOpen} onToggle={onToggleMenu} label="Hook actions">
			<button class="icon-btn" title="Test hook" onclick={(e) => { e.stopPropagation(); onTest(); }} disabled={testing || saving}>
				{#if testing}<Loader2 size={14} class="animate-spin" />{:else}<Play size={14} />{/if}
			</button>
			<button class="icon-btn danger" title="Delete hook" onclick={(e) => { e.stopPropagation(); onDelete(); }} disabled={saving}>
				<Trash2 size={14} />
			</button>
		</RowActionMenu>
		<button
			type="button"
			class="switch"
			class:on={hook.enabled}
			role="switch"
			aria-checked={hook.enabled}
			aria-label={hook.enabled ? 'Disable hook' : 'Enable hook'}
			title={hook.enabled ? 'Disable hook' : 'Enable hook'}
			onclick={onToggle}
			disabled={saving}
		><i></i></button>
	</div>
</div>

<style>
	.hook-off {
		opacity: 0.6;
	}

	.hcmd {
		font-family: var(--font-mono, monospace);
	}

	.hfail {
		color: var(--warning);
		display: inline-flex;
		align-items: center;
		gap: 3px;
		vertical-align: middle;
	}

	.hfail :global(svg) {
		flex-shrink: 0;
	}

	:global(.animate-spin) {
		animation: spin 1s linear infinite;
	}

	@keyframes spin {
		from { transform: rotate(0deg); }
		to { transform: rotate(360deg); }
	}
</style>
