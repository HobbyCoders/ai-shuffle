<script lang="ts">
	/**
	 * Default models: the model each task uses. Text tasks, the fal.ai media
	 * tools (image, video, 3D, audio), dictation, voice calls (read-only) and
	 * the fal.ai catalog. A list of tasks beside the selected task's detail.
	 */
	import { onMount } from 'svelte';
	import { Sparkles } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import type { StatusTone } from '$lib/settings/types';
	import { watchResources } from '$lib/services/watch-resources';
	import { catalogStore } from '$lib/stores/catalog';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import { ALL_TOOLS, MediaTools, toolByItem, toolMeta, type ToolGroup } from './media-tools.svelte';
	import { setModelsState, TextTasks, VoiceDefaults, VOICE_CALL_MODEL } from './tasks.svelte';
	import { CATALOG_ICON, DICTATION_ICON, TEXT_ICON, TOOL_ICONS, VOICE_CALL_ICON } from './icons';
	import TextTasksDetail from './TextTasksDetail.svelte';
	import ToolDetail from './ToolDetail.svelte';
	import DictationDetail from './DictationDetail.svelte';
	import VoiceCallsDetail from './VoiceCallsDetail.svelte';
	import CatalogDetail from './CatalogDetail.svelte';

	const tools = new MediaTools();
	const text = new TextTasks(tools);
	const voice = new VoiceDefaults();
	setModelsState({ tools, text, voice });

	onMount(() => {
		const stop = tools.start();
		void text.load();
		void voice.load();
		return () => {
			stop();
			text.dispose();
			voice.dispose();
		};
	});
	watchResources(['settings'], async () => {
		await Promise.all([tools.load(), text.refresh(), voice.load()]);
	});

	let filter = $state('');

	interface Row {
		id: string;
		name: string;
		summary: string;
		icon: SettingsIcon;
		status?: StatusTone;
	}

	function toolRows(...groups: ToolGroup[]): Row[] {
		return ALL_TOOLS.filter((t) => groups.includes(toolMeta(t).group)).map((t) => {
			const meta = toolMeta(t);
			const name = tools.displayName(t);
			return {
				id: meta.item,
				name: meta.name,
				summary: name || 'No model chosen',
				icon: TOOL_ICONS[t.id],
				status: !name ? 'off' : tools.blockedByFal(t) ? 'warn' : undefined
			};
		});
	}

	const catalogSummary = $derived.by(() => {
		void tools.catalogVersion;
		const n = catalogStore.catalogSize;
		return n ? `${n.toLocaleString()} models` : 'Browse and pick';
	});

	const effortText = $derived(text.effort ? `${text.effortLabel.toLowerCase()} effort` : 'default effort');
	const groups = $derived.by((): { label: string; rows: Row[] }[] => {
		const audioTools = toolRows('Audio');
		return [
			{
				label: 'Text',
				rows: [
					{
						id: 'text',
						name: 'Text tasks',
						summary: `${text.label}, ${effortText}`,
						icon: TEXT_ICON,
						status: text.blocked ? 'warn' : undefined
					}
				]
			},
			{ label: 'Image', rows: toolRows('Image') },
			{ label: 'Video', rows: toolRows('Video') },
			{ label: '3D', rows: toolRows('3D') },
			{
				label: 'Audio',
				rows: [
					{
						id: 'dict',
						name: 'Dictation',
						summary: voice.settings ? voice.inputModelName() : 'Chat mic and hotkey',
						icon: DICTATION_ICON,
						status: tools.falEnabled === false && voice.settings && voice.inputOnFal ? 'warn' : undefined
					},
					...audioTools,
					{ id: 'voicecall', name: 'Voice calls', summary: `${VOICE_CALL_MODEL}, fixed`, icon: VOICE_CALL_ICON }
				]
			},
			{
				label: 'Catalog',
				rows: [{ id: 'catalog', name: 'All fal.ai models', summary: catalogSummary, icon: CATALOG_ICON }]
			}
		];
	});

	const taskCount = $derived(groups.reduce((n, g) => n + g.rows.length, 0) - 1);
	const q = $derived(filter.trim().toLowerCase());
	const visible = $derived(
		groups
			.map((g) => ({
				...g,
				rows: q ? g.rows.filter((r) => `${r.name} ${r.summary} ${g.label}`.toLowerCase().includes(q)) : g.rows
			}))
			.filter((g) => g.rows.length)
	);

	const selected = $derived(settingsNav.selectedItem('models'));
	const selectedTool = $derived(toolByItem(selected));
</script>

<CollectionPage category="models" count="{taskCount} tasks" filterPlaceholder="Filter default models" bind:filter>
	{#snippet list()}
		{#each visible as group (group.label)}
			<ListGroupLabel label={group.label} />
			{#each group.rows as row (row.id)}
				<ListItem
					id={row.id}
					name={row.name}
					summary={row.summary}
					icon={row.icon}
					status={row.status}
					category="models"
				/>
			{/each}
		{:else}
			<p class="st-empty">No task matches “{filter.trim()}”.</p>
		{/each}
	{/snippet}
	{#snippet detail()}
		{#if selected === 'text'}
			<TextTasksDetail />
		{:else if selected === 'dict'}
			<DictationDetail />
		{:else if selected === 'voicecall'}
			<VoiceCallsDetail />
		{:else if selected === 'catalog'}
			<CatalogDetail />
		{:else if selectedTool}
			{#key selectedTool.id}
				<ToolDetail tool={selectedTool} />
			{/key}
		{:else}
			<EmptyDetail icon={Sparkles} message="Select a task." />
		{/if}
	{/snippet}
</CollectionPage>
