<script lang="ts">
	/**
	 * NewChatSetupCallout — under the new-session sentence, when the picked
	 * project is not on the worker the chat will run on:
	 *
	 *   idle     "<project> isn't on <worker> yet. We'll set it up there
	 *            first, then send your message."  (Send reads "Set up & send")
	 *   running  "Setting up on <worker>… 40%" — the message is held and sends
	 *            on its own once the worker is ready. Cancel stops it.
	 *   failed   the reason, the fix for its code, and that the draft is kept.
	 *
	 * The hold itself lives in ChatInput (holdForProjectSetup); this only says
	 * what is happening. Theme tokens only.
	 */
	import { Loader2, CloudDownload, AlertOctagon } from 'lucide-svelte';
	import { cancelProjectSetup } from '$lib/api/project-workers';
	import { loadProjectsOverview, projectsOverview } from '$lib/stores/projects-overview';
	import { activeSession } from '$lib/stores/active-session';
	import { layout } from '$lib/stores/layout.svelte';
	import { newChatSetup, newChatSetupNeed } from '$lib/stores/project-setup';
	import { errorAction } from '$lib/components/deck/cards/project-workers-view';

	const need = $derived($newChatSetupNeed);
	const phase = $derived($newChatSetup);
	const needKey = $derived(need ? `${need.projectV2Id}:${need.installId}` : null);
	// A failure belongs to the project and worker it was for.
	const failed = $derived(phase.phase === 'failed' && phase.key === needKey ? phase : null);
	const running = $derived(phase.phase === 'running' ? phase : null);
	const action = $derived(failed && need ? errorAction(failed.code, need.workerName) : null);
	const isSelfTarget = $derived.by(() => {
		if (!need) return false;
		const row = $projectsOverview.find((r) => r.project_v2_id === need.projectV2Id);
		return !!row?.workers.find((w) => w.install_id === need.installId)?.is_self;
	});

	// The need reads the cross-worker overview: load it when the chat may run
	// somewhere without the project (another worker, or a project that is
	// not on this one).
	const watchKey = $derived.by(() => {
		const s = $activeSession;
		if (s.sessionId || (!s.project && !s.projectV2Id)) return null;
		if (!s.deviceIntent && s.project) return null;
		return `${s.project}|${s.projectV2Id}|${s.deviceIntent?.id ?? ''}`;
	});
	$effect(() => {
		if (watchKey) void loadProjectsOverview();
	});

	// Picking another project or worker drops a stale failure.
	$effect(() => {
		if (phase.phase === 'failed' && phase.key !== needKey) newChatSetup.clearFailure();
	});

	async function cancel() {
		const opId = $projectsOverview
			.find((r) => r.project_v2_id === need?.projectV2Id)
			?.workers.find((w) => w.install_id === need?.installId)?.setup?.op_id;
		newChatSetup.abandon();
		if (opId) await cancelProjectSetup(opId).catch(() => {});
	}

	function openGitHubSettings() {
		layout.openFeaturePage('settings', 'category=integrations&item=github');
	}
</script>

{#if running || need}
	<div
		class="ncs"
		class:ncs--busy={!!running}
		class:ncs--failed={!!failed}
		role={failed ? 'alert' : 'status'}
		aria-live="polite"
		data-ui-setup={running ? 'running' : failed ? 'failed' : 'needed'}
	>
		<span class="ncs-ic" aria-hidden="true">
			{#if running}<Loader2 size={16} class="ncs-spin" />{:else if failed}<AlertOctagon size={16} />{:else}<CloudDownload size={16} />{/if}
		</span>
		<div class="ncs-body">
			{#if running}
				<p><b>Setting up on {running.workerName}…</b>{#if typeof running.progress === 'number'} {Math.round(running.progress)}%{/if}</p>
				<p class="ncs-sub">Your message is held and sends on its own once {running.workerName} is ready.</p>
			{:else if failed && need}
				<p><b>Couldn't set up {need.projectName} on {need.workerName}.</b> {failed.reason}</p>
				<p class="ncs-sub">Your message is still in the box. Fix that, then press Set up &amp; send again.</p>
			{:else if need}
				<p><b>{need.projectName} isn't on {need.workerName} yet.</b> We'll set it up there first, then send your message.</p>
				<p class="ncs-sub">
					{need.kind === 'folder'
						? `Creates an empty ${need.projectName} folder there; files aren't copied.`
						: `${need.workerName} clones it with its own GitHub sign-in.`}
				</p>
			{/if}
		</div>
		{#if running}
			<button type="button" class="ncs-btn" onclick={cancel}>Cancel</button>
		{:else if action?.kind === 'github' && isSelfTarget}
			<button type="button" class="ncs-btn" onclick={openGitHubSettings}>{action.label}</button>
		{/if}
	</div>
{/if}

<style>
	.ncs {
		display: flex;
		align-items: flex-start;
		gap: 10px;
		width: 100%;
		max-width: 40rem;
		margin: 14px auto 0;
		padding: 11px 13px;
		border: 1px solid color-mix(in oklch, var(--primary) 35%, transparent);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
		border-radius: calc(var(--radius, 0.75rem) - 1px);
		text-align: left;
		color: var(--foreground);
		font-size: 0.8125rem;
		line-height: 1.45;
		animation: ncs-in 220ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.ncs--failed {
		border-color: color-mix(in oklch, var(--destructive) 40%, transparent);
		background: color-mix(in oklch, var(--destructive) 7%, transparent);
	}
	.ncs-ic { flex: none; display: grid; place-items: center; margin-top: 1px; color: var(--primary); }
	.ncs--failed .ncs-ic { color: var(--destructive); }
	.ncs-body { flex: 1; min-width: 0; }
	.ncs-body p { margin: 0; }
	.ncs-sub { margin-top: 2px !important; color: var(--muted-foreground); font-size: 0.75rem; }
	.ncs-btn {
		flex: none;
		align-self: center;
		height: 30px;
		padding: 0 11px;
		border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 3px);
		background: var(--card);
		color: var(--foreground);
		font: inherit;
		font-size: 0.78rem;
		font-weight: 600;
		cursor: pointer;
		white-space: nowrap;
		transition: background-color 120ms ease;
	}
	.ncs-btn:hover { background: var(--hover-overlay); }
	.ncs-btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	:global(.ncs-spin) { animation: ncs-spin 0.9s linear infinite; }
	@keyframes ncs-spin { to { transform: rotate(360deg); } }
	@keyframes ncs-in { from { opacity: 0; transform: translateY(-3px); } }
	@media (max-width: 480px) {
		.ncs { flex-wrap: wrap; }
		.ncs-btn { margin-left: 26px; }
	}
	@media (prefers-reduced-motion: reduce) {
		.ncs { animation: none; }
		:global(.ncs-spin) { animation: none; }
	}
</style>
