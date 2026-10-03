<script lang="ts">
	/** A hook a plugin contributes. Read-only; the plugin manages it. */
	import { Puzzle } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import { EVENT_HINTS, isToolEvent, type PluginHookRow } from './hooks-model.svelte';

	interface Props {
		hook: PluginHookRow;
	}

	let { hook }: Props = $props();

	const hint = $derived(EVENT_HINTS[hook.event as keyof typeof EVENT_HINTS]);
</script>

<DetailPane
	name={hook.plugin_name}
	icon={Puzzle}
	status="ok"
	statusText="Managed by plugin"
	badge="Plugin hook"
	subtitle={hook.description || undefined}
	category="hooks"
	item={hook.id}
	facts={[
		{ label: 'Event', value: hook.event },
		{ label: 'Type', value: hook.hook_type }
	]}
>
	{#snippet primary()}
		<button
			type="button"
			class="st-btn"
			onclick={() => settingsNav.go({ category: 'plugins', item: `plugin:${hook.plugin_id}` })}
		>
			Open plugin
		</button>
	{/snippet}
	{#snippet children()}
		<RowGroup title="Trigger">
			<SettingRow label="Event" {hint}>
				<ValueText value={hook.event} mono tone="fg" />
			</SettingRow>
			{#if isToolEvent(hook.event)}
				<SettingRow label="Tool matcher">
					<ValueText value={hook.tool_matcher || 'All tools'} mono />
				</SettingRow>
			{/if}
			<SettingRow label="Editing" hint="Change it in the plugin">
				<ValueText value="Read-only" />
			</SettingRow>
		</RowGroup>
	{/snippet}
</DetailPane>
