<script lang="ts">
	/**
	 * PR watch: what a watched pull request's agent may do and what it may
	 * cost. The watch itself polls GitHub in plain code; only these wakes use
	 * a model. One registry entry (entries/usage.ts) per `<SettingRow id>`.
	 */
	import './editors/editors.css';
	import { onMount } from 'svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import type { PrWatchMode } from '$lib/api/pr-watches';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import NumberField from '../rows/NumberField.svelte';
	import TextField from '../rows/TextField.svelte';
	import { PR_WATCH_BOUNDS as B, PR_WATCH_DEFAULTS as D, PrWatchSettingsModel, parseBots } from './pr-watch-settings.svelte';

	const model = new PrWatchSettingsModel();
	const access = getSettingsAccess();
	const v = $derived(model.values);
	const locked = $derived(model.busy || access.readOnly || !model.settings);
	const notifyOnly = $derived(v.mode === 'notify');

	const modes: { value: PrWatchMode; label: string; title: string }[] = [
		{ value: 'fix', label: 'Fix it', title: 'Push fixes and answer reviews; never merges' },
		{ value: 'triage', label: 'Triage', title: 'Assess and suggest; changes nothing on GitHub' },
		{ value: 'notify', label: 'Notify only', title: 'No model at all; the bell only' }
	];

	onMount(() => void model.load());
</script>

<RowGroup title="PR watch">
	<SettingRow id="usage.prWatchMode" value={v.mode} onReset={() => model.save({ mode: D.mode })}>
		<Segmented
			options={modes}
			value={v.mode}
			size="sm"
			disabled={locked}
			onchange={(mode) => model.save({ mode })}
		/>
	</SettingRow>
	<SettingRow
		id="usage.prWatchWakes"
		dim={notifyOnly}
		value={v.max_wakes_per_day}
		onReset={() => model.save({ max_wakes_per_day: D.max_wakes_per_day })}
	>
		<NumberField
			value={v.max_wakes_per_day}
			min={B.max_wakes_per_day[0]}
			max={B.max_wakes_per_day[1]}
			unit="per PR"
			disabled={locked || notifyOnly}
			onchange={(n) => model.save({ max_wakes_per_day: Math.round(n) })}
		/>
	</SettingRow>
	<SettingRow
		id="usage.prWatchSpend"
		dim={notifyOnly}
		value={v.max_usd_per_day}
		onReset={() => model.save({ max_usd_per_day: D.max_usd_per_day })}
	>
		<NumberField
			value={v.max_usd_per_day}
			min={B.max_usd_per_day[0]}
			max={B.max_usd_per_day[1]}
			step={0.5}
			unit="USD"
			disabled={locked || notifyOnly}
			onchange={(n) => model.save({ max_usd_per_day: Math.round(n * 100) / 100 })}
		/>
	</SettingRow>
	<SettingRow
		id="usage.prWatchRepeats"
		dim={notifyOnly}
		value={v.max_repeat_failures}
		onReset={() => model.save({ max_repeat_failures: D.max_repeat_failures })}
	>
		<NumberField
			value={v.max_repeat_failures}
			min={B.max_repeat_failures[0]}
			max={B.max_repeat_failures[1]}
			unit="times"
			disabled={locked || notifyOnly}
			onchange={(n) => model.save({ max_repeat_failures: Math.round(n) })}
		/>
	</SettingRow>
	<SettingRow id="usage.prWatchIdle" value={v.idle_days} onReset={() => model.save({ idle_days: D.idle_days })}>
		<NumberField
			value={v.idle_days}
			min={B.idle_days[0]}
			max={B.idle_days[1]}
			unit="days"
			disabled={locked}
			onchange={(n) => model.save({ idle_days: Math.round(n) })}
		/>
	</SettingRow>
	<SettingRow
		id="usage.prWatchBots"
		dim={notifyOnly}
		value={v.trusted_bots.join(', ')}
		onReset={() => model.save({ trusted_bots: D.trusted_bots })}
	>
		<TextField
			value={v.trusted_bots.join(', ')}
			placeholder="coderabbitai, copilot"
			maxlength={600}
			disabled={locked || notifyOnly}
			onchange={(text) => void model.save({ trusted_bots: parseBots(text) })}
		/>
	</SettingRow>
</RowGroup>
{#if model.loadError}<p class="st-msg err" role="alert">{model.loadError}</p>{/if}
{#if model.saveError}<p class="st-msg err" role="alert">{model.saveError}</p>{/if}
