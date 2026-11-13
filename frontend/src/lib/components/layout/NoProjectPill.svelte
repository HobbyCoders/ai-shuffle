<script lang="ts">
	/**
	 * NoProjectPill — the mark for a running session without a project
	 * (#371 step 4). Pressing it opens the attach sheet, which says what
	 * attaching adds and that it is one-way.
	 *
	 * On the desktop it is a chip in the row under the composer, beside the
	 * agent and worker (ContextPillBar). The phone composer has no such row, so
	 * the phone top bar keeps an icon-sized button (`compact`).
	 */
	import { ChevronDown, FolderClosed, FolderPlus } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { profiles, sessions } from '$lib/stores/app-data.svelte';
	import { archivedChats } from '$lib/stores/archived-chats';
	import { subagentChats } from '$lib/stores/subagent-chats';
	import { attachBlockedReason, noProjectPillVisible, type AttachState } from '$lib/components/chat/session-scope';
	import AttachProjectSheet from './AttachProjectSheet.svelte';

	interface Props {
		/** `chip`: the composer's context row. `compact`: the phone top bar's icon. */
		variant?: 'chip' | 'compact';
	}

	let { variant = 'chip' }: Props = $props();

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
		hidden: !!row?.hidden
	});
	const visible = $derived(noProjectPillVisible(attachState));
	const blockedReason = $derived(attachBlockedReason(attachState));
	const agentName = $derived($profiles.find((p) => p.id === $activeSession.profile)?.name ?? null);
</script>

{#if visible && sessionId}
	{#if variant === 'compact'}
		<button
			type="button"
			class="np-pill"
			aria-haspopup="dialog"
			aria-expanded={open}
			aria-label="No project. Attach a project to this session"
			title="No project — attach one to get a checkout and git"
			onclick={() => (open = true)}
		>
			<FolderPlus size={18} strokeWidth={1.8} aria-hidden="true" />
		</button>
	{:else}
		<!-- The look is `.ctx-chip` (app.css), like the agent and worker beside it,
		     and the same folder and word as the new-chat row's "No project". -->
		<button
			type="button"
			class="ctx-chip np-chip"
			class:open
			aria-haspopup="dialog"
			aria-expanded={open}
			aria-label="No project. Attach a project to this session"
			title="No project — attach one to get a checkout and git"
			onclick={() => (open = true)}
		>
			<FolderClosed size={14} strokeWidth={1.75} aria-hidden="true" />
			<span class="ctx-chip-label">No project</span>
			<ChevronDown size={12} strokeWidth={1.5} aria-hidden="true" />
		</button>
	{/if}
	<AttachProjectSheet {open} {sessionId} {agentName} {blockedReason} onClose={() => (open = false)} />
{/if}

<style>
	.np-pill {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		padding: 0;
		border: none;
		border-radius: 12px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 150ms ease, background 150ms ease;
	}
	.np-pill :global(svg) { color: var(--subtle-foreground); transition: color 150ms ease; }
	.np-pill:hover, .np-pill[aria-expanded='true'] {
		color: var(--foreground);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
	}
	.np-pill:hover :global(svg), .np-pill[aria-expanded='true'] :global(svg) { color: var(--primary); }
	.np-pill:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	/* The chip keeps its word whole: it is short, and the row's names shrink first. */
	.np-chip { flex-shrink: 0; }
	@media (prefers-reduced-motion: reduce) {
		.np-pill, .np-pill :global(svg) { transition: none; }
	}
</style>
