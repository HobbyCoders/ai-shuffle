<script lang="ts">
	/**
	 * The one line under the phone composer: where this chat runs, in words.
	 * In a chat, "Dev sandbox · feature/second" with a project and "Dev agent ·
	 * Local" without. On a new chat the header names the worker, so the line is
	 * "Dev agent · Dev sandbox" or "Dev agent · No project".
	 * Each name is its own button: a tap opens Chat controls straight on that
	 * name's selector (the agent list, the project picker, Runs on), so changing
	 * one takes a single tap. Controls, the line's last item, opens (or closes)
	 * the full panel.
	 * Under a new chat the line is the `New session` region until the sheet takes over.
	 */
	import { ChevronDown, SlidersHorizontal } from 'lucide-svelte';
	import { activeSession } from '$lib/stores/active-session';
	import { clusterProjects, profiles, projects, sessions } from '$lib/stores/app-data.svelte';
	import { gitStatus } from '$lib/stores/git.svelte';
	import { refreshWorkerPresence, workerInstalls } from '$lib/stores/worker-presence';
	import { selfWorkerName, workerDisplayName } from '$lib/components/ui/worker-name';
	import type { ComposerSetupRow } from '../composer-shell';
	import { composerContextLine } from './context-line';

	interface Props {
		/** A new chat, before its first message. */
		newChat: boolean;
		/** Chat controls are open, so the sheet is the region now. */
		sheetOpen: boolean;
		/** Open Chat controls on the selector for the tapped name. */
		onOpen: (row: ComposerSetupRow) => void;
		/** The line's Controls item: open Chat controls, or close them when open. */
		onToggleControls?: () => void;
		/** The panel's DOM id, for the Controls item's `aria-controls`. */
		panelId?: string;
	}

	let { newChat, sheetOpen, onOpen, onToggleControls, panelId }: Props = $props();

	/** What each name is, for its button's accessible name. */
	const ROW_LABELS: Record<ComposerSetupRow, string> = {
		agent: 'Agent',
		project: 'Project',
		branch: 'Branch',
		worker: 'Runs on'
	};

	const selfName = $derived(selfWorkerName($workerInstalls));
	// Durable ownership wins over both the connected device and stale compose intent.
	const sessionRow = $derived($sessions.find((s) => s.id === $activeSession.sessionId));
	const ownerName = $derived(sessionRow?.device_name ?? $activeSession.ownerDeviceName ?? null);
	const intentName = $derived(
		$activeSession.deviceIntent?.name ? workerDisplayName($activeSession.deviceIntent.name) : null
	);
	const parts = $derived(
		composerContextLine({
			newChat,
			projectName:
				$projects.find((p) => p.id === $activeSession.project)?.name ??
				$clusterProjects.find((p) => p.project_v2_id === $activeSession.projectV2Id)?.name ??
				null,
			branch:
				$activeSession.selectedWorktreeBranch ||
				$activeSession.worktreeBranch ||
				($activeSession.project ? ($gitStatus?.current_branch ?? null) : null),
			agentName: $profiles.find((p) => p.id === $activeSession.profile)?.name ?? null,
			workerName: $activeSession.sessionId ? (ownerName ? workerDisplayName(ownerName) : selfName) : intentName ?? selfName,
			pickedWorkerName: !$activeSession.sessionId && intentName !== selfName ? intentName : null
		})
	);
	const label = $derived(parts.map((part) => part.text).join(' · '));

	// The list names this worker. The new chat's picker that used to load it now
	// lives in the sheet, which mounts when opened.
	$effect(() => {
		if (!$workerInstalls) void refreshWorkerPresence().catch(() => {});
	});
</script>

<section class="composer-context-line" aria-label={newChat && !sheetOpen ? 'New chat' : undefined}>
	<div class="ccl-row" role="group" aria-label={`Chat context: ${label}`}>
		{#each parts as part, index (part.row)}
			{#if index > 0}<span class="ccl-sep" aria-hidden="true">·</span>{/if}
			<button
				type="button"
				class="ccl-btn"
				class:ccl-last={index === parts.length - 1}
				data-row={part.row}
				aria-haspopup="dialog"
				aria-label={`${ROW_LABELS[part.row]}: ${part.text}`}
				onclick={() => onOpen(part.row)}
			>
				<span class="ccl-part">{part.text}</span>
				<ChevronDown size={12} strokeWidth={2} aria-hidden="true" class="ccl-chev" />
			</button>
		{/each}
	</div>
	{#if onToggleControls}
		<span class="ccl-sep" aria-hidden="true">·</span>
		<button
			type="button"
			class="ccl-controls"
			data-controls-trigger
			aria-haspopup="dialog"
			aria-controls={panelId}
			aria-expanded={sheetOpen}
			aria-label="All chat controls"
			onclick={onToggleControls}
		>
			<SlidersHorizontal size={14} strokeWidth={2} aria-hidden="true" />
			<span>Controls</span>
		</button>
	{/if}
</section>

<style>
	.composer-context-line {
		display: flex;
		align-items: center;
		justify-content: center;
		min-width: 0;
		padding: 0 4px;
		margin-top: 4px;
	}
	.ccl-row {
		display: flex;
		flex: 0 1 auto;
		align-items: center;
		justify-content: center;
		max-width: 100%;
		min-width: 0;
	}
	/* Controls: the line's last item, never shortened. A name gives way first. */
	.ccl-controls {
		display: inline-flex;
		flex: none;
		align-items: center;
		gap: 5px;
		min-width: 44px;
		min-height: 44px;
		margin-block: -6px;
		padding: 0 6px;
		border: 0;
		border-radius: var(--radius-control);
		background: transparent;
		color: var(--foreground);
		font: inherit;
		font-size: 13px;
		line-height: 1.3;
		white-space: nowrap;
		cursor: pointer;
		-webkit-tap-highlight-color: transparent;
	}
	.ccl-controls :global(svg) { flex: none; opacity: 0.85; }
	@media (hover: hover) {
		.ccl-controls:hover { background: var(--hover-overlay); }
	}
	.ccl-controls[aria-expanded='true'] { background: var(--active-overlay); }
	.ccl-controls:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	/* Each name is its own 44px-tall tap target; the text itself is a 13px line. */
	.ccl-btn {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		flex: 0 1 auto;
		min-width: 44px;
		min-height: 44px;
		margin-block: -6px;
		padding: 0 6px;
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
	/* Whole names first. Only a name too long for the line gives way, the last
	   one before the others, and it ellipsizes at its end rather than mid-word. */
	.ccl-btn.ccl-last { flex: 0 10 auto; }
	@media (hover: hover) {
		.ccl-btn:hover { color: var(--foreground); }
	}
	.ccl-btn:active { color: var(--foreground); }
	.ccl-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.ccl-part { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.ccl-sep { flex: none; opacity: 0.7; color: var(--muted-foreground); font-size: 13px; }
	.ccl-btn :global(.ccl-chev) { flex: none; opacity: 0.7; }
</style>
