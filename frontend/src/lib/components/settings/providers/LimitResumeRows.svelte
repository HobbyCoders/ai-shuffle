<script lang="ts">
	/**
	 * A provider's usage-limit switches (#1831): resume by itself once after
	 * the limit resets, and compact the chat first. Compact only means
	 * something while resume is on, so it is dimmed and disabled otherwise.
	 */
	import { onMount } from 'svelte';
	import { getSettingsAccess } from '$lib/settings/access.svelte';
	import { LIMIT_COMPACT_HELP, LIMIT_RESUME_HELP } from '$lib/settings/entries/providers';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import { LimitResume, LIMIT_RESUME_DEFAULTS } from './limit-resume.svelte';

	interface Props {
		/** `claude` for Claude Code, else the model provider's id. */
		providerKey: string;
		/** Registry ids, for the rows Settings search can jump to. */
		ids?: { resume: string; compact: string };
	}

	let { providerKey, ids }: Props = $props();

	const model = new LimitResume();
	const access = getSettingsAccess();
	const s = $derived(model.switches(providerKey));
	const locked = $derived(model.busy || access.readOnly || !model.settings);

	onMount(() => void model.load());
</script>

<RowGroup title="Usage limit">
	<SettingRow
		id={ids?.resume}
		label="Resume when the limit resets"
		hint="Once, after the reset"
		help={LIMIT_RESUME_HELP}
		value={s.resume_on_reset}
		defaultValue={LIMIT_RESUME_DEFAULTS.resume_on_reset}
		onReset={() => model.save(providerKey, { resume_on_reset: LIMIT_RESUME_DEFAULTS.resume_on_reset })}
	>
		<Switch
			checked={s.resume_on_reset}
			disabled={locked}
			label="Resume when the limit resets"
			onchange={(v) => model.save(providerKey, { resume_on_reset: v })}
		/>
	</SettingRow>
	<SettingRow
		id={ids?.compact}
		label="Compact before resuming"
		hint="Recommended"
		help={LIMIT_COMPACT_HELP}
		dim={!s.resume_on_reset}
		value={s.compact_before_resume}
		defaultValue={LIMIT_RESUME_DEFAULTS.compact_before_resume}
		onReset={() => model.save(providerKey, { compact_before_resume: LIMIT_RESUME_DEFAULTS.compact_before_resume })}
	>
		<Switch
			checked={s.compact_before_resume}
			disabled={locked || !s.resume_on_reset}
			label="Compact before resuming"
			onchange={(v) => model.save(providerKey, { compact_before_resume: v })}
		/>
	</SettingRow>
</RowGroup>
{#if model.loadError}<p class="pv-msg err" role="alert">{model.loadError}</p>{/if}
{#if model.saveError}<p class="pv-msg err" role="alert">{model.saveError}</p>{/if}
