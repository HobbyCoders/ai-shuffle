<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { api } from '$lib/api/client';

	const dispatch = createEventDispatcher();

	// Props
	export let show = false;
	export let editingProfile: any = null;
	export let profiles: any[] = [];
	export let allSubagents: any[] = [];
	export let availableTools: { all_tools: any[]; categories: any[] } = { all_tools: [], categories: [] };
	export let groups: any;

	// View state
	let showForm = false;
	let activeTab = 'general';

	// Profile form state
	let profileForm = {
		id: '',
		name: '',
		description: '',
		model: 'sonnet',
		permission_mode: 'default',
		max_turns: null as number | null,
		allowed_tools: [] as string[],
		disallowed_tools: [] as string[],
		enabled_agents: [] as string[],
		include_partial_messages: true,
		continue_conversation: false,
		fork_session: false,
		system_prompt_type: 'preset',
		system_prompt_preset: 'claude_code',
		system_prompt_append: '',
		system_prompt_content: '',
		system_prompt_inject_env: false,
		enabled_ai_tools: [] as string[],
		setting_sources: [] as string[],
		cwd: '',
		add_dirs: '',
		user: '',
		max_buffer_size: null as number | null
	};

	// Tool selection mode
	let toolSelectionMode: 'all' | 'allow' | 'disallow' = 'all';
	let aiToolSelectionMode: 'all' | 'custom' = 'custom';

	// AI Tools
	interface AITool {
		id: string;
		name: string;
		description: string;
		category: string;
		available: boolean;
		providers: string[];
		active_provider: string | null;
	}

	interface AIToolCategory {
		id: string;
		name: string;
		tools: AITool[];
	}

	let aiToolCategories: AIToolCategory[] = [];
	let aiToolsLoading = false;

	// Tab definitions
	const tabs = [
		{ id: 'general', label: 'General', icon: 'settings' },
		{ id: 'tools', label: 'Tools', icon: 'wrench' },
		{ id: 'agents', label: 'Agents', icon: 'users' },
		{ id: 'prompt', label: 'Prompt', icon: 'message' },
		{ id: 'advanced', label: 'Advanced', icon: 'cog' }
	];

	// Import handling
	let profileImportInput: HTMLInputElement;
	let profileImporting = false;

	// Load AI tools when form opens
	async function loadAvailableAITools() {
		aiToolsLoading = true;
		try {
			const response = await api.get<{ categories: AIToolCategory[] }>('/settings/ai-tools/available');
			aiToolCategories = response.categories;
		} catch (e) {
			console.error('Failed to load AI tools:', e);
			aiToolCategories = [];
		}
		aiToolsLoading = false;
	}

	function getAllAIToolIds(): string[] {
		return aiToolCategories.flatMap(cat => cat.tools.map(t => t.id));
	}

	function resetForm() {
		profileForm = {
			id: '',
			name: '',
			description: '',
			model: 'sonnet',
			permission_mode: 'default',
			max_turns: null,
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			include_partial_messages: true,
			continue_conversation: false,
			fork_session: false,
			system_prompt_type: 'preset',
			system_prompt_preset: 'claude_code',
			system_prompt_append: '',
			system_prompt_content: '',
			system_prompt_inject_env: false,
			enabled_ai_tools: [],
			setting_sources: [],
			cwd: '',
			add_dirs: '',
			user: '',
			max_buffer_size: null
		};
		toolSelectionMode = 'all';
		aiToolSelectionMode = 'custom';
		activeTab = 'general';
	}

	function openNewForm() {
		resetForm();
		editingProfile = null;
		showForm = true;
		loadAvailableAITools();
	}

	function openEditForm(profile: any) {
		editingProfile = profile;
		const config = profile.config || {};
		const sp = config.system_prompt || {};
		const aiTools = config.ai_tools || {};

		const allowedTools = config.allowed_tools || [];
		const disallowedTools = config.disallowed_tools || [];

		// Build enabled AI tools array
		const enabledAITools: string[] = [];
		if (aiTools.image_generation) enabledAITools.push('image_generation');
		if (aiTools.image_editing) enabledAITools.push('image_editing');
		if (aiTools.image_reference) enabledAITools.push('image_reference');
		if (aiTools.video_generation) enabledAITools.push('video_generation');
		if (aiTools.video_with_audio) enabledAITools.push('video_with_audio');
		if (aiTools.image_to_video) enabledAITools.push('image_to_video');
		if (aiTools.video_extend) enabledAITools.push('video_extend');
		if (aiTools.video_bridge) enabledAITools.push('video_bridge');
		if (aiTools.video_understanding) enabledAITools.push('video_understanding');

		// Determine tool selection mode
		if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: config.model || 'sonnet',
			permission_mode: config.permission_mode || 'default',
			max_turns: config.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: config.enabled_agents || [],
			include_partial_messages: config.include_partial_messages !== false,
			continue_conversation: config.continue_conversation || false,
			fork_session: config.fork_session || false,
			system_prompt_type: sp.type || 'preset',
			system_prompt_preset: sp.preset || 'claude_code',
			system_prompt_append: sp.append || '',
			system_prompt_content: sp.content || '',
			system_prompt_inject_env: sp.inject_env_details || false,
			enabled_ai_tools: enabledAITools,
			setting_sources: config.setting_sources || [],
			cwd: config.cwd || '',
			add_dirs: (config.add_dirs || []).join(', '),
			user: config.user || '',
			max_buffer_size: config.max_buffer_size || null
		};

		showForm = true;
		activeTab = 'general';
		loadAvailableAITools().then(() => {
			// Check if all AI tools are enabled
			const allIds = getAllAIToolIds();
			if (enabledAITools.length === allIds.length && allIds.every(id => enabledAITools.includes(id))) {
				aiToolSelectionMode = 'all';
			} else {
				aiToolSelectionMode = 'custom';
			}
		});
	}

	function close() {
		show = false;
		showForm = false;
		resetForm();
		dispatch('close');
	}

	// Tool helpers
	function toggleTool(toolName: string) {
		if (toolSelectionMode === 'allow') {
			if (profileForm.allowed_tools.includes(toolName)) {
				profileForm.allowed_tools = profileForm.allowed_tools.filter(t => t !== toolName);
			} else {
				profileForm.allowed_tools = [...profileForm.allowed_tools, toolName];
			}
		} else if (toolSelectionMode === 'disallow') {
			if (profileForm.disallowed_tools.includes(toolName)) {
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter(t => t !== toolName);
			} else {
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, toolName];
			}
		}
	}

	function toggleCategory(category: any) {
		const categoryToolNames = category.tools.map((t: any) => t.name);
		if (toolSelectionMode === 'allow') {
			const allSelected = categoryToolNames.every((name: string) => profileForm.allowed_tools.includes(name));
			if (allSelected) {
				profileForm.allowed_tools = profileForm.allowed_tools.filter(t => !categoryToolNames.includes(t));
			} else {
				const newTools = categoryToolNames.filter((name: string) => !profileForm.allowed_tools.includes(name));
				profileForm.allowed_tools = [...profileForm.allowed_tools, ...newTools];
			}
		} else if (toolSelectionMode === 'disallow') {
			const allDisabled = categoryToolNames.every((name: string) => profileForm.disallowed_tools.includes(name));
			if (allDisabled) {
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter(t => !categoryToolNames.includes(t));
			} else {
				const newTools = categoryToolNames.filter((name: string) => !profileForm.disallowed_tools.includes(name));
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, ...newTools];
			}
		}
	}

	function getCategorySelectionState(category: any): 'all' | 'some' | 'none' {
		const categoryToolNames = category.tools.map((t: any) => t.name);
		const list = toolSelectionMode === 'allow' ? profileForm.allowed_tools : profileForm.disallowed_tools;
		const selectedCount = categoryToolNames.filter((name: string) => list.includes(name)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === categoryToolNames.length) return 'all';
		return 'some';
	}

	// AI Tool helpers
	function toggleAITool(toolId: string) {
		if (profileForm.enabled_ai_tools.includes(toolId)) {
			profileForm.enabled_ai_tools = profileForm.enabled_ai_tools.filter(t => t !== toolId);
		} else {
			profileForm.enabled_ai_tools = [...profileForm.enabled_ai_tools, toolId];
		}
	}

	function toggleAIToolCategory(category: AIToolCategory) {
		const ids = category.tools.map(t => t.id);
		const allSelected = ids.every(id => profileForm.enabled_ai_tools.includes(id));
		if (allSelected) {
			profileForm.enabled_ai_tools = profileForm.enabled_ai_tools.filter(t => !ids.includes(t));
		} else {
			const newTools = ids.filter(id => !profileForm.enabled_ai_tools.includes(id));
			profileForm.enabled_ai_tools = [...profileForm.enabled_ai_tools, ...newTools];
		}
	}

	// Agent helpers
	function toggleAgent(agentId: string) {
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter(id => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
		}
	}

	function toggleSettingSource(source: string) {
		if (profileForm.setting_sources.includes(source)) {
			profileForm.setting_sources = profileForm.setting_sources.filter(s => s !== source);
		} else {
			profileForm.setting_sources = [...profileForm.setting_sources, source];
		}
	}

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku': return 'Haiku';
			case 'sonnet': return 'Sonnet';
			case 'sonnet-1m': return 'Sonnet 1M';
			case 'opus': return 'Opus';
			default: return model || 'Default';
		}
	}

	// Save profile
	async function saveProfile() {
		if (!profileForm.id || !profileForm.name) return;

		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			include_partial_messages: profileForm.include_partial_messages,
			continue_conversation: profileForm.continue_conversation,
			fork_session: profileForm.fork_session
		};

		if (profileForm.max_turns) config.max_turns = profileForm.max_turns;
		if (toolSelectionMode === 'allow' && profileForm.allowed_tools.length > 0) {
			config.allowed_tools = profileForm.allowed_tools;
		}
		if (toolSelectionMode === 'disallow' && profileForm.disallowed_tools.length > 0) {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		if (profileForm.enabled_agents.length > 0) {
			config.enabled_agents = profileForm.enabled_agents;
		}
		if (profileForm.setting_sources.length > 0) {
			config.setting_sources = profileForm.setting_sources;
		}
		if (profileForm.cwd.trim()) config.cwd = profileForm.cwd;
		if (profileForm.add_dirs.trim()) {
			config.add_dirs = profileForm.add_dirs.split(',').map(d => d.trim()).filter(Boolean);
		}
		if (profileForm.user.trim()) config.user = profileForm.user;
		if (profileForm.max_buffer_size) config.max_buffer_size = profileForm.max_buffer_size;

		// AI tools
		const effectiveAITools = aiToolSelectionMode === 'all' ? getAllAIToolIds() : profileForm.enabled_ai_tools;
		if (effectiveAITools.length > 0) {
			config.ai_tools = {
				image_generation: effectiveAITools.includes('image_generation'),
				image_editing: effectiveAITools.includes('image_editing'),
				image_reference: effectiveAITools.includes('image_reference'),
				video_generation: effectiveAITools.includes('video_generation'),
				video_with_audio: effectiveAITools.includes('video_with_audio'),
				image_to_video: effectiveAITools.includes('image_to_video'),
				video_extend: effectiveAITools.includes('video_extend'),
				video_bridge: effectiveAITools.includes('video_bridge'),
				video_understanding: effectiveAITools.includes('video_understanding')
			};
		}

		// System prompt
		if (profileForm.system_prompt_type === 'preset') {
			config.system_prompt = {
				type: 'preset',
				preset: 'claude_code'
			};
			if (profileForm.system_prompt_append.trim()) {
				config.system_prompt.append = profileForm.system_prompt_append;
			}
		} else {
			config.system_prompt = {
				type: 'custom',
				content: profileForm.system_prompt_content,
				inject_env_details: profileForm.system_prompt_inject_env
			};
		}

		dispatch('save', {
			isNew: !editingProfile,
			data: {
				id: profileForm.id.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
				name: profileForm.name,
				description: profileForm.description || undefined,
				config
			}
		});

		showForm = false;
		resetForm();
	}

	// Export / Import
	function exportProfile(profileId: string) {
		dispatch('export', profileId);
	}

	function triggerImport() {
		profileImportInput?.click();
	}

	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		profileImporting = true;
		dispatch('import', input.files[0]);
		input.value = '';
		profileImporting = false;
	}

	function deleteProfile(profileId: string) {
		dispatch('delete', profileId);
	}

	function assignToGroup(profileId: string, groupName: string) {
		dispatch('assignGroup', { profileId, groupName });
	}

	function removeFromGroup(profileId: string) {
		dispatch('removeGroup', profileId);
	}

	function createGroup(profileId: string) {
		const name = prompt('New group name:');
		if (name?.trim()) {
			dispatch('createGroup', { profileId, groupName: name.trim() });
		}
	}

	// Computed values for tool counts
	$: selectedToolCount = toolSelectionMode === 'allow'
		? profileForm.allowed_tools.length
		: toolSelectionMode === 'disallow'
			? profileForm.disallowed_tools.length
			: availableTools.all_tools.length;

	$: aiToolCount = aiToolSelectionMode === 'all'
		? getAllAIToolIds().length
		: profileForm.enabled_ai_tools.length;
</script>

{#if show}
	<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
	<div
		class="fixed inset-0 max-sm:bottom-[calc(4.5rem+env(safe-area-inset-bottom,0px))] bg-black/60 backdrop-blur-sm z-50 flex items-center justify-center p-2 sm:p-4"
		on:click={close}
		on:keydown={(e) => e.key === 'Escape' && close()}
		role="dialog"
		aria-modal="true"
		tabindex="-1"
	>
		<!-- svelte-ignore a11y_no_static_element_interactions a11y_click_events_have_key_events -->
		<div
			class="bg-card rounded-2xl w-full max-w-4xl max-h-[95vh] sm:max-h-[90vh] overflow-hidden shadow-2xl border border-border flex flex-col"
			on:click|stopPropagation
		>
			<!-- Header -->
			<div class="px-4 sm:px-6 py-4 border-b border-border flex items-center justify-between shrink-0 bg-gradient-to-r from-primary/5 to-transparent">
				<div>
					<h2 class="text-lg font-semibold text-foreground">
						{#if showForm}
							{editingProfile ? 'Edit Profile' : 'New Profile'}
						{:else}
							Profiles
						{/if}
					</h2>
					{#if showForm && editingProfile}
						<p class="text-xs text-muted-foreground font-mono">{editingProfile.id}</p>
					{/if}
				</div>
				<button
					class="p-2 text-muted-foreground hover:text-foreground rounded-lg hover:bg-muted transition-colors"
					on:click={close}
					aria-label="Close"
				>
					<svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>

			{#if showForm}
				<!-- Form View with Tabs -->
				<div class="flex flex-col md:flex-row flex-1 overflow-hidden">
					<!-- Tab Navigation - Horizontal on mobile, Vertical on desktop -->
					<nav class="shrink-0 border-b md:border-b-0 md:border-r border-border bg-muted/30">
						<!-- Mobile: Horizontal scrollable tabs -->
						<div class="flex md:hidden overflow-x-auto scrollbar-hide">
							{#each tabs as tab}
								<button
									class="flex-1 flex items-center justify-center gap-2 px-4 py-3 text-sm font-medium whitespace-nowrap transition-all relative {activeTab === tab.id ? 'text-primary' : 'text-muted-foreground hover:text-foreground'}"
									on:click={() => activeTab = tab.id}
								>
									{#if tab.icon === 'settings'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									{:else if tab.icon === 'wrench'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
										</svg>
									{:else if tab.icon === 'users'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" />
										</svg>
									{:else if tab.icon === 'message'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
										</svg>
									{:else if tab.icon === 'cog'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
										</svg>
									{/if}
									<span class="hidden sm:inline">{tab.label}</span>
									{#if activeTab === tab.id}
										<div class="absolute bottom-0 left-0 right-0 h-0.5 bg-primary"></div>
									{/if}
								</button>
							{/each}
						</div>
						<!-- Desktop: Vertical tabs -->
						<div class="hidden md:flex flex-col w-44 p-3 space-y-1">
							{#each tabs as tab}
								<button
									class="flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-all {activeTab === tab.id ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground hover:bg-muted/50'}"
									on:click={() => activeTab = tab.id}
								>
									{#if tab.icon === 'settings'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
										</svg>
									{:else if tab.icon === 'wrench'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
										</svg>
									{:else if tab.icon === 'users'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z" />
										</svg>
									{:else if tab.icon === 'message'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
										</svg>
									{:else if tab.icon === 'cog'}
										<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4" />
										</svg>
									{/if}
									<span>{tab.label}</span>
								</button>
							{/each}
						</div>
					</nav>

					<!-- Tab Content -->
					<div class="flex-1 overflow-y-auto">
						<div class="p-4 space-y-6">
							{#if activeTab === 'general'}
								<!-- General Tab -->
								<div class="space-y-4">
									<div>
										<h3 class="text-sm font-medium text-foreground mb-3">Profile Information</h3>
										<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">ID <span class="text-destructive">*</span></label>
												<input
													bind:value={profileForm.id}
													disabled={!!editingProfile}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-primary/50"
													placeholder="my-profile"
												/>
											</div>
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">Name <span class="text-destructive">*</span></label>
												<input
													bind:value={profileForm.name}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
													placeholder="My Profile"
												/>
											</div>
										</div>
										<div class="mt-4">
											<label class="block text-xs text-muted-foreground mb-1.5">Description</label>
											<input
												bind:value={profileForm.description}
												class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
												placeholder="Optional description"
											/>
										</div>
									</div>

									<div class="border-t border-border pt-4">
										<h3 class="text-sm font-medium text-foreground mb-3">Core Settings</h3>
										<div class="grid grid-cols-1 sm:grid-cols-3 gap-4">
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">Model</label>
												<select
													bind:value={profileForm.model}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
												>
													<option value="sonnet">Sonnet</option>
													<option value="sonnet-1m">Sonnet 1M</option>
													<option value="opus">Opus</option>
													<option value="haiku">Haiku</option>
												</select>
											</div>
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">Permission Mode</label>
												<select
													bind:value={profileForm.permission_mode}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
												>
													<option value="default">Default</option>
													<option value="acceptEdits">Accept Edits</option>
													<option value="plan">Plan</option>
													<option value="bypassPermissions">Bypass</option>
												</select>
											</div>
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">Max Turns</label>
												<input
													type="number"
													bind:value={profileForm.max_turns}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
													placeholder="Unlimited"
												/>
											</div>
										</div>
									</div>

									<div class="border-t border-border pt-4">
										<h3 class="text-sm font-medium text-foreground mb-3">Behavior</h3>
										<div class="space-y-3">
											<label class="flex items-start gap-3 cursor-pointer group">
												<input
													type="checkbox"
													bind:checked={profileForm.include_partial_messages}
													class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
												/>
												<div>
													<span class="text-sm text-foreground group-hover:text-primary transition-colors">Include Partial Messages</span>
													<p class="text-xs text-muted-foreground">Stream partial text as it's being generated</p>
												</div>
											</label>
											<label class="flex items-start gap-3 cursor-pointer group">
												<input
													type="checkbox"
													bind:checked={profileForm.continue_conversation}
													class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
												/>
												<div>
													<span class="text-sm text-foreground group-hover:text-primary transition-colors">Continue Conversation</span>
													<p class="text-xs text-muted-foreground">Automatically continue most recent conversation</p>
												</div>
											</label>
											<label class="flex items-start gap-3 cursor-pointer group">
												<input
													type="checkbox"
													bind:checked={profileForm.fork_session}
													class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
												/>
												<div>
													<span class="text-sm text-foreground group-hover:text-primary transition-colors">Fork Session</span>
													<p class="text-xs text-muted-foreground">Create new session ID when resuming</p>
												</div>
											</label>
										</div>
									</div>
								</div>

							{:else if activeTab === 'tools'}
								<!-- Tools Tab -->
								<div class="space-y-6">
									<!-- Claude Tools Section -->
									<div>
										<div class="flex items-center justify-between mb-3">
											<h3 class="text-sm font-medium text-foreground">Claude Tools</h3>
											<span class="text-xs px-2 py-1 bg-primary/10 text-primary rounded-full">
												{availableTools.all_tools.length} available
											</span>
										</div>

										<!-- Tool Mode Selector -->
										<div class="flex flex-wrap gap-2 mb-4">
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'all'; profileForm.allowed_tools = []; profileForm.disallowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'all' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												All Tools
											</button>
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'allow'; profileForm.disallowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'allow' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Allow Only
											</button>
											<button
												type="button"
												on:click={() => { toolSelectionMode = 'disallow'; profileForm.allowed_tools = []; }}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {toolSelectionMode === 'disallow' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Disallow
											</button>
										</div>

										<p class="text-xs text-muted-foreground mb-4">
											{#if toolSelectionMode === 'all'}
												Agent can use all available tools.
											{:else if toolSelectionMode === 'allow'}
												Agent can ONLY use selected tools.
											{:else}
												Agent can use all tools EXCEPT selected ones.
											{/if}
										</p>

										{#if toolSelectionMode !== 'all'}
											<div class="space-y-2 max-h-64 overflow-y-auto pr-2">
												{#each availableTools.categories as category}
													{#if category.tools.length > 0}
														{@const state = getCategorySelectionState(category)}
														<div class="border border-border rounded-lg overflow-hidden">
															<button
																type="button"
																on:click={() => toggleCategory(category)}
																class="w-full px-3 py-2 bg-muted/50 flex items-center gap-3 text-sm text-foreground hover:bg-muted transition-colors"
															>
																<div class="w-4 h-4 rounded flex items-center justify-center transition-colors {state !== 'none' ? 'bg-primary' : 'bg-muted border border-border'}">
																	{#if state === 'all'}
																		<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
																		</svg>
																	{:else if state === 'some'}
																		<svg class="w-3 h-3 text-primary-foreground" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																			<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
																		</svg>
																	{/if}
																</div>
																<span class="flex-1 text-left font-medium">{category.name}</span>
																<span class="text-xs text-muted-foreground">{category.tools.length}</span>
															</button>
															<div class="p-2 grid grid-cols-1 sm:grid-cols-2 gap-1 bg-card">
																{#each category.tools as tool}
																	{@const isSelected = toolSelectionMode === 'allow'
																		? profileForm.allowed_tools.includes(tool.name)
																		: profileForm.disallowed_tools.includes(tool.name)}
																	<label class="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-muted/50 cursor-pointer text-sm">
																		<input
																			type="checkbox"
																			checked={isSelected}
																			on:change={() => toggleTool(tool.name)}
																			class="w-3.5 h-3.5 rounded bg-muted border-border text-primary focus:ring-primary/50"
																		/>
																		<span class="text-foreground truncate" title={tool.description}>{tool.name}</span>
																	</label>
																{/each}
															</div>
														</div>
													{/if}
												{/each}
											</div>

											<div class="mt-3 text-xs text-muted-foreground">
												{#if toolSelectionMode === 'allow'}
													{profileForm.allowed_tools.length} tool{profileForm.allowed_tools.length !== 1 ? 's' : ''} allowed
												{:else}
													{profileForm.disallowed_tools.length} tool{profileForm.disallowed_tools.length !== 1 ? 's' : ''} blocked
												{/if}
											</div>
										{/if}
									</div>

									<!-- AI Tools Section -->
									<div class="border-t border-border pt-6">
										<div class="flex items-center justify-between mb-3">
											<h3 class="text-sm font-medium text-foreground">AI Tools</h3>
											{#if !aiToolsLoading}
												<span class="text-xs px-2 py-1 bg-violet-500/10 text-violet-500 rounded-full">
													{aiToolCount} enabled
												</span>
											{/if}
										</div>

										<div class="flex flex-wrap gap-2 mb-4">
											<button
												type="button"
												on:click={() => aiToolSelectionMode = 'all'}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {aiToolSelectionMode === 'all' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												All Tools
											</button>
											<button
												type="button"
												on:click={() => aiToolSelectionMode = 'custom'}
												class="px-3 py-1.5 text-xs rounded-lg transition-colors {aiToolSelectionMode === 'custom' ? 'bg-violet-600 text-white' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Custom
											</button>
										</div>

										{#if aiToolSelectionMode === 'custom'}
											{#if aiToolsLoading}
												<div class="text-sm text-muted-foreground text-center py-8">Loading AI tools...</div>
											{:else if aiToolCategories.length === 0}
												<div class="text-sm text-muted-foreground text-center py-8 bg-muted/30 rounded-lg">
													No AI tools available. Configure API keys in Settings → Integrations.
												</div>
											{:else}
												<div class="space-y-3">
													{#each aiToolCategories as category}
														{#if category.tools.length > 0}
															<div class="border border-border rounded-lg p-3">
																<div class="flex items-center justify-between mb-2">
																	<span class="text-sm font-medium text-foreground">{category.name}</span>
																	<button
																		type="button"
																		on:click={() => toggleAIToolCategory(category)}
																		class="text-xs text-primary hover:underline"
																	>
																		Toggle all
																	</button>
																</div>
																<div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
																	{#each category.tools as tool}
																		{@const isSelected = profileForm.enabled_ai_tools.includes(tool.id)}
																		<label class="flex items-start gap-2 p-2 rounded-lg hover:bg-muted/50 {tool.available ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'}">
																			<input
																				type="checkbox"
																				checked={isSelected}
																				disabled={!tool.available}
																				on:change={() => toggleAITool(tool.id)}
																				class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-violet-600 focus:ring-violet-500/50"
																			/>
																			<div class="flex-1 min-w-0">
																				<div class="flex items-center gap-2 flex-wrap">
																					<span class="text-sm text-foreground">{tool.name}</span>
																					{#if !tool.available}
																						<span class="text-[10px] px-1.5 py-0.5 bg-amber-500/20 text-amber-600 rounded">No API key</span>
																					{:else if tool.active_provider}
																						<span class="text-[10px] px-1.5 py-0.5 bg-green-500/20 text-green-600 rounded">{tool.active_provider}</span>
																					{/if}
																				</div>
																				<p class="text-xs text-muted-foreground truncate">{tool.description}</p>
																			</div>
																		</label>
																	{/each}
																</div>
															</div>
														{/if}
													{/each}
												</div>
											{/if}
										{:else}
											<p class="text-xs text-muted-foreground">All configured AI tools are enabled. Configure API keys in Settings → Integrations.</p>
										{/if}
									</div>
								</div>

							{:else if activeTab === 'agents'}
								<!-- Agents Tab -->
								<div class="space-y-4">
									<div class="flex items-center justify-between">
										<h3 class="text-sm font-medium text-foreground">Enabled Subagents</h3>
										{#if allSubagents.length > 0}
											<div class="flex gap-2">
												<button
													type="button"
													on:click={() => profileForm.enabled_agents = []}
													class="text-xs text-muted-foreground hover:text-foreground"
												>
													Clear
												</button>
												<button
													type="button"
													on:click={() => profileForm.enabled_agents = allSubagents.map(a => a.id)}
													class="text-xs text-primary hover:underline"
												>
													Select All
												</button>
											</div>
										{/if}
									</div>

									{#if allSubagents.length === 0}
										<div class="text-sm text-muted-foreground text-center py-12 bg-muted/30 rounded-lg">
											<svg class="w-12 h-12 mx-auto mb-3 text-muted-foreground/50" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
											</svg>
											<p>No subagents configured</p>
											<p class="text-xs mt-1">Create subagents from the Agents menu</p>
										</div>
									{:else}
										<p class="text-xs text-muted-foreground">Select which subagents this profile can use</p>
										<div class="grid grid-cols-1 sm:grid-cols-2 gap-2 max-h-80 overflow-y-auto pr-1">
											{#each allSubagents as agent (agent.id)}
												{@const isSelected = profileForm.enabled_agents.includes(agent.id)}
												<label class="flex items-start gap-3 p-3 rounded-lg border transition-colors cursor-pointer {isSelected ? 'border-primary bg-primary/5' : 'border-border hover:border-primary/50'}">
													<input
														type="checkbox"
														checked={isSelected}
														on:change={() => toggleAgent(agent.id)}
														class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
													/>
													<div class="flex-1 min-w-0">
														<div class="flex items-center gap-2">
															<span class="text-sm font-medium text-foreground">{agent.name}</span>
															<span class="text-[10px] px-1.5 py-0.5 rounded bg-primary/10 text-primary">{getModelDisplay(agent.model)}</span>
														</div>
														<p class="text-xs text-muted-foreground mt-0.5 line-clamp-2">{agent.description}</p>
													</div>
												</label>
											{/each}
										</div>
										<div class="text-xs text-muted-foreground pt-2 border-t border-border">
											{profileForm.enabled_agents.length} of {allSubagents.length} enabled
										</div>
									{/if}
								</div>

							{:else if activeTab === 'prompt'}
								<!-- System Prompt Tab -->
								<div class="space-y-4">
									<div>
										<h3 class="text-sm font-medium text-foreground mb-3">System Prompt Configuration</h3>
										<div class="flex flex-wrap gap-2 mb-4">
											<button
												type="button"
												on:click={() => profileForm.system_prompt_type = 'preset'}
												class="px-4 py-2 text-sm rounded-lg transition-colors {profileForm.system_prompt_type === 'preset' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Claude Code + Append
											</button>
											<button
												type="button"
												on:click={() => profileForm.system_prompt_type = 'custom'}
												class="px-4 py-2 text-sm rounded-lg transition-colors {profileForm.system_prompt_type === 'custom' ? 'bg-primary text-primary-foreground' : 'bg-muted text-foreground hover:bg-muted/80'}"
											>
												Custom Prompt
											</button>
										</div>
									</div>

									{#if profileForm.system_prompt_type === 'preset'}
										<div>
											<label class="block text-xs text-muted-foreground mb-1.5">Append Instructions</label>
											<textarea
												bind:value={profileForm.system_prompt_append}
												class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground resize-y focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
												rows="8"
												placeholder="Additional instructions to append to Claude Code's system prompt..."
											></textarea>
											<p class="text-xs text-muted-foreground mt-2">These instructions will be added after Claude Code's built-in system prompt.</p>
										</div>
									{:else}
										<div>
											<label class="block text-xs text-muted-foreground mb-1.5">Custom System Prompt</label>
											<textarea
												bind:value={profileForm.system_prompt_content}
												class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground resize-y focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
												rows="10"
												placeholder="Enter your custom system prompt (can be blank)..."
											></textarea>
										</div>
										<label class="flex items-start gap-3 cursor-pointer group p-3 rounded-lg bg-muted/50 border border-border">
											<input
												type="checkbox"
												bind:checked={profileForm.system_prompt_inject_env}
												class="mt-0.5 w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
											/>
											<div>
												<span class="text-sm text-foreground group-hover:text-primary transition-colors">Inject Environment Details</span>
												<p class="text-xs text-muted-foreground">Adds working directory, platform, git status, and today's date</p>
											</div>
										</label>
									{/if}
								</div>

							{:else if activeTab === 'advanced'}
								<!-- Advanced Tab -->
								<div class="space-y-6">
									<div>
										<h3 class="text-sm font-medium text-foreground mb-3">Settings Sources</h3>
										<p class="text-xs text-muted-foreground mb-3">Load settings from filesystem locations</p>
										<div class="flex flex-wrap gap-4">
											{#each [
												{ id: 'user', label: 'User (~/.claude)' },
												{ id: 'project', label: 'Project (.claude)' },
												{ id: 'local', label: 'Local' }
											] as source}
												<label class="flex items-center gap-2 cursor-pointer">
													<input
														type="checkbox"
														checked={profileForm.setting_sources.includes(source.id)}
														on:change={() => toggleSettingSource(source.id)}
														class="w-4 h-4 rounded bg-muted border-border text-primary focus:ring-primary/50"
													/>
													<span class="text-sm text-foreground">{source.label}</span>
												</label>
											{/each}
										</div>
									</div>

									<div class="border-t border-border pt-6">
										<h3 class="text-sm font-medium text-foreground mb-3">Environment</h3>
										<div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">Working Directory</label>
												<input
													bind:value={profileForm.cwd}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
													placeholder="/workspace/my-project"
												/>
											</div>
											<div>
												<label class="block text-xs text-muted-foreground mb-1.5">User Identifier</label>
												<input
													bind:value={profileForm.user}
													class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
													placeholder="user@example.com"
												/>
											</div>
										</div>
										<div class="mt-4">
											<label class="block text-xs text-muted-foreground mb-1.5">Additional Directories</label>
											<input
												bind:value={profileForm.add_dirs}
												class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50 font-mono"
												placeholder="/extra/dir1, /extra/dir2 (comma-separated)"
											/>
										</div>
									</div>

									<div class="border-t border-border pt-6">
										<h3 class="text-sm font-medium text-foreground mb-3">Performance</h3>
										<div class="max-w-xs">
											<label class="block text-xs text-muted-foreground mb-1.5">Max Buffer Size (bytes)</label>
											<input
												type="number"
												bind:value={profileForm.max_buffer_size}
												class="w-full bg-muted border border-border rounded-lg px-3 py-2 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-primary/50"
												placeholder="Default"
											/>
										</div>
									</div>
								</div>
							{/if}
						</div>
					</div>
				</div>

				<!-- Form Footer -->
				<div class="px-4 sm:px-6 py-4 border-t border-border flex justify-end gap-2 shrink-0 bg-muted/30">
					<button
						on:click={() => { showForm = false; resetForm(); }}
						class="px-4 py-2.5 text-sm font-medium text-foreground bg-background border border-border rounded-xl hover:bg-muted transition-colors"
					>
						Cancel
					</button>
					<button
						on:click={saveProfile}
						disabled={!profileForm.id || !profileForm.name}
						class="px-6 py-2.5 text-sm font-medium bg-primary text-primary-foreground rounded-xl hover:bg-primary/90 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
					>
						{editingProfile ? 'Save Changes' : 'Create Profile'}
					</button>
				</div>

			{:else}
				<!-- Profile List View -->
				<div class="p-4 overflow-y-auto flex-1">
					<input type="file" accept=".json" bind:this={profileImportInput} on:change={handleImport} class="hidden" />

					{#if profiles.length === 0}
						<div class="text-center py-12">
							<svg class="w-16 h-16 mx-auto mb-4 text-muted-foreground/30" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
							</svg>
							<p class="text-muted-foreground">No profiles yet</p>
							<p class="text-xs text-muted-foreground mt-1">Create your first profile to get started</p>
						</div>
					{:else}
						<div class="grid gap-2">
							{#each profiles as profile}
								<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
								<div
									class="p-4 bg-background hover:bg-accent border border-border rounded-xl transition-all group cursor-pointer"
									on:click={() => !profile.is_builtin && openEditForm(profile)}
								>
									<div class="flex items-start justify-between gap-3">
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<span class="font-medium text-foreground">{profile.name}</span>
												{#if profile.is_builtin}
													<span class="text-[10px] px-1.5 py-0.5 bg-muted text-muted-foreground rounded">Built-in</span>
												{/if}
												{#if groups?.profiles?.assignments?.[profile.id]}
													<span class="text-[10px] px-1.5 py-0.5 bg-primary/10 text-primary rounded">{groups.profiles.assignments[profile.id]}</span>
												{/if}
											</div>
											<p class="text-xs text-muted-foreground mt-1 line-clamp-2">{profile.description || profile.id}</p>
										</div>
										<div class="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity" on:click|stopPropagation>
											<!-- Group dropdown -->
											<div class="relative group/dropdown">
												<button class="p-1.5 text-muted-foreground hover:text-foreground rounded hover:bg-muted" title="Assign to group">
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" />
													</svg>
												</button>
												<div class="absolute right-0 top-full mt-1 w-40 bg-card border border-border rounded-lg shadow-lg opacity-0 invisible group-hover/dropdown:opacity-100 group-hover/dropdown:visible transition-all z-50">
													<div class="py-1 max-h-48 overflow-y-auto">
														{#each groups?.profiles?.groups || [] as group}
															<button
																on:click={() => assignToGroup(profile.id, group.name)}
																class="w-full px-3 py-1.5 text-left text-sm hover:bg-muted transition-colors flex items-center gap-2"
															>
																{#if groups?.profiles?.assignments?.[profile.id] === group.name}
																	<svg class="w-3 h-3 text-primary" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																		<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
																	</svg>
																{:else}
																	<span class="w-3"></span>
																{/if}
																{group.name}
															</button>
														{/each}
														{#if groups?.profiles?.assignments?.[profile.id]}
															<button
																on:click={() => removeFromGroup(profile.id)}
																class="w-full px-3 py-1.5 text-left text-sm hover:bg-muted transition-colors text-muted-foreground"
															>
																<span class="ml-5">Remove</span>
															</button>
														{/if}
														<div class="border-t border-border my-1"></div>
														<button
															on:click={() => createGroup(profile.id)}
															class="w-full px-3 py-1.5 text-left text-sm hover:bg-muted transition-colors text-muted-foreground"
														>
															<span class="ml-5">+ New group</span>
														</button>
													</div>
												</div>
											</div>

											<button on:click={() => exportProfile(profile.id)} class="p-1.5 text-muted-foreground hover:text-foreground rounded hover:bg-muted" title="Export">
												<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
													<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
												</svg>
											</button>
											{#if !profile.is_builtin}
												<button on:click={() => deleteProfile(profile.id)} class="p-1.5 text-muted-foreground hover:text-destructive rounded hover:bg-muted" title="Delete">
													<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
														<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
													</svg>
												</button>
											{/if}
										</div>
									</div>
								</div>
							{/each}
						</div>
					{/if}
				</div>

				<!-- List Footer -->
				<div class="px-4 py-3 border-t border-border flex gap-2 shrink-0">
					<button
						on:click={openNewForm}
						class="flex-1 py-2.5 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors flex items-center justify-center gap-2"
					>
						<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						New Profile
					</button>
					<button
						on:click={triggerImport}
						disabled={profileImporting}
						class="px-4 py-2.5 border border-dashed border-border rounded-lg text-muted-foreground hover:text-foreground hover:border-primary/50 transition-colors flex items-center gap-2"
					>
						{#if profileImporting}
							<span class="animate-spin w-4 h-4 border-2 border-current border-t-transparent rounded-full"></span>
						{:else}
							<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
							</svg>
						{/if}
						Import
					</button>
				</div>
			{/if}
		</div>
	</div>
{/if}

<style>
	.scrollbar-hide {
		-ms-overflow-style: none;
		scrollbar-width: none;
	}
	.scrollbar-hide::-webkit-scrollbar {
		display: none;
	}
</style>
