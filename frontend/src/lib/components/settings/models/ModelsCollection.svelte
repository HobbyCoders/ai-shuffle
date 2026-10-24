<script lang="ts">
	/**
	 * Default models (#379 step 3): the model catalog, the remembered choice
	 * per generation kind, and the app tasks that are not agent requests
	 * (session titles and commit text, dictation, read-aloud, voice calls).
	 *
	 * The per-tool media defaults are gone: an agent request names what it
	 * wants or gets a choice card, and a remembered pick is reused. Saved
	 * defaults were migrated into remembered choices on the server; media
	 * transform tools (editing, image to video, ...) keep their saved model and
	 * can be pinned from the catalog.
	 */
	import { onMount } from 'svelte';
	import { Sparkles } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import type { StatusTone } from '$lib/settings/types';
	import { watchResources } from '$lib/services/watch-resources';
	import { catalogStore } from '$lib/stores/catalog';
	import { GENERATION_KINDS, generationChoices } from '$lib/stores/generation-choices.svelte';
	import { modelLabel, sourceLabel, type GenerationKind } from '$lib/utils/generation-choice';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import { ALL_TOOLS, MediaTools, toolByItem, toolMeta } from './media-tools.svelte';
	import { setModelsState, TextTasks, VoiceDefaults, VOICE_CALL_MODEL } from './tasks.svelte';
	import {
		CATALOG_ICON,
		DICTATION_ICON,
		GENERATION_ICONS,
		GENERATION_NAMES,
		TEXT_ICON,
		TOOL_ICONS,
		VOICE_CALL_ICON
	} from './icons';
	import TextTasksDetail from './TextTasksDetail.svelte';
	import ToolDetail from './ToolDetail.svelte';
	import DictationDetail from './DictationDetail.svelte';
	import VoiceCallsDetail from './VoiceCallsDetail.svelte';
	import CatalogDetail from './CatalogDetail.svelte';
	import GenerationChoiceDetail from './GenerationChoiceDetail.svelte';

	/** The one media tool left here: the read-aloud voice is an app task. */
	const READ_ALOUD = ALL_TOOLS.find((t) => t.id === 'tts');

	const tools = new MediaTools();
	const text = new TextTasks(tools);
	const voice = new VoiceDefaults();
	setModelsState({ tools, text, voice });

	onMount(() => {
		const stop = tools.start();
		void text.load();
		void voice.load();
		void generationChoices.load(true);
		return () => {
			stop();
			text.dispose();
			voice.dispose();
		};
	});
	watchResources(['settings'], async () => {
		await Promise.all([tools.load(), text.refresh(), voice.load(), generationChoices.load(true)]);
	});

	let filter = $state('');

	interface Row {
		id: string;
		name: string;
		summary: string;
		icon: SettingsIcon;
		status?: StatusTone;
	}

	function generationRow(kind: GenerationKind): Row {
		const choice = generationChoices.remembered[kind];
		return {
			id: `gen-${kind}`,
			name: GENERATION_NAMES[kind],
			summary: choice ? `${sourceLabel(choice.source)} · ${modelLabel(choice.model)}` : 'Asks each time',
			icon: GENERATION_ICONS[kind],
			status: choice ? undefined : 'off'
		};
	}

	function readAloudRow(): Row[] {
		if (!READ_ALOUD) return [];
		const name = tools.displayName(READ_ALOUD);
		return [
			{
				id: toolMeta(READ_ALOUD).item,
				name: 'Read aloud',
				summary: name || 'No model chosen',
				icon: TOOL_ICONS[READ_ALOUD.id],
				status: !name ? 'off' : tools.blockedByFal(READ_ALOUD) ? 'warn' : undefined
			}
		];
	}

	const catalogSummary = $derived.by(() => {
		void tools.catalogVersion;
		const n = catalogStore.catalogSize;
		return n ? `All accounts, ${n.toLocaleString()} fal.ai models` : 'All accounts and fal.ai models';
	});

	const effortText = $derived(text.effort ? `${text.effortLabel.toLowerCase()} effort` : 'default effort');
	const groups = $derived.by((): { label: string; rows: Row[] }[] => [
		{ label: 'Generation', rows: GENERATION_KINDS.map(generationRow) },
		{
			label: 'App tasks',
			rows: [
				{
					id: 'text',
					name: 'Text tasks',
					summary: `${text.label}, ${effortText}`,
					icon: TEXT_ICON,
					status: text.blocked ? 'warn' : undefined
				},
				{
					id: 'dict',
					name: 'Dictation',
					summary: voice.settings ? voice.inputModelName() : 'Chat mic and hotkey',
					icon: DICTATION_ICON,
					status: tools.falEnabled === false && voice.settings && voice.inputOnFal ? 'warn' : undefined
				},
				...readAloudRow(),
				{ id: 'voicecall', name: 'Voice calls', summary: voice.settings?.voice_model ?? VOICE_CALL_MODEL, icon: VOICE_CALL_ICON }
			]
		},
		{
			label: 'Catalog',
			rows: [{ id: 'catalog', name: 'Generation catalog', summary: catalogSummary, icon: CATALOG_ICON }]
		}
	]);

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
	const selectedKind = $derived(
		GENERATION_KINDS.find((kind) => selected === `gen-${kind}`) ?? null
	);
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
		{:else if selectedKind}
			{#key selectedKind}
				<GenerationChoiceDetail kind={selectedKind} />
			{/key}
		{:else if selectedTool && selectedTool.id === READ_ALOUD?.id}
			<ToolDetail tool={selectedTool} />
		{:else}
			<EmptyDetail icon={Sparkles} message="Select a task." />
		{/if}
	{/snippet}
</CollectionPage>
