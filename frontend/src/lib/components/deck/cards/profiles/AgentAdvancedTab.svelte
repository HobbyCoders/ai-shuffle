<script lang="ts">
	/**
	 * The Advanced tab: per-turn model-call cap, workspace directories, setting
	 * sources (fluid's user/project/local scope filter) and reply streaming.
	 */
	import RowGroup from '$lib/components/settings/rows/RowGroup.svelte';
	import SettingRow from '$lib/components/settings/rows/SettingRow.svelte';
	import Switch from '$lib/components/settings/rows/Switch.svelte';
	import TextField from '$lib/components/settings/rows/TextField.svelte';
	import type { AgentForm } from './agentForm';

	interface Props {
		form: AgentForm;
		onFormChange: (field: keyof AgentForm, value: unknown) => void;
	}

	let { form, onFormChange }: Props = $props();

	// Stored as null when every scope is on (the default; the config key is
	// omitted); an explicit list otherwise, including [] for a fully
	// filesystem-isolated agent.
	const ALL_SOURCES = ['user', 'project', 'local'];
	const SOURCES = [
		{ id: 'user', label: 'User', hint: '~/.fluid instructions and skills.' },
		{ id: 'project', label: 'Project', hint: 'AGENTS.md and .fluid trees.' },
		{ id: 'local', label: 'Local', hint: 'Gitignored local overrides.' }
	];
	const selectedSources = $derived(form.setting_sources ?? ALL_SOURCES);

	function toggleSource(id: string) {
		const next = selectedSources.includes(id) ? selectedSources.filter((s) => s !== id) : [...selectedSources, id];
		// All three on = the default; store null so the config key is omitted.
		onFormChange('setting_sources', next.length === ALL_SOURCES.length ? null : next);
	}

	function setMaxTurns(raw: string) {
		const n = Number(raw.trim());
		onFormChange('max_turns', raw.trim() && Number.isFinite(n) && n >= 1 ? Math.floor(n) : null);
	}
</script>

<RowGroup title="Limits">
	<SettingRow label="Max model calls per turn" hint="Empty means unlimited.">
		<TextField
			value={form.max_turns ? String(form.max_turns) : ''}
			width={120}
			inputmode="numeric"
			placeholder="Unlimited"
			oninput={setMaxTurns}
		/>
	</SettingRow>
</RowGroup>

<RowGroup title="Workspace">
	<SettingRow label="Working directory" hint="Where it starts.">
		<TextField value={form.cwd} mono width={260} placeholder="/home/me/projects/api" oninput={(v) => onFormChange('cwd', v)} />
	</SettingRow>
	<SettingRow label="Additional directories" hint="Comma-separated.">
		<TextField value={form.add_dirs} mono width={260} placeholder="/home/me/notes" oninput={(v) => onFormChange('add_dirs', v)} />
	</SettingRow>
</RowGroup>

<RowGroup title="Setting sources">
	{#each SOURCES as row (row.id)}
		<SettingRow label={row.label} hint={row.hint}>
			<Switch checked={selectedSources.includes(row.id)} label={`${row.label} setting source`} onchange={() => toggleSource(row.id)} />
		</SettingRow>
	{/each}
</RowGroup>
<p class="st-note">Off removes what that scope adds.</p>

<RowGroup title="Replies">
	<SettingRow label="Stream replies" hint="Show text as it generates.">
		<Switch
			checked={form.include_partial_messages}
			label="Stream replies"
			onchange={(v) => onFormChange('include_partial_messages', v)}
		/>
	</SettingRow>
</RowGroup>

<style>
	.st-note {
		margin: 8px 2px 0;
		font-size: 12.5px;
		color: var(--st-mfg);
	}
</style>
