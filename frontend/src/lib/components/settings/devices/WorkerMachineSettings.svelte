<script lang="ts">
	import { onMount } from 'svelte';
	import type { WorkerAction } from '$lib/api/worker-settings';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import SelectField from '../rows/SelectField.svelte';
	import Switch from '../rows/Switch.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { MachineSettingsModel, OUTCOME_TEXT, signInText } from './machine-settings.svelte';

	let { deviceId, self = false, showAvailability = true, onAvailabilityChange, model = new MachineSettingsModel() }: {
		deviceId?: string; self?: boolean; showAvailability?: boolean; onAvailabilityChange?: () => void; model?: MachineSettingsModel;
	} = $props();
	const access = getSettingsAccess();
	const worker = $derived(model.workers.find((w) => self ? w.is_self : w.device_id === deviceId));
	const cli = $derived(worker?.status?.claude);
	const channel = $derived(worker?.status?.release_channel);
	const busy = $derived(model.saving || access.readOnly || model.stale);
	const online = $derived(worker?.online && worker.outcome === 'ok');
	const installing = $derived(cli?.job?.state === 'running');
	const claudeSignIn = $derived([
		`${signInText(cli?.status_available ? cli.signed_in : null)} on ${worker?.device_name ?? ''}`,
		cli?.status_available && cli.signed_in ? cli.email : null,
		cli?.status_available && cli.signed_in ? cli.plan : null
	].filter(Boolean).join(' · '));
	let allAction = $state('claude-install');
	let confirmAll = $state(false);
	const allOptions = [
		{ value: 'claude-install', label: 'Install Claude CLI' },
		{ value: 'claude-update', label: 'Update Claude CLI' },
		{ value: 'keep', label: 'Keep Claude CLI installed' },
		{ value: 'no-keep', label: 'Don’t keep Claude CLI installed' },
		{ value: 'stable', label: 'Use stable releases' },
		{ value: 'nightly', label: 'Use nightly releases' },
		{ value: 'on', label: 'Worker stays on' },
		{ value: 'off', label: 'Worker may sleep' },
		{ value: 'default', label: 'Default stays-on setting' }
	];

	onMount(() => model.start());
	function act(action: WorkerAction) {
		return worker ? model.act(worker.device_id, action) : Promise.resolve(false);
	}
	function applyAll() {
		confirmAll = false;
		let action: WorkerAction;
		if (allAction === 'stable' || allAction === 'nightly') action = { operation: 'release-channel', channel: allAction };
		else if (allAction === 'keep' || allAction === 'no-keep') action = { operation: 'claude-keep-installed', keep_installed: allAction === 'keep' };
		else if (['on', 'off', 'default'].includes(allAction)) action = { operation: 'always-on', always_on: allAction === 'default' ? null : allAction === 'on' };
		else action = { operation: allAction as 'claude-install' | 'claude-update' };
		void model.act('all', action).then(() => {
			if (action.operation === 'always-on') onAvailabilityChange?.();
		});
	}
</script>

<section aria-label="Worker machine settings" class="machine-settings">
	<RowGroup title="Machine settings">
		{#if !worker}
			<p class="note" role="status">{model.loading ? 'Reading worker settings…' : 'Worker settings unavailable'}</p>
		{:else}
			{#if worker.outcome !== 'ok'}
				<p class="note" role="status">{worker.device_name}: {OUTCOME_TEXT[worker.reason] ?? 'Status unavailable'}</p>
			{/if}
			<SettingRow label="Claude CLI" hint={cli?.version ?? undefined}>
				<div class="controls">
					<span>{cli ? cli.installed ? 'Installed' : 'Not installed' : online ? 'Checking…' : 'Unavailable'}</span>
					<button type="button" class="st-btn sm" disabled={busy || !online || !cli?.can_install || installing || (cli.installed && !cli.installed_by_app)}
						onclick={() => act({ operation: cli?.installed ? 'claude-update' : 'claude-install' })}>
						{installing ? 'Working…' : cli?.installed ? 'Update Claude CLI' : 'Install Claude CLI'}
					</button>
				</div>
			</SettingRow>
			{#if cli?.installed && !cli.installed_by_app}<p class="note">This CLI is managed outside AI Shuffle.</p>{/if}
			{#if cli?.job && !installing}<p class="note" role="status">Claude CLI {cli.job.kind}: {cli.job.state}</p>{/if}
			<SettingRow label="Keep installed" help="Keep Claude CLI available after this worker restarts. Turning this off does not uninstall it.">
				<Switch checked={cli?.keep_installed ?? false} disabled={busy || !online || !cli || installing}
					onchange={(value) => act({ operation: 'claude-keep-installed', keep_installed: value })} />
			</SettingRow>
			<SettingRow label="Claude sign-in">
				<span class="status-text">{claudeSignIn}</span>
			</SettingRow>
			<p class="note">Sign-in and sign-out aren’t available here yet.</p>
			<SettingRow label="GitHub CLI sign-in">
				<span class="status-text">{signInText(worker.status?.github_signed_in)} on {worker.device_name}</span>
			</SettingRow>
			<SettingRow label="Release channel" hint={channel?.restart_required ? 'Restart required' : undefined}
				help="Each worker’s launcher checks platform support and safe returns to stable. A channel change does not install an app update.">
				{#if channel}
					<SelectField value={channel.channel} options={['stable', 'nightly']} disabled={busy || !online || !channel.editable}
						onchange={(value) => act({ operation: 'release-channel', channel: value as 'stable' | 'nightly' })} />
				{:else}<span>Unavailable</span>{/if}
			</SettingRow>
			{#if channel && !channel.editable}<p class="note">The release channel is controlled by this worker’s launcher or environment.</p>{/if}
			{#if showAvailability}
				<SettingRow label="Worker stays on" hint={worker.availability.always_on_override === null ? 'Launch default' : 'Set by you'}
					help="Watches prefer a worker that stays on. This shared preference can be changed even while the worker is offline.">
					<SelectField value={worker.availability.always_on_override === null ? 'default' : worker.availability.always_on_override ? 'on' : 'off'}
						options={[{ value: 'default', label: 'Use default' }, { value: 'on', label: 'Stays on' }, { value: 'off', label: 'May sleep' }]}
						disabled={busy || !worker.availability.editable}
						onchange={(value) => act({ operation: 'always-on', always_on: value === 'default' ? null : value === 'on' })} />
				</SettingRow>
			{/if}
		{/if}
	</RowGroup>
	<RowGroup title="All workers">
		<SettingRow label="Apply to all workers" help="Machine actions only run on online workers and are never queued. Stays-on preferences sync when workers reconnect.">
			<div class="controls">
				<SelectField value={allAction} options={allOptions} label="All workers action" disabled={busy} onchange={(value) => { allAction = value; confirmAll = false; }} />
				<button type="button" class="st-btn sm" disabled={busy || !model.workers.length} onclick={() => confirmAll = true}>Apply…</button>
			</div>
		</SettingRow>
		{#if confirmAll}
			<p class="note">{allOptions.find((o) => o.value === allAction)?.label} on all {model.workers.length} workers?</p>
			<div class="confirmation">
				<button type="button" class="st-btn sm" disabled={busy} onclick={applyAll}>Apply to all workers</button>
				<button type="button" class="st-btn ghost sm" onclick={() => confirmAll = false}>Cancel</button>
			</div>
		{/if}
		{#if model.results.length}
			<ul class="results" aria-label="Worker action results" aria-live="polite">
				{#each model.results as result (result.device_id)}
					<li>{result.device_name}: {OUTCOME_TEXT[result.reason] ?? 'Status unavailable'}</li>
				{/each}
			</ul>
		{/if}
	</RowGroup>
	{#if model.error}<InlineBanner tone="warn"><span role="alert">{model.error}</span></InlineBanner>{/if}
	<button type="button" class="st-btn ghost sm" disabled={model.loading} onclick={() => model.load()}>Refresh worker status</button>
</section>

<style>
	.machine-settings { min-width: 0; }
	.controls { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; min-width: 0; }
	.status-text { overflow-wrap: anywhere; text-align: right; }
	.note, .results { margin: 0; padding: 8px 12px; color: var(--st-mfg); font-size: 12px; overflow-wrap: anywhere; }
	.results { padding-left: 28px; }
	.confirmation { display: flex; flex-wrap: wrap; gap: 8px; padding: 8px 12px; }
</style>
