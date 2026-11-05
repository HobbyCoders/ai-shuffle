<script lang="ts">
	/** Redacted resident identity and nested preparation timings for the connected worker. */
	import { ChevronDown } from 'lucide-svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import type { UpdatesState, UpdateRuntimeStatus } from '../updates-state.svelte';

	let { updates, status, workerName }: { updates?: UpdatesState; status?: UpdateRuntimeStatus; workerName?: string } = $props();
	let open = $state(false);
	let confirming = $state(false);
	let cancel = $state<HTMLButtonElement | null>(null);
	let refreshButton = $state<HTMLButtonElement | null>(null);
	const uid = $props.id();
	const runtime = $derived(updates?.runtime ?? status);
	const updater = $derived(runtime?.updater);
	const diagnostics = $derived(runtime?.diagnostics);
	const refreshMode = $derived(!updater?.revision && (updater?.refresh === 'current' || updater?.refresh === 'available') ? 'unknown' : updater?.refresh ?? 'unknown');
	const canOfferRefresh = $derived(!!updates && refreshMode === 'available' && updater?.capabilities.includes('refresh-updater-v1'));
	const refreshText = $derived.by(() => {
		switch (refreshMode) {
			case 'current': return 'The running updater matches the active app revision.';
			case 'available': return 'A newer updater can be loaded with a supported worker restart. Updating the app alone does not replace it.';
			case 'container_restart_required': return 'A supported container restart is required to load the newer updater. An app-only restart is not enough.';
			case 'image_update_required': return 'This updater is managed by the container image. Changing it requires an operator-managed image transition; this does not mean the app or image is out of date.';
			case 'disabled': return 'Updater refresh is disabled on this worker.';
			default: return 'This worker does not report its running updater revision. App version alone cannot confirm whether the updater is current. Older supervisors may need an operator-approved restart.';
		}
	});

	const stages: Record<string, string> = {
		command: 'Build command', source_download: 'Source download', source_extract: 'Source extraction', source_verify: 'Source verification',
		runtimes: 'Runtime preparation', assembly: 'Update assembly', python_dependencies: 'Python dependencies', python_cache: 'Python cache',
		fluid: 'Fluid installation', prebuilt_ui: 'Prebuilt UI', prepare_files: 'File preparation', frontend_cache: 'UI cache',
		frontend_dependencies: 'UI dependencies', frontend_build: 'UI build', tools_cache: 'Tools cache', tools_dependencies: 'Tools dependencies',
		tools_build: 'Tools build', browser_mcp_cache: 'Browser cache', browser_mcp_dependencies: 'Browser dependencies', browser_mcp_build: 'Browser build',
		desktop_cache: 'Desktop cache', desktop_dependencies: 'Desktop dependencies', desktop_build: 'Desktop build'
	};
	const outcomes: Record<string, string> = {
		completed: 'Completed', failed: 'Failed', hit: 'Reused verified cache', miss: 'No reusable verified entry', fallback: 'Fallback to source build', disabled: 'Not offered or reuse disabled'
	};

	// Never render arbitrary producer strings, commands, paths or endpoints here.
	function revision(value: string | null | undefined): string {
		return value && /^[a-f0-9]{7,40}$/.test(value) ? value.slice(0, 12) : 'Unknown';
	}
	function elapsed(ms: number): string {
		if (!Number.isFinite(ms) || ms < 0) return 'Unknown';
		if (ms < 1000) return `${Math.round(ms)} ms`;
		if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
		return `${Math.floor(ms / 60_000)} min ${Math.floor((ms % 60_000) / 1000)} s`;
	}
	function dismiss() {
		confirming = false;
		refreshButton?.focus();
	}
	$effect(() => {
		if (confirming) cancel?.focus({ preventScroll: true });
	});
</script>

{#snippet details()}
	<section class="ur" id="{uid}-details" aria-label={workerName ? `${workerName} update details` : 'Connected worker update details'}>
		{#if updates?.status === 'loading'}
			<p class="st-msg" role="status"><span class="st-spin" aria-hidden="true"></span> Loading update details…</p>
		{:else if updates?.status === 'error' || updates?.status === 'forbidden'}
			<p class="st-msg">{updates?.status === 'forbidden' ? 'Update details are available after approval.' : 'Could not load update details.'}</p>
			{#if updates?.status === 'error'}<button type="button" class="st-btn" onclick={() => void updates?.refresh()}>Try again</button>{/if}
		{:else}
			<p class="ur-scope">{workerName ? 'This worker’s report' : 'Connected worker only'} · app and updater can run different revisions</p>
			{#if updates?.runtimeStale}<p class="st-msg" role="status">Worker status is temporarily unavailable. Showing the last report; refresh is paused.</p>{/if}
			<dl class="ur-identities">
				<div><dt>Running app</dt><dd>{revision(runtime?.running_version)}</dd></div>
				<div><dt>Running updater</dt><dd>{revision(updater?.revision)}</dd></div>
			</dl>
			<p class="ur-explanation">{refreshText}</p>
			{#if (updates?.policy?.allow_updates ?? runtime?.allow_updates) === false}<p class="st-msg">Updates are paused. No updater refresh will be requested.</p>{/if}
			{#if canOfferRefresh}
				{#if runtime?.request_pending || runtime?.install_pending}<p class="st-msg">An update action is pending. Let it finish before refreshing the updater.</p>{/if}
				<div class="ur-actions">
					<button type="button" class="st-btn" bind:this={refreshButton} disabled={!updates?.canRefreshUpdater} aria-busy={updates?.refreshingUpdater} onclick={() => confirming = true}>
						{updates?.refreshingUpdater ? 'Requesting…' : updates?.updaterQueued ? 'Refresh requested' : 'Refresh updater…'}
					</button>
				</div>
			{/if}
			{#if confirming}
				<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
				<div class="ur-confirm" role="group" aria-labelledby="{uid}-confirm" onkeydown={(event) => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); dismiss(); } }}>
					<p id="{uid}-confirm"><strong>Restart the connected worker to refresh its updater?</strong></p>
					<p>This affects this worker only, even when “Update every worker” is on. The worker checks for active work, update policy and a verified compatible source before it restarts. It will not stop running work for you.</p>
					<div class="ur-actions">
						<button type="button" class="st-btn ghost" bind:this={cancel} onclick={dismiss}>Cancel</button>
						<button type="button" class="st-btn warn" disabled={!updates?.canRefreshUpdater} onclick={() => { confirming = false; void updates?.refreshUpdater(); }}>Refresh and restart worker</button>
					</div>
				</div>
			{/if}
			{#if updates?.updaterNotice}<p class="st-msg" role="status">{updates?.updaterNotice}</p>{/if}
			{#if updates?.updaterError}<p class="st-msg err" role="alert">{updates?.updaterError}</p>{/if}
			<div class="ur-timings">
				<h3>Latest preparation</h3>
				{#if diagnostics}
					<div class="ur-total"><span class="ur-revision">Revision {revision(diagnostics.revision)}</span><strong>{elapsed(diagnostics.total_elapsed_ms)} total</strong></div>
					<p class="ur-explanation">Elapsed wall time from the latest report. Stages can overlap or include other stages; do not add them together. This is not an installation confirmation.</p>
					{#if diagnostics.stages.length}
						<ul class="ur-stages" aria-label="Preparation stages">
							{#each diagnostics.stages as stage}
								<li class:failed={stage.outcome === 'failed'}>
									<div><span>{stages[stage.stage] ?? 'Update stage'}</span><span class="ur-outcome">{stage.stage === 'prebuilt_ui' && stage.outcome === 'hit' ? 'Used signed prebuilt UI' : outcomes[stage.outcome] ?? 'Unknown outcome'}</span>
										{#if stage.stage === 'prebuilt_ui' && stage.outcome === 'fallback'}<span class="ur-reason">Prebuilt UI not reused; using a source build instead.</span>{/if}
									</div>
									<span class="ur-duration">{elapsed(stage.elapsed_ms)}</span>
								</li>
							{/each}
						</ul>
					{:else}<p class="st-msg">No completed stages reported yet.</p>{/if}
				{:else}<p class="st-msg">No update timings reported yet.</p>{/if}
			</div>
		{/if}
	</section>
{/snippet}

<SettingRow label="Update details" hint={workerName ? 'Read-only worker report' : 'Connected worker only'} expanded={open ? details : undefined}>
	<button type="button" class="st-btn ur-toggle" aria-label={workerName ? `Update details for ${workerName}` : 'Update details'} aria-expanded={open} aria-controls={open ? `${uid}-details` : undefined} onclick={() => { open = !open; if (!open) confirming = false; }}>
		{open ? 'Hide details' : 'View details'}<ChevronDown size={14} class={open ? 'ur-rotated' : ''} />
	</button>
</SettingRow>

<style>
	.ur { min-width: 0; padding: 4px 2px; animation: ur-in 0.2s ease-out; }
	.ur-scope { margin: 0 0 12px; font-size: 12px; color: var(--st-mfg); }
	.ur-identities { display: grid; grid-template-columns: 1fr 1fr; margin: 0; border: 1px solid var(--st-border); border-radius: var(--st-radius-md); overflow: hidden; }
	.ur-identities > div { min-width: 0; padding: 12px; background: var(--st-muted); }
	.ur-identities > div + div { border-left: 1px solid var(--st-border); }
	dt { font-size: 12px; color: var(--st-mfg); }
	dd { font-family: var(--st-mono); margin: 6px 0 0; font-size: 13px; overflow-wrap: anywhere; }
	.ur-explanation { margin: 10px 0; color: var(--st-mfg); font-size: 12.5px; line-height: 1.6; }
	.ur-actions { display: flex; justify-content: flex-end; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
	.ur-confirm { padding: 12px; margin: 12px 0; border-radius: var(--st-radius-md); background: var(--st-warning-soft); border-left: 3px solid var(--st-warning); }
	.ur-confirm p { margin: 0 0 8px; font-size: 13px; line-height: 1.6; }
	.ur-timings { border-top: 1px solid var(--st-border); margin-top: 16px; padding-top: 16px; }
	h3 { margin: 0 0 10px; font-size: 13px; font-weight: 550; }
	.ur-total { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; font-size: 12.5px; }
	.ur-revision { color: var(--st-mfg); font-family: var(--st-mono); font-size: 11.5px; }
	.ur-stages { padding: 0; margin: 12px 0 0; list-style: none; border: 1px solid var(--st-border); border-radius: var(--st-radius-md); overflow: hidden; }
	.ur-stages li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; padding: 10px 12px; font-size: 12.5px; }
	.ur-stages li + li { border-top: 1px solid var(--st-border); }
	.ur-outcome, .ur-reason { display: block; margin-top: 3px; color: var(--st-mfg); font-size: 12px; line-height: 1.5; }
	.ur-duration { font-family: var(--st-mono); font-size: 11.5px; font-variant-numeric: tabular-nums; }
	.failed .ur-outcome { color: var(--st-destructive); }
	.ur-toggle :global(svg) { transition: transform 0.2s ease-out; }
	.ur-toggle :global(.ur-rotated) { transform: rotate(180deg); }
	@keyframes ur-in { from { opacity: 0; transform: translateY(-2px); } }
	@container settings (max-width: 600px) {
		.ur-identities { grid-template-columns: minmax(0, 1fr); }
		.ur-identities > div + div { border-left: 0; border-top: 1px solid var(--st-border); }
		.ur :global(button), .ur-toggle { min-height: 44px; }
	}
	@media (prefers-reduced-motion: reduce) { .ur { animation: none; } .ur-toggle :global(svg) { transition: none; } }
</style>
