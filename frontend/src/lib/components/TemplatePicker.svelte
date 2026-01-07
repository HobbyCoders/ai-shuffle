<script lang="ts">
	import { createEventDispatcher, onMount } from 'svelte';
	import type { Template } from '$lib/api/client';
	import { getTemplates } from '$lib/api/client';

	const dispatch = createEventDispatcher<{
		select: Template;
		close: void;
	}>();

	export let profileId: string | null = null;

	let templates: Template[] = [];
	let loading = true;
	let error = '';
	let searchQuery = '';

	// Get icon display
	function getIconDisplay(iconName: string | null): string {
		if (!iconName) return '\uD83D\uDCDD'; // Default memo emoji
		const iconMap: Record<string, string> = {
			'magnifying_glass_tilted_right': '\uD83D\uDD0D',
			'books': '\uD83D\uDCDA',
			'bug': '\uD83D\uDC1B',
			'test_tube': '\uD83E\uDDEA',
			'memo': '\uD83D\uDCDD',
			'hammer_and_wrench': '\uD83D\uDEE0',
			'rocket': '\uD83D\uDE80',
			'brain': '\uD83E\uDDE0',
			'light_bulb': '\uD83D\uDCA1',
			'speech_balloon': '\uD83D\uDCAC',
			'file_folder': '\uD83D\uDCC1'
		};
		return iconMap[iconName] || '\uD83D\uDCDD';
	}

	// Load templates
	async function loadTemplates() {
		loading = true;
		error = '';
		try {
			templates = await getTemplates({ profileId: profileId || undefined });
		} catch (err: any) {
			error = err.detail || 'Failed to load templates';
		} finally {
			loading = false;
		}
	}

	// Group templates by category
	function getGroupedTemplates(): Map<string, Template[]> {
		const filtered = searchQuery
			? templates.filter(t =>
				t.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
				(t.description || '').toLowerCase().includes(searchQuery.toLowerCase())
			)
			: templates;

		const grouped = new Map<string, Template[]>();
		for (const template of filtered) {
			const cat = template.category || '';
			if (!grouped.has(cat)) {
				grouped.set(cat, []);
			}
			grouped.get(cat)!.push(template);
		}
		return grouped;
	}

	// Get category display name
	function getCategoryName(catId: string): string {
		const categoryNames: Record<string, string> = {
			'': 'General',
			'coding': 'Coding',
			'writing': 'Writing',
			'analysis': 'Analysis',
			'debugging': 'Debugging',
			'documentation': 'Documentation'
		};
		return categoryNames[catId] || catId;
	}

	// Select a template
	function selectTemplate(template: Template) {
		dispatch('select', template);
	}

	onMount(() => {
		loadTemplates();
	});
</script>

<div class="space-y-4">
	<!-- Search -->
	<div class="relative">
		<svg class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
		</svg>
		<input
			type="text"
			bind:value={searchQuery}
			placeholder="Search templates..."
			class="w-full pl-10 pr-4 py-2 rounded-lg bg-muted/50 border border-border text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 text-sm"
		/>
	</div>

	<!-- Templates -->
	{#if loading}
		<div class="flex items-center justify-center py-8">
			<div class="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent"></div>
		</div>
	{:else if error}
		<div class="p-3 rounded-lg bg-destructive/10 text-destructive text-sm">
			{error}
		</div>
	{:else if templates.length === 0}
		<div class="text-center py-6 text-muted-foreground text-sm">
			No templates available. Create templates in Settings.
		</div>
	{:else}
		{@const grouped = getGroupedTemplates()}
		{#if grouped.size === 0}
			<div class="text-center py-6 text-muted-foreground text-sm">
				No templates match your search.
			</div>
		{:else}
			<div class="space-y-4 max-h-[50vh] overflow-y-auto">
				{#each [...grouped.entries()] as [category, categoryTemplates]}
					<div class="space-y-2">
						<h4 class="text-xs font-medium text-muted-foreground uppercase tracking-wide">{getCategoryName(category)}</h4>
						<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
							{#each categoryTemplates as template (template.id)}
								<button
									type="button"
									on:click={() => selectTemplate(template)}
									class="flex items-start gap-3 p-3 rounded-lg bg-muted/50 border border-border hover:border-primary/50 hover:bg-hover-overlay transition-colors text-left group"
								>
									<div class="w-8 h-8 rounded-lg bg-accent flex items-center justify-center text-lg flex-shrink-0 group-hover:bg-primary/20 transition-colors">
										{getIconDisplay(template.icon)}
									</div>
									<div class="flex-1 min-w-0">
										<div class="font-medium text-foreground text-sm">{template.name}</div>
										{#if template.description}
											<p class="text-xs text-muted-foreground mt-0.5 line-clamp-2">{template.description}</p>
										{/if}
									</div>
								</button>
							{/each}
						</div>
					</div>
				{/each}
			</div>
		{/if}
	{/if}

	<!-- Blank chat option -->
	<div class="pt-2 border-t border-border">
		<button
			type="button"
			on:click={() => dispatch('close')}
			class="w-full px-4 py-2 rounded-lg bg-muted/50 text-muted-foreground hover:text-foreground hover:bg-hover-overlay transition-colors text-sm flex items-center justify-center gap-2"
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
				<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
			</svg>
			Start blank chat
		</button>
	</div>
</div>
