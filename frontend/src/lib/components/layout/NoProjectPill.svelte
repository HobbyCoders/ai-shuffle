<script lang="ts">
	/**
	 * NoProjectPill — the header's mark for a running session without a
	 * project (#371 step 4). A quiet dashed pill; pressing it opens the attach
	 * sheet, which says what attaching adds and that it is one-way.
	 */
	import { FolderPlus } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles, sessions } from '$lib/stores/app-data.svelte';
	import { archivedChats } from '$lib/stores/archived-chats';
	import { subagentChats } from '$lib/stores/subagent-chats';
	import { attachBlockedReason, noProjectPillVisible, type AttachState } from '$lib/components/chat/session-scope';
	import AttachProjectSheet from './AttachProjectSheet.svelte';

	interface Props {
		/** The legacy assistant home never takes a project. */
		assistantMode?: boolean;
		/** Phone top bar: icon-sized pill. */
		compact?: boolean;
	}

	let { assistantMode = false, compact = false }: Props = $props();

	let open = $state(false);

	const sessionId = $derived($activeSession.sessionId);
	const row = $derived($sessions.find((s) => s.id === sessionId) ?? null);
	const attachState = $derived<AttachState>({
		sessionId,
		hasProject: $activeSession.mode !== 'chat' || !!$activeSession.project || !!$activeSession.projectV2Id,
		isRemote: !!(row?.is_remote ?? $activeSession.isRemote),
		busy: !!($activeSession.isStreaming || $activeSession.optimisticStreaming),
		// A turn parked on background work: the server refuses the attach then too.
		backgroundWork: !!row?.parked_task_kind,
		archived: !!(sessionId && $archivedChats.has(sessionId)),
		subagent: !!(sessionId && $subagentChats.has(sessionId)),
		assistantHome: assistantMode
	});
	const visible = $derived(noProjectPillVisible(attachState));
	const blockedReason = $derived(attachBlockedReason(attachState));
	const agentName = $derived($profiles.find((p) => p.id === $activeSession.profile)?.name ?? null);
</script>

{#if visible && sessionId}
	<button
		type="button"
		class="np-pill"
		class:compact
		aria-haspopup="dialog"
		aria-expanded={open}
		aria-label="No project. Attach a project to this session"
		title="No project — attach one to get a checkout and git"
		onclick={() => (open = true)}
	>
		<FolderPlus size={compact ? 18 : 14} strokeWidth={1.8} class="np-icon" aria-hidden="true" />
		{#if !compact}<span>No project</span>{/if}
	</button>
	<AttachProjectSheet {open} {sessionId} {agentName} {blockedReason} onClose={() => (open = false)} />
{/if}

<style>
	.np-pill {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 28px;
		padding: 0 11px 0 9px;
		border: 1px dashed var(--border-strong);
		border-radius: 999px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition: border-color 150ms ease, color 150ms ease, background 150ms ease;
	}
	.np-pill :global(.np-icon) { color: var(--subtle-foreground); transition: color 150ms ease; }
	.np-pill:hover, .np-pill[aria-expanded='true'] {
		border-style: solid;
		border-color: color-mix(in oklch, var(--primary) 55%, var(--border));
		color: var(--foreground);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}
	.np-pill:hover :global(.np-icon), .np-pill[aria-expanded='true'] :global(.np-icon) { color: var(--primary); }
	.np-pill:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.np-pill.compact { width: 44px; height: 44px; padding: 0; justify-content: center; border: none; border-radius: 12px; }
	@media (prefers-reduced-motion: reduce) {
		.np-pill, .np-pill :global(.np-icon) { transition: none; }
	}
</style>
