<script lang="ts">
	/**
	 * General: Appearance, Conversation, Usage limits, Thinking and tools,
	 * Region.
	 *
	 * One registry entry (entries/general.ts) per `<SettingRow id>`. Every
	 * control saves through its store (theme, chatPreferences,
	 * timezonePreference), then `flashSaved()`. The chat preferences sync to
	 * the server through preferenceSync, so a change made elsewhere arrives
	 * through the same store.
	 */
	import { onMount } from 'svelte';
	import { theme, themePreference, currentPresetId, themePresets } from '$lib/stores/theme.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
	import { chatPreferences, type ChatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Segmented from '../rows/Segmented.svelte';
	import SelectField, { type SelectGroup } from '../rows/SelectField.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import Switch from '../rows/Switch.svelte';
	import { getEntry } from '$lib/settings/registry';
	import ThemeStyleGrid from './editors/ThemeStyleGrid.svelte';
	import LimitResumeRows from './LimitResumeRows.svelte';

	type ThemeChoice = 'light' | 'dark' | 'system';
	const THEME_OPTIONS: { value: ThemeChoice; label: string }[] = [
		{ value: 'light', label: 'Light' },
		{ value: 'dark', label: 'Dark' },
		{ value: 'system', label: 'System' }
	];

	const TIME_ZONES: SelectGroup[] = [
		{
			label: '',
			options: [
				{ value: 'local', label: 'Browser default' },
				{ value: 'UTC', label: 'UTC' }
			]
		},
		{
			label: 'Americas',
			options: [
				{ value: 'America/New_York', label: 'Eastern (New York)' },
				{ value: 'America/Chicago', label: 'Central (Chicago)' },
				{ value: 'America/Denver', label: 'Mountain (Denver)' },
				{ value: 'America/Los_Angeles', label: 'Pacific (Los Angeles)' },
				{ value: 'America/Anchorage', label: 'Alaska' },
				{ value: 'Pacific/Honolulu', label: 'Hawaii' },
				{ value: 'America/Sao_Paulo', label: 'São Paulo' }
			]
		},
		{
			label: 'Europe and Africa',
			options: [
				{ value: 'Europe/London', label: 'London' },
				{ value: 'Europe/Paris', label: 'Paris / Berlin' },
				{ value: 'Europe/Moscow', label: 'Moscow' },
				{ value: 'Africa/Cairo', label: 'Cairo' }
			]
		},
		{
			label: 'Asia and Pacific',
			options: [
				{ value: 'Asia/Dubai', label: 'Dubai' },
				{ value: 'Asia/Kolkata', label: 'India (Kolkata)' },
				{ value: 'Asia/Shanghai', label: 'China (Shanghai)' },
				{ value: 'Asia/Tokyo', label: 'Tokyo' },
				{ value: 'Asia/Seoul', label: 'Seoul' },
				{ value: 'Australia/Sydney', label: 'Sydney' },
				{ value: 'Pacific/Auckland', label: 'Auckland' }
			]
		}
	];

	let styleOpen = $state(false);
	const presetId = $derived($currentPresetId ?? '');
	const presetLabel = $derived(themePresets.find((p) => p.id === presetId)?.label ?? 'Default');
	const mode = $derived<'light' | 'dark'>(
		$themePreference === 'light' ||
			($themePreference === 'system' &&
				typeof matchMedia !== 'undefined' &&
				matchMedia('(prefers-color-scheme: light)').matches)
			? 'light'
			: 'dark'
	);

	onMount(() => {
		void timezonePreference.load();
		void chatPreferences.load();
	});

	function setTheme(next: ThemeChoice) {
		theme.setTheme(next);
		flashSaved();
	}

	function setPreset(id: string) {
		theme.setPreset(id || null);
		flashSaved();
	}

	type ChatKey = keyof ChatPreferences;
	/** Row id -> chat preference it edits, in page order per group. */
	const CONVERSATION: [string, ChatKey][] = [
		['general.suggestReply', 'suggestNextResponse'],
		['general.autoScroll', 'autoScrollOnNewMessage'],
		['general.timestamps', 'showMessageTimestamps'],
		['general.compact', 'compactMessageView']
	];
	const THINKING: [string, ChatKey][] = [
		['general.showThinking', 'showThinkingBlocks'],
		['general.expandThinking', 'thinkingStartExpanded'],
		['general.thinkingSummary', 'showThinkingSummary'],
		['general.expandTools', 'toolsStartExpanded'],
		['general.groupTools', 'groupToolCalls'],
		['general.expandTodos', 'todosStartExpanded']
	];
	/** Only meaningful while thinking blocks show. */
	const NEEDS_THINKING = new Set<ChatKey>(['thinkingStartExpanded', 'showThinkingSummary']);

	function setChat(key: ChatKey, value: boolean) {
		chatPreferences.updatePreference(key, value);
		flashSaved();
	}

	function resetChat(id: string, key: ChatKey) {
		const fallback = getEntry(id)?.default;
		if (typeof fallback === 'boolean') setChat(key, fallback);
	}

	function setTimezone(tz: string) {
		timezonePreference.setTimezone(tz);
		flashSaved();
	}
</script>

<PreferencePage category="general">
	<RowGroup title="Appearance">
		<SettingRow id="general.theme">
			<Segmented options={THEME_OPTIONS} value={$themePreference} onchange={setTheme} />
		</SettingRow>
		<SettingRow
			id="general.themeStyle"
			value={presetId}
			onReset={() => setPreset('')}
			expanded={styleOpen ? styleGrid : undefined}
		>
			<ExpandToggle value={presetLabel} bind:open={styleOpen} />
		</SettingRow>
	</RowGroup>
	<RowGroup title="Conversation">
		{#each CONVERSATION as [id, key] (id)}
			{@render chatRow(id, key)}
		{/each}
	</RowGroup>
	<LimitResumeRows />
	<RowGroup title="Thinking and tools">
		{#each THINKING as [id, key] (id)}
			{@render chatRow(id, key)}
		{/each}
	</RowGroup>
	<RowGroup title="Region">
		<SettingRow id="general.timezone" value={$timezonePreference} onReset={() => setTimezone('local')}>
			<SelectField value={$timezonePreference} groups={TIME_ZONES} onchange={setTimezone} />
		</SettingRow>
	</RowGroup>
</PreferencePage>

{#snippet chatRow(id: string, key: ChatKey)}
	{@const off = NEEDS_THINKING.has(key) && !$chatPreferences.showThinkingBlocks}
	<SettingRow {id} value={$chatPreferences[key]} onReset={() => resetChat(id, key)} dim={off}>
		<Switch checked={$chatPreferences[key]} disabled={off} onchange={(v) => setChat(key, v)} />
	</SettingRow>
{/snippet}

{#snippet styleGrid()}
	<ThemeStyleGrid value={presetId} {mode} onselect={setPreset} />
{/snippet}
