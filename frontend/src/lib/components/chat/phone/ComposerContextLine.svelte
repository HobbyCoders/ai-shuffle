<script lang="ts">
	/**
	 * The one line under the phone composer: where this chat runs, in words.
	 * "Dev sandbox · feature/second" with a project, "Dev agent · Local" without.
	 * Tapping it opens Chat controls, which hold the selectors themselves. Under
	 * a new chat the line is the `New session` region until the sheet takes over.
	 */
	import { ChevronDown } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, profiles, projects } from '$lib/stores/app-data.svelte';
	import { gitStatus } from '$lib/stores/git.svelte';
	import { refreshWorkerPresence, workerInstalls } from '$lib/stores/worker-presence';
	import { selfWorkerName, workerDisplayName } from '$lib/components/ui/worker-name';
	import { composerContextLine } from './context-line';

	interface Props {
		/** A new chat, before its first message. */
		newChat: boolean;
		/** Chat controls are open, so the sheet is the region now. */
		sheetOpen: boolean;
		onOpen: () => void;
	}

	let { newChat, sheetOpen, onOpen }: Props = $props();

	const parts = $derived(
		composerContextLine({
			projectName:
				$projects.find((p) => p.id === $activeSession.project)?.name ??
				$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
				null,
			branch:
				$activeSession.selectedWorktreeBranch ||
				$activeSession.worktreeBranch ||
				($activeSession.project ? ($gitStatus?.current_branch ?? null) : null),
			agentName: $profiles.find((p) => p.id === $activeSession.profile)?.name ?? null,
			workerName: $activeSession.deviceIntent?.name
				? workerDisplayName($activeSession.deviceIntent.name)
				: selfWorkerName($workerInstalls)
		})
	);
	const label = $derived(parts.join(' · '));

	// The list names this worker. The new chat's picker that used to load it now
	// lives in the sheet, which mounts when opened.
	$effect(() => {
		if (!$workerInstalls) void refreshWorkerPresence().catch(() => {});
	});
</script>

<section class="composer-context-line" aria-label={newChat && !sheetOpen ? 'New session' : undefined}>
	<button
		type="button"
		class="ccl-btn"
		aria-haspopup="dialog"
		aria-expanded={sheetOpen}
		aria-label={`Chat context: ${label}. Open chat controls`}
		onclick={onOpen}
	>
		{#each parts as part, index (index)}
			{#if index > 0}<span class="ccl-sep" aria-hidden="true">·</span>{/if}
			<span class="ccl-part" class:ccl-last={index === parts.length - 1}>{part}</span>
		{/each}
		<ChevronDown size={14} strokeWidth={2} aria-hidden="true" class="ccl-chev" />
	</button>
</section>

<style>
	.composer-context-line {
		display: flex;
		justify-content: center;
		min-width: 0;
		margin-top: 4px;
	}
	/* 44px tall to tap; the text itself is a 13px line. */
	.ccl-btn {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 5px;
		max-width: 100%;
		min-width: 0;
		min-height: 44px;
		margin-block: -6px;
		padding: 0 10px;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 13px;
		line-height: 1.3;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
		transition: color 0.15s ease;
	}
	@media (hover: hover) {
		.ccl-btn:hover { color: var(--foreground); }
	}
	.ccl-btn:active { color: var(--foreground); }
	.ccl-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	/* Whole names first. Only a name too long for the line gives way, the second
	   one before the first, and it ellipsizes at its end rather than mid-word. */
	.ccl-part { flex: 0 0 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ccl-part:first-child { flex-shrink: 1; }
	.ccl-part.ccl-last { flex: 0 10 auto; }
	.ccl-sep { flex: none; opacity: 0.7; }
	.ccl-btn :global(.ccl-chev) { flex: none; opacity: 0.8; }
</style>
