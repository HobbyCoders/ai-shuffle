<script lang="ts">
	/**
	 * Usage limits (#1831): resume by itself once after a provider's usage
	 * limit resets, and compact the chat first. One pair of switches for every
	 * provider. Compact only means something while resume is on, so it is
	 * dimmed and disabled otherwise.
	 */
	import './editors/editors.css';
	import { onMount } from 'svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import { LimitResume, LIMIT_RESUME_DEFAULTS } from './limit-resume.svelte';

	const model = new LimitResume();
	const access = getSettingsAccess();
	const s = $derived(model.switches);
	const locked = $derived(model.busy || access.readOnly || !model.settings);

	onMount(() => void model.load());
</script>

<RowGroup title="Usage limits">
	<SettingRow
		id="general.limitResume"
		value={s.resume_on_reset}
		onReset={() => model.save({ resume_on_reset: LIMIT_RESUME_DEFAULTS.resume_on_reset })}
	>
		<Switch
			checked={s.resume_on_reset}
			disabled={locked}
			label="Resume when the limit resets"
			onchange={(v) => model.save({ resume_on_reset: v })}
		/>
	</SettingRow>
	<SettingRow
		id="general.limitCompact"
		dim={!s.resume_on_reset}
		value={s.compact_before_resume}
		onReset={() => model.save({ compact_before_resume: LIMIT_RESUME_DEFAULTS.compact_before_resume })}
	>
		<Switch
			checked={s.compact_before_resume}
			disabled={locked || !s.resume_on_reset}
			label="Compact before resuming"
			onchange={(v) => model.save({ compact_before_resume: v })}
		/>
	</SettingRow>
</RowGroup>
{#if model.loadError}<p class="st-msg err" role="alert">{model.loadError}</p>{/if}
{#if model.saveError}<p class="st-msg err" role="alert">{model.saveError}</p>{/if}
