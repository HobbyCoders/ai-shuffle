<script lang="ts">
	/**
	 * ProfileCard - Profile management in a card format
	 *
	 * Replaces ProfileModal with two view modes:
	 * - List view: Browse, search, and manage profiles
	 * - Form view: Edit or create profiles with tabbed interface
	 */
	import BaseCard from './BaseCard.svelte';
	import type { DeckCard } from './types';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile } from '$lib/api/client';
	import { profiles as profilesStore, tabs as tabsStore } from '$lib/stores/tabs';
	import { groups as groupsStore } from '$lib/stores/groups';
	import { plugins, installedPlugins, fileBasedAgents } from '$lib/stores/plugins';
	import type { PluginInfo, FileBasedAgent } from '$lib/api/plugins';
	import { PluginCard } from '$lib/components/plugins';

	// Types
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

	interface ToolsResponse {
		all_tools: { name: string; description: string }[];
		categories: { name: string; tools: { name: string; description: string }[] }[];
	}

	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
	}

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Props {
		card: DeckCard;
		onClose: () => void;
		onMinimize: () => void;
		onMaximize: () => void;
		onFocus: () => void;
		onMove: (x: number, y: number) => void;
		onResize: (w: number, h: number) => void;
		onDragEnd?: () => void;
		onResizeEnd?: () => void;
		mobile?: boolean;
	}

	let {
		card,
		onClose,
		onMinimize,
		onMaximize,
		onFocus,
		onMove,
		onResize,
		onDragEnd,
		onResizeEnd,
		mobile = false
	}: Props = $props();

	// Internal data state - loaded from stores/API
	let allSubagents = $state<Subagent[]>([]);
	let availableTools = $state<ToolsResponse>({ all_tools: [], categories: [] });
	let loading = $state(true);

	// Get profiles and groups from stores
	const profiles = $derived($profilesStore);
	const groups = $derived<GroupState>($groupsStore);

	// Load data on mount
	$effect(() => {
		loadData();
	});

	async function loadData() {
		loading = true;
		try {
			const [subagentsResult, toolsResult] = await Promise.all([
				api.get<Subagent[]>('/subagents').catch(() => []),
				api.get<ToolsResponse>('/tools').catch(() => ({ all_tools: [], categories: [] })),
				plugins.loadFileBasedAgents().catch(() => {})
			]);
			allSubagents = subagentsResult;
			availableTools = toolsResult;
		} catch (e) {
			console.error('Failed to load profile card data:', e);
		} finally {
			loading = false;
		}
	}

	// View state
	let viewMode = $state<'list' | 'form'>('list');
	let editingProfile = $state<Profile | null>(null);
	let activeTab = $state('general');
	let searchQuery = $state('');

	// Profile form state
	let profileForm = $state({
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
	});

	// Tool selection mode
	let toolSelectionMode = $state<'all' | 'allow' | 'disallow'>('all');
	let aiToolSelectionMode = $state<'all' | 'custom'>('custom');

	// AI Tools
	let aiToolCategories = $state<AIToolCategory[]>([]);
	let aiToolsLoading = $state(false);

	// Import handling
	let profileImportInput: HTMLInputElement | undefined = $state();
	let profileImporting = $state(false);

	// Plugin management
	let showPluginManager = $state(false);
	let pluginAgents = $derived($fileBasedAgents);
	let installedPluginsList = $derived($installedPlugins);

	// Tab definitions
	const tabs = [
		{ id: 'general', label: 'General', icon: 'settings' },
		{ id: 'tools', label: 'Tools', icon: 'wrench' },
		{ id: 'agents', label: 'Agents', icon: 'users' },
		{ id: 'prompt', label: 'Prompt', icon: 'message' },
		{ id: 'advanced', label: 'Advanced', icon: 'cog' }
	];

	// Filtered profiles based on search
	const filteredProfiles = $derived(
		searchQuery.trim()
			? profiles.filter(
					(p) =>
						p.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
						(p.description && p.description.toLowerCase().includes(searchQuery.toLowerCase())) ||
						p.id.toLowerCase().includes(searchQuery.toLowerCase())
				)
			: profiles
	);

	// Tool counts
	const selectedToolCount = $derived(
		toolSelectionMode === 'allow'
			? profileForm.allowed_tools.length
			: toolSelectionMode === 'disallow'
				? profileForm.disallowed_tools.length
				: availableTools.all_tools.length
	);

	const aiToolCount = $derived(
		aiToolSelectionMode === 'all' ? getAllAIToolIds().length : profileForm.enabled_ai_tools.length
	);

	// Load AI tools when form opens
	$effect(() => {
		if (viewMode === 'form') {
			loadAvailableAITools();
		}
	});

	async function loadAvailableAITools() {
		aiToolsLoading = true;
		try {
			const response = await api.get<{ categories: AIToolCategory[] }>(
				'/settings/ai-tools/available'
			);
			aiToolCategories = response.categories;
		} catch (e) {
			console.error('Failed to load AI tools:', e);
			aiToolCategories = [];
		}
		aiToolsLoading = false;
	}

	function getAllAIToolIds(): string[] {
		return aiToolCategories.flatMap((cat) => cat.tools.map((t) => t.id));
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
		viewMode = 'form';
	}

	function openEditForm(profile: Profile) {
		editingProfile = profile;
		const config = profile.config || {};
		const sp = (config.system_prompt || {}) as {
			type?: string;
			preset?: string;
			append?: string;
			content?: string;
			inject_env_details?: boolean;
		};
		const aiTools = (config as any).ai_tools || {};

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
			enabled_agents: (config as any).enabled_agents || [],
			include_partial_messages: config.include_partial_messages !== false,
			continue_conversation: (config as any).continue_conversation || false,
			fork_session: (config as any).fork_session || false,
			system_prompt_type: sp.type || 'preset',
			system_prompt_preset: sp.preset || 'claude_code',
			system_prompt_append: sp.append || '',
			system_prompt_content: sp.content || '',
			system_prompt_inject_env: sp.inject_env_details || false,
			enabled_ai_tools: enabledAITools,
			setting_sources: (config as any).setting_sources || [],
			cwd: (config as any).cwd || '',
			add_dirs: ((config as any).add_dirs || []).join(', '),
			user: (config as any).user || '',
			max_buffer_size: (config as any).max_buffer_size || null
		};

		viewMode = 'form';
		activeTab = 'general';

		// Check if all AI tools are enabled after loading
		loadAvailableAITools().then(() => {
			const allIds = getAllAIToolIds();
			if (
				enabledAITools.length === allIds.length &&
				allIds.every((id) => enabledAITools.includes(id))
			) {
				aiToolSelectionMode = 'all';
			} else {
				aiToolSelectionMode = 'custom';
			}
		});
	}

	function backToList() {
		viewMode = 'list';
		resetForm();
	}

	// Tool helpers
	function toggleTool(toolName: string) {
		if (toolSelectionMode === 'allow') {
			if (profileForm.allowed_tools.includes(toolName)) {
				profileForm.allowed_tools = profileForm.allowed_tools.filter((t) => t !== toolName);
			} else {
				profileForm.allowed_tools = [...profileForm.allowed_tools, toolName];
			}
		} else if (toolSelectionMode === 'disallow') {
			if (profileForm.disallowed_tools.includes(toolName)) {
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter((t) => t !== toolName);
			} else {
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, toolName];
			}
		}
	}

	function toggleCategory(category: { name: string; tools: { name: string }[] }) {
		const categoryToolNames = category.tools.map((t) => t.name);
		if (toolSelectionMode === 'allow') {
			const allSelected = categoryToolNames.every((name) =>
				profileForm.allowed_tools.includes(name)
			);
			if (allSelected) {
				profileForm.allowed_tools = profileForm.allowed_tools.filter(
					(t) => !categoryToolNames.includes(t)
				);
			} else {
				const newTools = categoryToolNames.filter(
					(name) => !profileForm.allowed_tools.includes(name)
				);
				profileForm.allowed_tools = [...profileForm.allowed_tools, ...newTools];
			}
		} else if (toolSelectionMode === 'disallow') {
			const allDisabled = categoryToolNames.every((name) =>
				profileForm.disallowed_tools.includes(name)
			);
			if (allDisabled) {
				profileForm.disallowed_tools = profileForm.disallowed_tools.filter(
					(t) => !categoryToolNames.includes(t)
				);
			} else {
				const newTools = categoryToolNames.filter(
					(name) => !profileForm.disallowed_tools.includes(name)
				);
				profileForm.disallowed_tools = [...profileForm.disallowed_tools, ...newTools];
			}
		}
	}

	function getCategorySelectionState(category: {
		name: string;
		tools: { name: string }[];
	}): 'all' | 'some' | 'none' {
		const categoryToolNames = category.tools.map((t) => t.name);
		const list =
			toolSelectionMode === 'allow' ? profileForm.allowed_tools : profileForm.disallowed_tools;
		const selectedCount = categoryToolNames.filter((name) => list.includes(name)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === categoryToolNames.length) return 'all';
		return 'some';
	}

	// AI Tool helpers
	function toggleAITool(toolId: string) {
		if (profileForm.enabled_ai_tools.includes(toolId)) {
			profileForm.enabled_ai_tools = profileForm.enabled_ai_tools.filter((t) => t !== toolId);
		} else {
			profileForm.enabled_ai_tools = [...profileForm.enabled_ai_tools, toolId];
		}
	}

	function toggleAIToolCategory(category: AIToolCategory) {
		const ids = category.tools.map((t) => t.id);
		const allSelected = ids.every((id) => profileForm.enabled_ai_tools.includes(id));
		if (allSelected) {
			profileForm.enabled_ai_tools = profileForm.enabled_ai_tools.filter((t) => !ids.includes(t));
		} else {
			const newTools = ids.filter((id) => !profileForm.enabled_ai_tools.includes(id));
			profileForm.enabled_ai_tools = [...profileForm.enabled_ai_tools, ...newTools];
		}
	}

	// Agent helpers
	function toggleAgent(agentId: string) {
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter((id) => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
		}
	}

	function toggleSettingSource(source: string) {
		if (profileForm.setting_sources.includes(source)) {
			profileForm.setting_sources = profileForm.setting_sources.filter((s) => s !== source);
		} else {
			profileForm.setting_sources = [...profileForm.setting_sources, source];
		}
	}

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku':
				return 'Haiku';
			case 'sonnet':
				return 'Sonnet';
			case 'sonnet-1m':
				return 'Sonnet 1M';
			case 'opus':
				return 'Opus';
			default:
				return model || 'Default';
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
			config.add_dirs = profileForm.add_dirs
				.split(',')
				.map((d) => d.trim())
				.filter(Boolean);
		}
		if (profileForm.user.trim()) config.user = profileForm.user;
		if (profileForm.max_buffer_size) config.max_buffer_size = profileForm.max_buffer_size;

		// AI tools
		const effectiveAITools =
			aiToolSelectionMode === 'all' ? getAllAIToolIds() : profileForm.enabled_ai_tools;
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

		const data = {
			id: profileForm.id.toLowerCase().replace(/[^a-z0-9-]/g, '-'),
			name: profileForm.name,
			description: profileForm.description || undefined,
			config
		};

		try {
			if (editingProfile) {
				await tabsStore.updateProfile(data.id, {
					name: data.name,
					description: data.description,
					config: data.config
				});
			} else {
				await tabsStore.createProfile(data);
			}
		} catch (e: any) {
			alert('Failed to save profile: ' + (e.detail || e.message || 'Unknown error'));
			return;
		}

		viewMode = 'list';
		resetForm();
	}

	// Export / Import
	async function handleExportProfile(profileId: string) {
		try {
			await exportAgent(profileId);
		} catch (e: any) {
			alert('Export failed: ' + (e.detail || 'Unknown error'));
		}
	}

	function triggerImport() {
		profileImportInput?.click();
	}

	async function handleImport(event: Event) {
		const input = event.target as HTMLInputElement;
		if (!input.files?.length) return;

		profileImporting = true;
		try {
			const file = input.files[0];
			const content = await file.text();
			const parsed = parseAgentExportFile(content);
			const profile = await importAgent(parsed);
			await tabsStore.loadProfiles();
			alert(`Successfully imported agent: ${profile.name}`);
		} catch (err: any) {
			alert('Import failed: ' + (err.detail || err.message || 'Invalid file format'));
		} finally {
			input.value = '';
			profileImporting = false;
		}
	}

	async function deleteProfile(profileId: string) {
		if (confirm('Delete this profile?')) {
			await tabsStore.deleteProfile(profileId);
		}
	}

	function assignToGroup(profileId: string, groupName: string) {
		groupsStore.assignToGroup('profiles', profileId, groupName);
	}

	function removeFromGroup(profileId: string) {
		groupsStore.removeFromGroup('profiles', profileId);
	}

	function createGroup(profileId: string) {
		const name = prompt('New group name:');
		if (name?.trim()) {
			groupsStore.createGroup('profiles', name.trim());
			groupsStore.assignToGroup('profiles', profileId, name.trim());
		}
	}
</script>

{#snippet cardContent()}
	<div class="profile-card-content">
		{#if viewMode === 'form'}
			<!-- Form View with Tabs -->
			<div class="form-container">
				<!-- Tab Navigation -->
				<nav class="tab-nav">
					<div class="tab-list">
						{#each tabs as tab}
							<button
								class="tab-btn"
								class:active={activeTab === tab.id}
								onclick={() => (activeTab = tab.id)}
							>
								{#if tab.icon === 'settings'}
									<svg class="tab-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
										/>
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
										/>
									</svg>
								{:else if tab.icon === 'wrench'}
									<svg class="tab-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"
										/>
									</svg>
								{:else if tab.icon === 'users'}
									<svg class="tab-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"
										/>
									</svg>
								{:else if tab.icon === 'message'}
									<svg class="tab-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z"
										/>
									</svg>
								{:else if tab.icon === 'cog'}
									<svg class="tab-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
										<path
											stroke-linecap="round"
											stroke-linejoin="round"
											stroke-width="2"
											d="M12 6V4m0 2a2 2 0 100 4m0-4a2 2 0 110 4m-6 8a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4m6 6v10m6-2a2 2 0 100-4m0 4a2 2 0 110-4m0 4v2m0-6V4"
										/>
									</svg>
								{/if}
								<span class="tab-label">{tab.label}</span>
							</button>
						{/each}
					</div>
				</nav>

				<!-- Tab Content -->
				<div class="tab-content">
					{#if activeTab === 'general'}
						<!-- General Tab -->
						<div class="tab-panel">
							<div class="form-section">
								<h3 class="section-title">Profile Information</h3>
								<div class="form-grid">
									<div class="form-field">
										<label class="field-label"
											>ID <span class="required">*</span></label
										>
										<input
											bind:value={profileForm.id}
											disabled={!!editingProfile}
											class="field-input"
											placeholder="my-profile"
										/>
									</div>
									<div class="form-field">
										<label class="field-label"
											>Name <span class="required">*</span></label
										>
										<input
											bind:value={profileForm.name}
											class="field-input"
											placeholder="My Profile"
										/>
									</div>
								</div>
								<div class="form-field full-width">
									<label class="field-label">Description</label>
									<input
										bind:value={profileForm.description}
										class="field-input"
										placeholder="Optional description"
									/>
								</div>
							</div>

							<div class="form-section">
								<h3 class="section-title">Core Settings</h3>
								<div class="form-grid cols-3">
									<div class="form-field">
										<label class="field-label">Model</label>
										<select bind:value={profileForm.model} class="field-input">
											<option value="sonnet">Sonnet</option>
											<option value="sonnet-1m">Sonnet 1M</option>
											<option value="opus">Opus</option>
											<option value="haiku">Haiku</option>
										</select>
									</div>
									<div class="form-field">
										<label class="field-label">Permission Mode</label>
										<select bind:value={profileForm.permission_mode} class="field-input">
											<option value="default">Default</option>
											<option value="acceptEdits">Accept Edits</option>
											<option value="plan">Plan</option>
											<option value="bypassPermissions">Bypass</option>
										</select>
									</div>
									<div class="form-field">
										<label class="field-label">Max Turns</label>
										<input
											type="number"
											bind:value={profileForm.max_turns}
											class="field-input"
											placeholder="Unlimited"
										/>
									</div>
								</div>
							</div>

							<div class="form-section">
								<h3 class="section-title">Behavior</h3>
								<div class="checkbox-group">
									<label class="checkbox-item">
										<input
											type="checkbox"
											bind:checked={profileForm.include_partial_messages}
											class="checkbox-input"
										/>
										<div class="checkbox-content">
											<span class="checkbox-label">Include Partial Messages</span>
											<p class="checkbox-desc">Stream partial text as it's being generated</p>
										</div>
									</label>
									<label class="checkbox-item">
										<input
											type="checkbox"
											bind:checked={profileForm.continue_conversation}
											class="checkbox-input"
										/>
										<div class="checkbox-content">
											<span class="checkbox-label">Continue Conversation</span>
											<p class="checkbox-desc">Automatically continue most recent conversation</p>
										</div>
									</label>
									<label class="checkbox-item">
										<input
											type="checkbox"
											bind:checked={profileForm.fork_session}
											class="checkbox-input"
										/>
										<div class="checkbox-content">
											<span class="checkbox-label">Fork Session</span>
											<p class="checkbox-desc">Create new session ID when resuming</p>
										</div>
									</label>
								</div>
							</div>
						</div>
					{:else if activeTab === 'tools'}
						<!-- Tools Tab -->
						<div class="tab-panel">
							<div class="form-section">
								<div class="section-header">
									<h3 class="section-title">Claude Tools</h3>
									<span class="badge primary">{availableTools.all_tools.length} available</span>
								</div>

								<!-- Tool Mode Selector -->
								<div class="mode-selector">
									<button
										type="button"
										class="mode-btn"
										class:active={toolSelectionMode === 'all'}
										onclick={() => {
											toolSelectionMode = 'all';
											profileForm.allowed_tools = [];
											profileForm.disallowed_tools = [];
										}}
									>
										All Tools
									</button>
									<button
										type="button"
										class="mode-btn"
										class:active={toolSelectionMode === 'allow'}
										onclick={() => {
											toolSelectionMode = 'allow';
											profileForm.disallowed_tools = [];
										}}
									>
										Allow Only
									</button>
									<button
										type="button"
										class="mode-btn"
										class:active={toolSelectionMode === 'disallow'}
										onclick={() => {
											toolSelectionMode = 'disallow';
											profileForm.allowed_tools = [];
										}}
									>
										Disallow
									</button>
								</div>

								<p class="helper-text">
									{#if toolSelectionMode === 'all'}
										Agent can use all available tools.
									{:else if toolSelectionMode === 'allow'}
										Agent can ONLY use selected tools.
									{:else}
										Agent can use all tools EXCEPT selected ones.
									{/if}
								</p>

								{#if toolSelectionMode !== 'all'}
									<div class="tools-list">
										{#each availableTools.categories as category}
											{#if category.tools.length > 0}
												{@const state = getCategorySelectionState(category)}
												<div class="tool-category">
													<button
														type="button"
														class="category-header"
														onclick={() => toggleCategory(category)}
													>
														<div
															class="category-checkbox"
															class:checked={state !== 'none'}
															class:partial={state === 'some'}
														>
															{#if state === 'all'}
																<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																	<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" />
																</svg>
															{:else if state === 'some'}
																<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
																	<path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" />
																</svg>
															{/if}
														</div>
														<span class="category-name">{category.name}</span>
														<span class="category-count">{category.tools.length}</span>
													</button>
													<div class="category-tools">
														{#each category.tools as tool}
															{@const isSelected =
																toolSelectionMode === 'allow'
																	? profileForm.allowed_tools.includes(tool.name)
																	: profileForm.disallowed_tools.includes(tool.name)}
															<label class="tool-item">
																<input
																	type="checkbox"
																	checked={isSelected}
																	onchange={() => toggleTool(tool.name)}
																	class="tool-checkbox"
																/>
																<span class="tool-name" title={tool.description}>{tool.name}</span>
															</label>
														{/each}
													</div>
												</div>
											{/if}
										{/each}
									</div>

									<div class="selection-count">
										{#if toolSelectionMode === 'allow'}
											{profileForm.allowed_tools.length} tool{profileForm.allowed_tools.length !== 1 ? 's' : ''} allowed
										{:else}
											{profileForm.disallowed_tools.length} tool{profileForm.disallowed_tools.length !== 1 ? 's' : ''} blocked
										{/if}
									</div>
								{/if}
							</div>

							<!-- AI Tools Section -->
							<div class="form-section">
								<div class="section-header">
									<h3 class="section-title">AI Tools</h3>
									{#if !aiToolsLoading}
										<span class="badge violet">{aiToolCount} enabled</span>
									{/if}
								</div>

								<div class="mode-selector">
									<button
										type="button"
										class="mode-btn violet"
										class:active={aiToolSelectionMode === 'all'}
										onclick={() => (aiToolSelectionMode = 'all')}
									>
										All Tools
									</button>
									<button
										type="button"
										class="mode-btn violet"
										class:active={aiToolSelectionMode === 'custom'}
										onclick={() => (aiToolSelectionMode = 'custom')}
									>
										Custom
									</button>
								</div>

								{#if aiToolSelectionMode === 'custom'}
									{#if aiToolsLoading}
										<div class="loading-state">Loading AI tools...</div>
									{:else if aiToolCategories.length === 0}
										<div class="empty-state">
											No AI tools available. Configure API keys in Settings.
										</div>
									{:else}
										<div class="ai-tools-list">
											{#each aiToolCategories as category}
												{#if category.tools.length > 0}
													<div class="ai-category">
														<div class="ai-category-header">
															<span class="ai-category-name">{category.name}</span>
															<button
																type="button"
																class="toggle-all-btn"
																onclick={() => toggleAIToolCategory(category)}
															>
																Toggle all
															</button>
														</div>
														<div class="ai-tools-grid">
															{#each category.tools as tool}
																{@const isSelected = profileForm.enabled_ai_tools.includes(tool.id)}
																<label
																	class="ai-tool-item"
																	class:disabled={!tool.available}
																>
																	<input
																		type="checkbox"
																		checked={isSelected}
																		disabled={!tool.available}
																		onchange={() => toggleAITool(tool.id)}
																		class="ai-tool-checkbox"
																	/>
																	<div class="ai-tool-info">
																		<div class="ai-tool-header">
																			<span class="ai-tool-name">{tool.name}</span>
																			{#if !tool.available}
																				<span class="badge warning small">No API key</span>
																			{:else if tool.active_provider}
																				<span class="badge success small">{tool.active_provider}</span>
																			{/if}
																		</div>
																		<p class="ai-tool-desc">{tool.description}</p>
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
									<p class="helper-text">
										All configured AI tools are enabled. Configure API keys in Settings.
									</p>
								{/if}
							</div>
						</div>
					{:else if activeTab === 'agents'}
						<!-- Agents Tab -->
						<div class="tab-panel">
							<!-- Database Subagents Section -->
							<div class="form-section">
								<div class="section-header">
									<h3 class="section-title">Database Subagents</h3>
									{#if allSubagents.length > 0}
										<div class="header-actions">
											<button
												type="button"
												class="text-btn muted"
												onclick={() => (profileForm.enabled_agents = [])}
											>
												Clear
											</button>
											<button
												type="button"
												class="text-btn primary"
												onclick={() =>
													(profileForm.enabled_agents = allSubagents.map((a) => a.id))}
											>
												Select All
											</button>
										</div>
									{/if}
								</div>

								{#if allSubagents.length === 0}
									<div class="empty-state">
										<p class="empty-hint">No database subagents configured</p>
									</div>
								{:else}
									<p class="helper-text">Select which subagents this profile can use</p>
									<div class="agents-grid">
										{#each allSubagents as agent (agent.id)}
											{@const isSelected = profileForm.enabled_agents.includes(agent.id)}
											<label class="agent-item" class:selected={isSelected}>
												<input
													type="checkbox"
													checked={isSelected}
													onchange={() => toggleAgent(agent.id)}
													class="agent-checkbox"
												/>
												<div class="agent-info">
													<div class="agent-header">
														<span class="agent-name">{agent.name}</span>
														<span class="badge primary small">{getModelDisplay(agent.model)}</span>
													</div>
													<p class="agent-desc">{agent.description}</p>
												</div>
											</label>
										{/each}
									</div>
									<div class="selection-count">
										{profileForm.enabled_agents.length} of {allSubagents.length} enabled
									</div>
								{/if}
							</div>

							<!-- Plugin Agents Section (Read-only) -->
							<div class="form-section">
								<div class="section-header">
									<h3 class="section-title">Plugin Agents</h3>
									<button
										type="button"
										class="text-btn primary"
										onclick={() => (showPluginManager = true)}
									>
										Manage Plugins
									</button>
								</div>

								{#if pluginAgents.length === 0}
									<div class="empty-state">
										<p class="empty-hint">No plugin agents available</p>
										<p class="empty-hint small">Install and enable plugins to get agents</p>
									</div>
								{:else}
									<p class="helper-text">Agents from enabled plugins (managed via plugin settings)</p>
									<div class="plugin-agents-list">
										{#each pluginAgents as agent (agent.id)}
											<div class="plugin-agent-item">
												<div class="plugin-agent-info">
													<div class="plugin-agent-header">
														<span class="plugin-agent-name">{agent.name}</span>
														<span class="badge muted small">{agent.plugin_name}</span>
														{#if agent.model}
															<span class="badge primary small">{getModelDisplay(agent.model)}</span>
														{/if}
													</div>
													<p class="plugin-agent-desc">{agent.description}</p>
												</div>
												{#if agent.tools && agent.tools.length > 0}
													<div class="plugin-agent-tools">
														{#each agent.tools.slice(0, 3) as tool}
															<span class="tool-badge">{tool}</span>
														{/each}
														{#if agent.tools.length > 3}
															<span class="tool-badge more">+{agent.tools.length - 3}</span>
														{/if}
													</div>
												{/if}
											</div>
										{/each}
									</div>
									<div class="selection-count">
										{pluginAgents.length} plugin agent{pluginAgents.length !== 1 ? 's' : ''} available
									</div>
								{/if}
							</div>

							<!-- Installed Plugins Quick View -->
							<div class="form-section">
								<div class="section-header">
									<h3 class="section-title">Installed Plugins</h3>
									<span class="badge muted">{installedPluginsList.length} installed</span>
								</div>

								{#if installedPluginsList.length === 0}
									<div class="empty-state">
										<p class="empty-hint">No plugins installed</p>
										<button
											type="button"
											class="btn secondary small"
											onclick={() => (showPluginManager = true)}
										>
											Browse Plugins
										</button>
									</div>
								{:else}
									<div class="installed-plugins-compact">
										{#each installedPluginsList.slice(0, 6) as plugin (plugin.id)}
											<div class="plugin-compact-item" class:disabled={!plugin.enabled}>
												<div class="plugin-compact-info">
													<span class="plugin-compact-name">{plugin.name}</span>
													<div class="plugin-compact-badges">
														{#if plugin.has_agents}
															<span class="feature-dot agents" title="Has agents"></span>
														{/if}
														{#if plugin.has_commands}
															<span class="feature-dot commands" title="Has commands"></span>
														{/if}
														{#if plugin.has_skills}
															<span class="feature-dot skills" title="Has skills"></span>
														{/if}
													</div>
												</div>
												<span class="plugin-status" class:enabled={plugin.enabled}>
													{plugin.enabled ? 'On' : 'Off'}
												</span>
											</div>
										{/each}
										{#if installedPluginsList.length > 6}
											<button
												type="button"
												class="show-more-btn"
												onclick={() => (showPluginManager = true)}
											>
												+{installedPluginsList.length - 6} more plugins
											</button>
										{/if}
									</div>
									<div class="plugins-footer">
										<button
											type="button"
											class="btn secondary small"
											onclick={() => (showPluginManager = true)}
										>
											Open Plugin Manager
										</button>
									</div>
								{/if}
							</div>
						</div>
					{:else if activeTab === 'prompt'}
						<!-- System Prompt Tab -->
						<div class="tab-panel">
							<div class="form-section">
								<h3 class="section-title">System Prompt Configuration</h3>
								<div class="mode-selector large">
									<button
										type="button"
										class="mode-btn"
										class:active={profileForm.system_prompt_type === 'preset'}
										onclick={() => (profileForm.system_prompt_type = 'preset')}
									>
										Claude Code + Append
									</button>
									<button
										type="button"
										class="mode-btn"
										class:active={profileForm.system_prompt_type === 'custom'}
										onclick={() => (profileForm.system_prompt_type = 'custom')}
									>
										Custom Prompt
									</button>
								</div>

								{#if profileForm.system_prompt_type === 'preset'}
									<div class="form-field full-width">
										<label class="field-label">Append Instructions</label>
										<textarea
											bind:value={profileForm.system_prompt_append}
											class="field-textarea"
											rows="8"
											placeholder="Additional instructions to append to Claude Code's system prompt..."
										></textarea>
										<p class="field-hint">
											These instructions will be added after Claude Code's built-in system prompt.
										</p>
									</div>
								{:else}
									<div class="form-field full-width">
										<label class="field-label">Custom System Prompt</label>
										<textarea
											bind:value={profileForm.system_prompt_content}
											class="field-textarea"
											rows="10"
											placeholder="Enter your custom system prompt (can be blank)..."
										></textarea>
									</div>
									<label class="checkbox-item highlighted">
										<input
											type="checkbox"
											bind:checked={profileForm.system_prompt_inject_env}
											class="checkbox-input"
										/>
										<div class="checkbox-content">
											<span class="checkbox-label">Inject Environment Details</span>
											<p class="checkbox-desc">
												Adds working directory, platform, git status, and today's date
											</p>
										</div>
									</label>
								{/if}
							</div>
						</div>
					{:else if activeTab === 'advanced'}
						<!-- Advanced Tab -->
						<div class="tab-panel">
							<div class="form-section">
								<h3 class="section-title">Settings Sources</h3>
								<p class="helper-text">Load settings from filesystem locations</p>
								<div class="inline-checkboxes">
									{#each [
										{ id: 'user', label: 'User (~/.claude)' },
										{ id: 'project', label: 'Project (.claude)' },
										{ id: 'local', label: 'Local' }
									] as source}
										<label class="inline-checkbox">
											<input
												type="checkbox"
												checked={profileForm.setting_sources.includes(source.id)}
												onchange={() => toggleSettingSource(source.id)}
												class="checkbox-input"
											/>
											<span class="checkbox-label">{source.label}</span>
										</label>
									{/each}
								</div>
							</div>

							<div class="form-section">
								<h3 class="section-title">Environment</h3>
								<div class="form-grid">
									<div class="form-field">
										<label class="field-label">Working Directory</label>
										<input
											bind:value={profileForm.cwd}
											class="field-input mono"
											placeholder="/workspace/my-project"
										/>
									</div>
									<div class="form-field">
										<label class="field-label">User Identifier</label>
										<input
											bind:value={profileForm.user}
											class="field-input"
											placeholder="user@example.com"
										/>
									</div>
								</div>
								<div class="form-field full-width">
									<label class="field-label">Additional Directories</label>
									<input
										bind:value={profileForm.add_dirs}
										class="field-input mono"
										placeholder="/extra/dir1, /extra/dir2 (comma-separated)"
									/>
								</div>
							</div>

							<div class="form-section">
								<h3 class="section-title">Performance</h3>
								<div class="form-field" style="max-width: 200px;">
									<label class="field-label">Max Buffer Size (bytes)</label>
									<input
										type="number"
										bind:value={profileForm.max_buffer_size}
										class="field-input"
										placeholder="Default"
									/>
								</div>
							</div>
						</div>
					{/if}
				</div>

				<!-- Form Footer -->
				<div class="form-footer">
					<button type="button" class="btn secondary" onclick={backToList}>Cancel</button>
					<button
						type="button"
						class="btn primary"
						disabled={!profileForm.id || !profileForm.name}
						onclick={saveProfile}
					>
						{editingProfile ? 'Save Changes' : 'Create Profile'}
					</button>
				</div>
			</div>
		{:else}
			<!-- List View -->
			<div class="list-container">
				<input
					type="file"
					accept=".json"
					bind:this={profileImportInput}
					onchange={handleImport}
					class="hidden"
				/>

				<!-- Search -->
				<div class="search-bar">
					<svg class="search-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
						/>
					</svg>
					<input
						type="text"
						bind:value={searchQuery}
						class="search-input"
						placeholder="Search profiles..."
					/>
				</div>

				<!-- Profile List -->
				<div class="profile-list">
					{#if filteredProfiles.length === 0}
						<div class="empty-state large">
							{#if searchQuery}
								<p>No profiles match your search</p>
							{:else}
								<svg class="empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										stroke-width="1.5"
										d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
									/>
								</svg>
								<p>No profiles yet</p>
								<p class="empty-hint">Create your first profile to get started</p>
							{/if}
						</div>
					{:else}
						{#each filteredProfiles as profile}
							<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
							<div
								class="profile-item"
								onclick={() => !profile.is_builtin && openEditForm(profile)}
							>
								<div class="profile-info">
									<div class="profile-header">
										<span class="profile-name">{profile.name}</span>
										{#if profile.is_builtin}
											<span class="badge muted">Built-in</span>
										{/if}
										{#if groups?.profiles?.assignments?.[profile.id]}
											<span class="badge primary"
												>{groups.profiles.assignments[profile.id]}</span
											>
										{/if}
									</div>
									<p class="profile-desc">{profile.description || profile.id}</p>
								</div>
								<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
								<div class="profile-actions" onclick={(e) => e.stopPropagation()}>
									<!-- Group dropdown -->
									<div class="dropdown-container">
										<button class="action-btn" title="Assign to group">
											<svg class="action-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													stroke-width="2"
													d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"
												/>
											</svg>
										</button>
										<div class="dropdown-menu">
											{#each groups?.profiles?.groups || [] as group}
												<button
													class="dropdown-item"
													onclick={() => assignToGroup(profile.id, group.name)}
												>
													{#if groups?.profiles?.assignments?.[profile.id] === group.name}
														<svg class="check-icon small" fill="none" stroke="currentColor" viewBox="0 0 24 24">
															<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
														</svg>
													{:else}
														<span class="spacer"></span>
													{/if}
													{group.name}
												</button>
											{/each}
											{#if groups?.profiles?.assignments?.[profile.id]}
												<button
													class="dropdown-item muted"
													onclick={() => removeFromGroup(profile.id)}
												>
													<span class="spacer"></span>
													Remove
												</button>
											{/if}
											<div class="dropdown-divider"></div>
											<button
												class="dropdown-item muted"
												onclick={() => createGroup(profile.id)}
											>
												<span class="spacer"></span>
												+ New group
											</button>
										</div>
									</div>

									<button
										class="action-btn"
										title="Export"
										onclick={() => handleExportProfile(profile.id)}
									>
										<svg class="action-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
											<path
												stroke-linecap="round"
												stroke-linejoin="round"
												stroke-width="2"
												d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"
											/>
										</svg>
									</button>
									{#if !profile.is_builtin}
										<button
											class="action-btn danger"
											title="Delete"
											onclick={() => deleteProfile(profile.id)}
										>
											<svg class="action-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
												<path
													stroke-linecap="round"
													stroke-linejoin="round"
													stroke-width="2"
													d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
												/>
											</svg>
										</button>
									{/if}
								</div>
							</div>
						{/each}
					{/if}
				</div>

				<!-- List Footer -->
				<div class="list-footer">
					<button class="btn new-profile" onclick={openNewForm}>
						<svg class="btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
						</svg>
						New Profile
					</button>
					<button class="btn import" onclick={triggerImport} disabled={profileImporting}>
						{#if profileImporting}
							<span class="spinner"></span>
						{:else}
							<svg class="btn-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12"
								/>
							</svg>
						{/if}
						Import
					</button>
				</div>
			</div>
		{/if}
	</div>
{/snippet}

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	<div class="profile-card-mobile">
		{@render cardContent()}
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard
		{card}
		{onClose}
		{onMinimize}
		{onMaximize}
		{onFocus}
		{onMove}
		{onResize}
		{onDragEnd}
		{onResizeEnd}
	>
		{@render cardContent()}
	</BaseCard>
{/if}

<!-- Plugin Manager Modal -->
{#if showPluginManager}
	<div class="plugin-modal-overlay" onclick={() => (showPluginManager = false)}>
		<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
		<div class="plugin-modal-container" onclick={(e) => e.stopPropagation()}>
			<div class="plugin-modal-header">
				<h2>Plugin Manager</h2>
				<button class="close-btn" onclick={() => (showPluginManager = false)}>
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
					</svg>
				</button>
			</div>
			<div class="plugin-modal-content">
				<PluginCard />
			</div>
		</div>
	</div>
{/if}

<style>
	.profile-card-content {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
	}

	.profile-card-mobile {
		height: 100%;
		display: flex;
		flex-direction: column;
		background: var(--card);
	}

	.hidden {
		display: none;
	}

	/* List View */
	.list-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.search-bar {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 12px;
		border-bottom: 1px solid hsl(var(--border));
		flex-shrink: 0;
	}

	.search-icon {
		width: 18px;
		height: 18px;
		color: hsl(var(--muted-foreground));
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		background: transparent;
		border: none;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		outline: none;
	}

	.search-input::placeholder {
		color: hsl(var(--muted-foreground));
	}

	.profile-list {
		flex: 1;
		overflow-y: auto;
		padding: 8px;
	}

	.profile-item {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 12px;
		padding: 12px;
		background: hsl(var(--background));
		border: 1px solid hsl(var(--border));
		border-radius: 12px;
		margin-bottom: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.profile-item:hover {
		background: hsl(var(--accent));
		border-color: hsl(var(--primary) / 0.3);
	}

	.profile-info {
		flex: 1;
		min-width: 0;
	}

	.profile-header {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.profile-name {
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.profile-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	.profile-actions {
		display: flex;
		align-items: center;
		gap: 2px;
		opacity: 0;
		transition: opacity 0.15s ease;
	}

	.profile-item:hover .profile-actions {
		opacity: 1;
	}

	.action-btn {
		padding: 6px;
		background: transparent;
		border: none;
		border-radius: 6px;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.action-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.action-btn.danger:hover {
		color: hsl(var(--destructive));
	}

	.action-icon {
		width: 16px;
		height: 16px;
	}

	/* Dropdown */
	.dropdown-container {
		position: relative;
	}

	.dropdown-menu {
		position: absolute;
		right: 0;
		top: 100%;
		margin-top: 4px;
		min-width: 160px;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
		opacity: 0;
		visibility: hidden;
		transition: all 0.15s ease;
		z-index: 50;
		max-height: 200px;
		overflow-y: auto;
	}

	.dropdown-container:hover .dropdown-menu {
		opacity: 1;
		visibility: visible;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: 8px;
		width: 100%;
		padding: 8px 12px;
		background: transparent;
		border: none;
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		text-align: left;
		transition: background 0.1s ease;
	}

	.dropdown-item:hover {
		background: hsl(var(--muted));
	}

	.dropdown-item.muted {
		color: hsl(var(--muted-foreground));
	}

	.dropdown-divider {
		height: 1px;
		background: hsl(var(--border));
		margin: 4px 0;
	}

	.spacer {
		width: 12px;
	}

	/* List Footer */
	.list-footer {
		display: flex;
		gap: 8px;
		padding: 12px;
		border-top: 1px solid hsl(var(--border));
		flex-shrink: 0;
	}

	.btn {
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 10px 16px;
		border-radius: 10px;
		font-size: 0.875rem;
		font-weight: 500;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.btn.new-profile {
		flex: 1;
		background: transparent;
		border: 1px dashed hsl(var(--border));
		color: hsl(var(--muted-foreground));
	}

	.btn.new-profile:hover {
		color: hsl(var(--foreground));
		border-color: hsl(var(--primary) / 0.5);
	}

	.btn.import {
		background: transparent;
		border: 1px dashed hsl(var(--border));
		color: hsl(var(--muted-foreground));
	}

	.btn.import:hover {
		color: hsl(var(--foreground));
		border-color: hsl(var(--primary) / 0.5);
	}

	.btn.import:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.btn-icon {
		width: 16px;
		height: 16px;
	}

	/* Form View */
	.form-container {
		display: flex;
		flex-direction: column;
		height: 100%;
		overflow: hidden;
	}

	.tab-nav {
		flex-shrink: 0;
		border-bottom: 1px solid hsl(var(--border));
		background: hsl(var(--muted) / 0.3);
	}

	.tab-list {
		display: flex;
		overflow-x: auto;
		scrollbar-width: none;
	}

	.tab-list::-webkit-scrollbar {
		display: none;
	}

	.tab-btn {
		flex: 1;
		display: flex;
		align-items: center;
		justify-content: center;
		gap: 6px;
		padding: 12px 16px;
		background: transparent;
		border: none;
		border-bottom: 2px solid transparent;
		color: hsl(var(--muted-foreground));
		font-size: 0.8125rem;
		font-weight: 500;
		white-space: nowrap;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.tab-btn:hover {
		color: hsl(var(--foreground));
	}

	.tab-btn.active {
		color: hsl(var(--primary));
		border-bottom-color: hsl(var(--primary));
	}

	.tab-icon {
		width: 16px;
		height: 16px;
	}

	.tab-label {
		display: none;
	}

	@media (min-width: 480px) {
		.tab-label {
			display: inline;
		}
	}

	.tab-content {
		flex: 1;
		overflow-y: auto;
	}

	.tab-panel {
		padding: 16px;
	}

	/* Form Sections */
	.form-section {
		margin-bottom: 24px;
	}

	.form-section:last-child {
		margin-bottom: 0;
	}

	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 12px;
	}

	.section-title {
		font-size: 0.875rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin: 0 0 12px 0;
	}

	.section-header .section-title {
		margin: 0;
	}

	.header-actions {
		display: flex;
		gap: 12px;
	}

	.text-btn {
		background: transparent;
		border: none;
		font-size: 0.75rem;
		cursor: pointer;
		transition: color 0.15s ease;
	}

	.text-btn.muted {
		color: hsl(var(--muted-foreground));
	}

	.text-btn.muted:hover {
		color: hsl(var(--foreground));
	}

	.text-btn.primary {
		color: hsl(var(--primary));
	}

	.text-btn.primary:hover {
		text-decoration: underline;
	}

	/* Form Grid */
	.form-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 12px;
	}

	.form-grid.cols-3 {
		grid-template-columns: repeat(3, 1fr);
	}

	@media (max-width: 480px) {
		.form-grid,
		.form-grid.cols-3 {
			grid-template-columns: 1fr;
		}
	}

	.form-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.form-field.full-width {
		grid-column: 1 / -1;
	}

	.field-label {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	.required {
		color: hsl(var(--destructive));
	}

	.field-input {
		width: 100%;
		padding: 8px 12px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		outline: none;
		transition: border-color 0.15s ease;
	}

	.field-input:focus {
		border-color: hsl(var(--primary));
	}

	.field-input:disabled {
		opacity: 0.5;
	}

	.field-input.mono {
		font-family: monospace;
	}

	.field-textarea {
		width: 100%;
		padding: 8px 12px;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		font-family: monospace;
		resize: vertical;
		outline: none;
		transition: border-color 0.15s ease;
	}

	.field-textarea:focus {
		border-color: hsl(var(--primary));
	}

	.field-hint {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 4px;
	}

	/* Checkboxes */
	.checkbox-group {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.checkbox-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		cursor: pointer;
	}

	.checkbox-item.highlighted {
		padding: 12px;
		background: hsl(var(--muted) / 0.5);
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
	}

	.checkbox-input {
		margin-top: 2px;
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	.checkbox-content {
		flex: 1;
	}

	.checkbox-label {
		font-size: 0.875rem;
		color: hsl(var(--foreground));
	}

	.checkbox-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 2px;
	}

	.inline-checkboxes {
		display: flex;
		flex-wrap: wrap;
		gap: 16px;
	}

	.inline-checkbox {
		display: flex;
		align-items: center;
		gap: 8px;
		cursor: pointer;
	}

	/* Mode Selector */
	.mode-selector {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		margin-bottom: 12px;
	}

	.mode-selector.large .mode-btn {
		padding: 10px 16px;
		font-size: 0.875rem;
	}

	.mode-btn {
		padding: 6px 12px;
		background: hsl(var(--muted));
		border: none;
		border-radius: 8px;
		font-size: 0.75rem;
		color: hsl(var(--foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.mode-btn:hover {
		background: hsl(var(--muted) / 0.8);
	}

	.mode-btn.active {
		background: hsl(var(--primary));
		color: hsl(var(--primary-foreground));
	}

	.mode-btn.violet.active {
		background: hsl(280 80% 55%);
	}

	/* Helper Text */
	.helper-text {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-bottom: 12px;
	}

	/* Tools List */
	.tools-list {
		max-height: 256px;
		overflow-y: auto;
		padding-right: 8px;
	}

	.tool-category {
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		overflow: hidden;
		margin-bottom: 8px;
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 10px 12px;
		background: hsl(var(--muted) / 0.5);
		border: none;
		color: hsl(var(--foreground));
		font-size: 0.875rem;
		cursor: pointer;
		transition: background 0.1s ease;
	}

	.category-header:hover {
		background: hsl(var(--muted));
	}

	.category-checkbox {
		width: 16px;
		height: 16px;
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: hsl(var(--muted));
		border: 1px solid hsl(var(--border));
		transition: all 0.15s ease;
	}

	.category-checkbox.checked {
		background: hsl(var(--primary));
		border-color: hsl(var(--primary));
	}

	.category-checkbox.partial {
		background: hsl(var(--primary));
		border-color: hsl(var(--primary));
	}

	.check-icon {
		width: 12px;
		height: 12px;
		color: hsl(var(--primary-foreground));
	}

	.check-icon.small {
		width: 12px;
		height: 12px;
		color: hsl(var(--primary));
	}

	.category-name {
		flex: 1;
		text-align: left;
		font-weight: 500;
	}

	.category-count {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
	}

	.category-tools {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 4px;
		padding: 8px;
		background: hsl(var(--card));
	}

	@media (max-width: 480px) {
		.category-tools {
			grid-template-columns: 1fr;
		}
	}

	.tool-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: 6px;
		cursor: pointer;
		transition: background 0.1s ease;
	}

	.tool-item:hover {
		background: hsl(var(--muted) / 0.5);
	}

	.tool-checkbox {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	.tool-name {
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.selection-count {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 12px;
		padding-top: 12px;
		border-top: 1px solid hsl(var(--border));
	}

	/* AI Tools */
	.ai-tools-list {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.ai-category {
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		padding: 12px;
	}

	.ai-category-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: 12px;
	}

	.ai-category-name {
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.toggle-all-btn {
		background: transparent;
		border: none;
		font-size: 0.75rem;
		color: hsl(var(--primary));
		cursor: pointer;
	}

	.toggle-all-btn:hover {
		text-decoration: underline;
	}

	.ai-tools-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 8px;
	}

	@media (max-width: 480px) {
		.ai-tools-grid {
			grid-template-columns: 1fr;
		}
	}

	.ai-tool-item {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 8px;
		border-radius: 8px;
		cursor: pointer;
		transition: background 0.1s ease;
	}

	.ai-tool-item:hover {
		background: hsl(var(--muted) / 0.5);
	}

	.ai-tool-item.disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.ai-tool-checkbox {
		margin-top: 2px;
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	.ai-tool-info {
		flex: 1;
		min-width: 0;
	}

	.ai-tool-header {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.ai-tool-name {
		font-size: 0.875rem;
		color: hsl(var(--foreground));
	}

	.ai-tool-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 2px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Agents Grid */
	.agents-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 8px;
		max-height: 320px;
		overflow-y: auto;
		padding-right: 4px;
	}

	@media (max-width: 480px) {
		.agents-grid {
			grid-template-columns: 1fr;
		}
	}

	.agent-item {
		display: flex;
		align-items: flex-start;
		gap: 12px;
		padding: 12px;
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.agent-item:hover {
		border-color: hsl(var(--primary) / 0.5);
	}

	.agent-item.selected {
		border-color: hsl(var(--primary));
		background: hsl(var(--primary) / 0.05);
	}

	.agent-checkbox {
		margin-top: 2px;
		width: 16px;
		height: 16px;
		flex-shrink: 0;
	}

	.agent-info {
		flex: 1;
		min-width: 0;
	}

	.agent-header {
		display: flex;
		align-items: center;
		gap: 6px;
	}

	.agent-name {
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.agent-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	/* Badges */
	.badge {
		display: inline-flex;
		align-items: center;
		padding: 2px 8px;
		font-size: 0.625rem;
		font-weight: 500;
		border-radius: 9999px;
	}

	.badge.primary {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	.badge.muted {
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
	}

	.badge.violet {
		background: hsl(280 80% 55% / 0.1);
		color: hsl(280 80% 55%);
	}

	.badge.success {
		background: hsl(142 76% 36% / 0.2);
		color: hsl(142 76% 36%);
	}

	.badge.warning {
		background: hsl(38 92% 50% / 0.2);
		color: hsl(38 92% 50%);
	}

	.badge.small {
		padding: 1px 6px;
		font-size: 0.5625rem;
	}

	/* Empty State */
	.empty-state {
		text-align: center;
		padding: 24px;
		color: hsl(var(--muted-foreground));
	}

	.empty-state.large {
		padding: 48px 24px;
	}

	.empty-icon {
		width: 48px;
		height: 48px;
		margin: 0 auto 12px;
		color: hsl(var(--muted-foreground) / 0.5);
	}

	.empty-hint {
		font-size: 0.75rem;
		margin-top: 4px;
	}

	.loading-state {
		text-align: center;
		padding: 32px;
		color: hsl(var(--muted-foreground));
		font-size: 0.875rem;
	}

	/* Form Footer */
	.form-footer {
		display: flex;
		justify-content: flex-end;
		gap: 8px;
		padding: 12px 16px;
		border-top: 1px solid hsl(var(--border));
		background: hsl(var(--muted) / 0.3);
		flex-shrink: 0;
	}

	.btn.secondary {
		background: hsl(var(--background));
		border: 1px solid hsl(var(--border));
		color: hsl(var(--foreground));
	}

	.btn.secondary:hover {
		background: hsl(var(--muted));
	}

	.btn.primary {
		background: hsl(var(--primary));
		border: none;
		color: hsl(var(--primary-foreground));
	}

	.btn.primary:hover {
		background: hsl(var(--primary) / 0.9);
	}

	.btn.primary:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	/* Spinner */
	.spinner {
		width: 16px;
		height: 16px;
		border: 2px solid currentColor;
		border-top-color: transparent;
		border-radius: 50%;
		animation: spin 0.8s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	/* Plugin Agent Styles */
	.plugin-agents-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		max-height: 200px;
		overflow-y: auto;
	}

	.plugin-agent-item {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: 10px 12px;
		background: hsl(var(--muted) / 0.3);
		border: 1px solid hsl(var(--border));
		border-radius: 8px;
	}

	.plugin-agent-info {
		flex: 1;
	}

	.plugin-agent-header {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-wrap: wrap;
	}

	.plugin-agent-name {
		font-size: 0.875rem;
		font-weight: 500;
		color: hsl(var(--foreground));
	}

	.plugin-agent-desc {
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		margin-top: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	.plugin-agent-tools {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
	}

	.tool-badge {
		display: inline-flex;
		padding: 2px 6px;
		font-size: 0.625rem;
		font-weight: 500;
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
		border-radius: 4px;
	}

	.tool-badge.more {
		background: hsl(var(--primary) / 0.1);
		color: hsl(var(--primary));
	}

	/* Installed Plugins Compact */
	.installed-plugins-compact {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 6px;
	}

	@media (max-width: 480px) {
		.installed-plugins-compact {
			grid-template-columns: 1fr;
		}
	}

	.plugin-compact-item {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 8px 10px;
		background: hsl(var(--muted) / 0.3);
		border: 1px solid hsl(var(--border));
		border-radius: 6px;
		transition: opacity 0.15s ease;
	}

	.plugin-compact-item.disabled {
		opacity: 0.5;
	}

	.plugin-compact-info {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		flex: 1;
	}

	.plugin-compact-name {
		font-size: 0.8125rem;
		color: hsl(var(--foreground));
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.plugin-compact-badges {
		display: flex;
		gap: 3px;
		flex-shrink: 0;
	}

	.feature-dot {
		width: 6px;
		height: 6px;
		border-radius: 50%;
	}

	.feature-dot.agents {
		background: hsl(var(--primary));
	}

	.feature-dot.commands {
		background: hsl(142 76% 36%);
	}

	.feature-dot.skills {
		background: hsl(280 80% 55%);
	}

	.plugin-status {
		font-size: 0.625rem;
		font-weight: 500;
		padding: 2px 6px;
		border-radius: 4px;
		background: hsl(var(--muted));
		color: hsl(var(--muted-foreground));
	}

	.plugin-status.enabled {
		background: hsl(142 76% 36% / 0.2);
		color: hsl(142 76% 36%);
	}

	.show-more-btn {
		grid-column: 1 / -1;
		padding: 8px;
		background: transparent;
		border: 1px dashed hsl(var(--border));
		border-radius: 6px;
		font-size: 0.75rem;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.show-more-btn:hover {
		color: hsl(var(--foreground));
		border-color: hsl(var(--primary) / 0.5);
	}

	.plugins-footer {
		margin-top: 12px;
		padding-top: 12px;
		border-top: 1px solid hsl(var(--border));
		display: flex;
		justify-content: center;
	}

	.btn.small {
		padding: 6px 12px;
		font-size: 0.75rem;
	}

	.empty-hint.small {
		font-size: 0.6875rem;
		opacity: 0.7;
	}

	/* Plugin Modal */
	.plugin-modal-overlay {
		position: fixed;
		inset: 0;
		background: rgba(0, 0, 0, 0.6);
		backdrop-filter: blur(4px);
		display: flex;
		align-items: center;
		justify-content: center;
		z-index: 1000;
		padding: 24px;
	}

	.plugin-modal-container {
		width: 100%;
		max-width: 900px;
		max-height: 85vh;
		background: hsl(var(--card));
		border: 1px solid hsl(var(--border));
		border-radius: 16px;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		box-shadow: 0 20px 60px rgba(0, 0, 0, 0.4);
	}

	.plugin-modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 16px 20px;
		border-bottom: 1px solid hsl(var(--border));
		flex-shrink: 0;
	}

	.plugin-modal-header h2 {
		font-size: 1.125rem;
		font-weight: 600;
		color: hsl(var(--foreground));
		margin: 0;
	}

	.close-btn {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: transparent;
		border: none;
		border-radius: 8px;
		color: hsl(var(--muted-foreground));
		cursor: pointer;
		transition: all 0.15s ease;
	}

	.close-btn:hover {
		background: hsl(var(--muted));
		color: hsl(var(--foreground));
	}

	.close-btn svg {
		width: 18px;
		height: 18px;
	}

	.plugin-modal-content {
		flex: 1;
		overflow: hidden;
		display: flex;
		flex-direction: column;
	}
</style>
