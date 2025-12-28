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
	import './card-design-system.css';

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
			// Load all data in parallel, with individual error handling
			// Note: plugins.loadFileBasedAgents() updates the store directly, so we don't need its return value
			const [subagentsResult, toolsResult] = await Promise.all([
				api.get<Subagent[]>('/subagents').catch((err) => {
					console.warn('Failed to load subagents:', err);
					return [];
				}),
				api.get<ToolsResponse>('/tools').catch((err) => {
					console.warn('Failed to load tools:', err);
					return { all_tools: [], categories: [] };
				}),
				plugins.loadFileBasedAgents().catch((err) => {
					// Silently handle - store already sets error state
					console.warn('Failed to load file-based agents:', err);
				})
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
		// 3D Model Generation Tools
		if (aiTools.text_to_3d) enabledAITools.push('text_to_3d');
		if (aiTools.image_to_3d) enabledAITools.push('image_to_3d');
		if (aiTools.retexture_3d) enabledAITools.push('retexture_3d');
		if (aiTools.rig_3d) enabledAITools.push('rig_3d');
		if (aiTools.animate_3d) enabledAITools.push('animate_3d');

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

	function getModelBadgeClass(model?: string): string {
		switch (model) {
			case 'haiku':
				return 'card-badge--haiku';
			case 'sonnet':
				return 'card-badge--sonnet';
			case 'sonnet-1m':
				return 'card-badge--sonnet-1m';
			case 'opus':
				return 'card-badge--opus';
			default:
				return 'card-badge--muted';
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
				video_understanding: effectiveAITools.includes('video_understanding'),
				// 3D Model Generation Tools
				text_to_3d: effectiveAITools.includes('text_to_3d'),
				image_to_3d: effectiveAITools.includes('image_to_3d'),
				retexture_3d: effectiveAITools.includes('retexture_3d'),
				rig_3d: effectiveAITools.includes('rig_3d'),
				animate_3d: effectiveAITools.includes('animate_3d')
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

{#snippet content()}
	<div class="card-system profile-card">
		{#if viewMode === 'form'}
			<!-- Form View with Tabs -->
			<!-- Tab Navigation -->
			<div class="card-tabs">
				{#each tabs as tab}
					<button
						class="card-tab"
						class:card-tab--active={activeTab === tab.id}
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

			<!-- Tab Content -->
			<div class="card-content">
				<div class="centered-content">
				{#if activeTab === 'general'}
					<!-- General Tab -->
					<div class="card-form-section">
						<h3 class="card-form-title">Profile Information</h3>
						<div class="form-grid">
							<div class="form-field">
								<label class="card-form-label"
									>ID <span class="required">*</span></label
								>
								<input
									bind:value={profileForm.id}
									disabled={!!editingProfile}
									class="card-form-input card-form-input--mono"
									placeholder="my-profile"
								/>
							</div>
							<div class="form-field">
								<label class="card-form-label"
									>Name <span class="required">*</span></label
								>
								<input
									bind:value={profileForm.name}
									class="card-form-input"
									placeholder="My Profile"
								/>
							</div>
						</div>
						<div class="form-field full-width">
							<label class="card-form-label">Description</label>
							<input
								bind:value={profileForm.description}
								class="card-form-input"
								placeholder="Optional description"
							/>
						</div>
					</div>

					<div class="card-form-section">
						<h3 class="card-form-title">Core Settings</h3>
						<div class="form-grid cols-3">
							<div class="form-field">
								<label class="card-form-label">Model</label>
								<select bind:value={profileForm.model} class="card-form-input">
									<option value="sonnet">Sonnet</option>
									<option value="sonnet-1m">Sonnet 1M</option>
									<option value="opus">Opus</option>
									<option value="haiku">Haiku</option>
								</select>
							</div>
							<div class="form-field">
								<label class="card-form-label">Permission Mode</label>
								<select bind:value={profileForm.permission_mode} class="card-form-input">
									<option value="default">Default</option>
									<option value="acceptEdits">Accept Edits</option>
									<option value="plan">Plan</option>
									<option value="bypassPermissions">Bypass</option>
								</select>
							</div>
							<div class="form-field">
								<label class="card-form-label">Max Turns</label>
								<input
									type="number"
									bind:value={profileForm.max_turns}
									class="card-form-input"
									placeholder="Unlimited"
								/>
							</div>
						</div>
					</div>

					<div class="card-form-section">
						<h3 class="card-form-title">Behavior</h3>
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
				{:else if activeTab === 'tools'}
					<!-- Tools Tab -->
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Claude Tools</h3>
							<span class="card-badge card-badge--primary">{availableTools.all_tools.length} available</span>
						</div>

						<!-- Tool Mode Selector -->
						<div class="card-mode-selector">
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={toolSelectionMode === 'all'}
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
								class="card-mode-btn"
								class:card-mode-btn--active={toolSelectionMode === 'allow'}
								onclick={() => {
									toolSelectionMode = 'allow';
									profileForm.disallowed_tools = [];
								}}
							>
								Allow Only
							</button>
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={toolSelectionMode === 'disallow'}
								onclick={() => {
									toolSelectionMode = 'disallow';
									profileForm.allowed_tools = [];
								}}
							>
								Disallow
							</button>
						</div>

						<p class="card-form-hint">
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
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">AI Tools</h3>
							{#if !aiToolsLoading}
								<span class="card-badge card-badge--primary">{aiToolCount} enabled</span>
							{/if}
						</div>

						<div class="card-mode-selector">
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={aiToolSelectionMode === 'all'}
								onclick={() => (aiToolSelectionMode = 'all')}
							>
								All Tools
							</button>
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={aiToolSelectionMode === 'custom'}
								onclick={() => (aiToolSelectionMode = 'custom')}
							>
								Custom
							</button>
						</div>

						{#if aiToolSelectionMode === 'custom'}
							{#if aiToolsLoading}
								<div class="loading-state">Loading AI tools...</div>
							{:else if aiToolCategories.length === 0}
								<div class="card-empty-state">
									<p class="card-empty-description">No AI tools available. Configure API keys in Settings.</p>
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
																		<span class="card-badge card-badge--warning">No API key</span>
																	{:else if tool.active_provider}
																		<span class="card-badge card-badge--success">{tool.active_provider}</span>
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
							<p class="card-form-hint">
								All configured AI tools are enabled. Configure API keys in Settings.
							</p>
						{/if}
					</div>
				{:else if activeTab === 'agents'}
					<!-- Agents Tab -->
					<!-- Database Subagents Section -->
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Database Subagents</h3>
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
							<div class="card-empty-state">
								<p class="card-empty-description">No database subagents configured</p>
							</div>
						{:else}
							<p class="card-form-hint">Select which subagents this profile can use</p>
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
												<span class="card-badge {getModelBadgeClass(agent.model)}">{getModelDisplay(agent.model)}</span>
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
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Plugin Agents</h3>
							<button
								type="button"
								class="text-btn primary"
								onclick={() => (showPluginManager = true)}
							>
								Manage Plugins
							</button>
						</div>

						{#if pluginAgents.length === 0}
							<div class="card-empty-state">
								<p class="card-empty-description">No plugin agents available</p>
								<p class="card-empty-description small">Install and enable plugins to get agents</p>
							</div>
						{:else}
							<p class="card-form-hint">Agents from enabled plugins (managed via plugin settings)</p>
							<div class="plugin-agents-list">
								{#each pluginAgents as agent (agent.id)}
									<div class="plugin-agent-item">
										<div class="plugin-agent-info">
											<div class="plugin-agent-header">
												<span class="plugin-agent-name">{agent.name}</span>
												<span class="card-badge card-badge--muted">{agent.plugin_name}</span>
												{#if agent.model}
													<span class="card-badge {getModelBadgeClass(agent.model)}">{getModelDisplay(agent.model)}</span>
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
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Installed Plugins</h3>
							<span class="card-badge card-badge--muted">{installedPluginsList.length} installed</span>
						</div>

						{#if installedPluginsList.length === 0}
							<div class="card-empty-state">
								<p class="card-empty-description">No plugins installed</p>
								<button
									type="button"
									class="card-btn-secondary small"
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
									class="card-btn-secondary small"
									onclick={() => (showPluginManager = true)}
								>
									Open Plugin Manager
								</button>
							</div>
						{/if}
					</div>
				{:else if activeTab === 'prompt'}
					<!-- System Prompt Tab -->
					<div class="card-form-section">
						<h3 class="card-form-title">System Prompt Configuration</h3>

						<div class="card-mode-selector large">
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={profileForm.system_prompt_type === 'preset'}
								onclick={() => (profileForm.system_prompt_type = 'preset')}
							>
								Claude Code + Append
							</button>
							<button
								type="button"
								class="card-mode-btn"
								class:card-mode-btn--active={profileForm.system_prompt_type === 'custom'}
								onclick={() => (profileForm.system_prompt_type = 'custom')}
							>
								Custom Prompt
							</button>
						</div>

						{#if profileForm.system_prompt_type === 'preset'}
							<div class="form-field full-width">
								<label class="card-form-label">Append Instructions</label>
								<textarea
									bind:value={profileForm.system_prompt_append}
									class="card-form-input card-form-textarea"
									rows="8"
									placeholder="Additional instructions to append to Claude Code's system prompt..."
								></textarea>
								<p class="card-form-hint">
									These instructions will be added after Claude Code's built-in system prompt.
								</p>
							</div>
						{:else}
							<div class="form-field full-width">
								<label class="card-form-label">Custom System Prompt</label>
								<textarea
									bind:value={profileForm.system_prompt_content}
									class="card-form-input card-form-textarea"
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
				{:else if activeTab === 'advanced'}
					<!-- Advanced Tab -->
					<div class="card-form-section">
						<h3 class="card-form-title">Settings Sources</h3>
						<p class="card-form-hint">Load settings from filesystem locations</p>
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

					<div class="card-form-section">
						<h3 class="card-form-title">Environment</h3>
						<div class="form-grid">
							<div class="form-field">
								<label class="card-form-label">Working Directory</label>
								<input
									bind:value={profileForm.cwd}
									class="card-form-input card-form-input--mono"
									placeholder="/workspace/my-project"
								/>
							</div>
							<div class="form-field">
								<label class="card-form-label">User Identifier</label>
								<input
									bind:value={profileForm.user}
									class="card-form-input"
									placeholder="user@example.com"
								/>
							</div>
						</div>
						<div class="form-field full-width">
							<label class="card-form-label">Additional Directories</label>
							<input
								bind:value={profileForm.add_dirs}
								class="card-form-input card-form-input--mono"
								placeholder="/extra/dir1, /extra/dir2 (comma-separated)"
							/>
						</div>
					</div>

					<div class="card-form-section">
						<h3 class="card-form-title">Performance</h3>
						<div class="form-field" style="max-width: 200px;">
							<label class="card-form-label">Max Buffer Size (bytes)</label>
							<input
								type="number"
								bind:value={profileForm.max_buffer_size}
								class="card-form-input"
								placeholder="Default"
							/>
						</div>
					</div>
				{/if}
				</div>
			</div>

			<!-- Form Footer -->
			<div class="card-footer">
				<button type="button" class="card-btn-secondary" onclick={backToList}>Cancel</button>
				<button
					type="button"
					class="card-btn-primary"
					disabled={!profileForm.id || !profileForm.name}
					onclick={saveProfile}
				>
					{editingProfile ? 'Save Changes' : 'Create Profile'}
				</button>
			</div>
		{:else}
			<!-- List View -->
			<input
				type="file"
				accept=".json"
				bind:this={profileImportInput}
				onchange={handleImport}
				class="hidden"
			/>

			<!-- Header -->
			<div class="card-header">
				<div class="card-header-info">
					{#if mobile}
						<span class="card-header-title">Profiles</span>
					{/if}
					<span class="card-header-subtitle">
						{profiles.length} profile{profiles.length !== 1 ? 's' : ''} configured
					</span>
				</div>
			</div>

			<!-- Search -->
			<div class="card-search-wrapper">
				<div class="card-search-container">
					<svg class="card-search-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
						class="card-search-input"
						placeholder="Search profiles..."
					/>
				</div>
			</div>

			<!-- Profile List -->
			<div class="card-content">
				<div class="centered-content">
				{#if filteredProfiles.length === 0}
					<div class="card-empty-state">
						{#if searchQuery}
							<p class="card-empty-title">No profiles match your search</p>
						{:else}
							<svg class="card-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="1.5"
									d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
								/>
							</svg>
							<p class="card-empty-title">No profiles yet</p>
							<p class="card-empty-description">Create your first profile to get started</p>
						{/if}
					</div>
				{:else}
					{#each filteredProfiles as profile}
						<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
						<div
							class="card-list-item"
							onclick={() => !profile.is_builtin && openEditForm(profile)}
						>
							<div class="card-item-icon">
								<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
									<path
										stroke-linecap="round"
										stroke-linejoin="round"
										stroke-width="2"
										d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"
									/>
								</svg>
							</div>
							<div class="card-item-content">
								<div class="card-item-header">
									<span class="card-item-name">{profile.name}</span>
									{#if profile.is_builtin}
										<span class="card-badge card-badge--muted">Built-in</span>
									{/if}
									{#if groups?.profiles?.assignments?.[profile.id]}
										<span class="card-badge card-badge--primary"
											>{groups.profiles.assignments[profile.id]}</span
										>
									{/if}
								</div>
								<p class="card-item-description">{profile.description || profile.id}</p>
							</div>
							<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
							<div class="card-item-actions" onclick={(e) => e.stopPropagation()}>
								<!-- Group dropdown -->
								<div class="dropdown-container">
									<button class="card-action-btn" title="Assign to group">
										<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
									class="card-action-btn"
									title="Export"
									onclick={() => handleExportProfile(profile.id)}
								>
									<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
										class="card-action-btn card-action-btn--destructive"
										title="Delete"
										onclick={() => deleteProfile(profile.id)}
									>
										<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
			</div>

			<!-- List Footer -->
			<div class="card-footer">
				<button class="card-btn-primary" onclick={openNewForm}>
					<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" />
					</svg>
					New Profile
				</button>
				<button class="card-btn-secondary" onclick={triggerImport} disabled={profileImporting}>
					{#if profileImporting}
						<span class="card-spinner card-spinner--small"></span>
					{:else}
						<svg fill="none" stroke="currentColor" viewBox="0 0 24 24">
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
		{/if}
	</div>
{/snippet}

{#if mobile}
	<!-- Mobile: No BaseCard wrapper, just the content -->
	<div class="profile-card-mobile">
		{@render content()}
	</div>
{:else}
	<!-- Desktop: Full BaseCard with all features -->
	<BaseCard
		{card}
		{onClose}
		{onMaximize}
		{onFocus}
		{onMove}
		{onResize}
		{onDragEnd}
		{onResizeEnd}
	>
		{@render content()}
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
	/* Profile Card Container */
	.profile-card {
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
	}

	.hidden {
		display: none;
	}

	/* Centered content wrapper - prevents content from stretching too wide on large screens */
	.centered-content {
		width: 100%;
		max-width: 900px;
		margin: 0 auto;
	}

	/* Search Wrapper - for padding around search */
	.card-search-wrapper {
		padding: var(--space-3, 12px) var(--space-4, 16px);
		padding-top: 0;
		flex-shrink: 0;
	}

	/* Tab icon sizing */
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

	/* Form Grid */
	.form-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-3, 12px);
		margin-top: var(--space-3, 12px);
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
		gap: var(--space-2, 8px);
	}

	.form-field.full-width {
		grid-column: 1 / -1;
		margin-top: var(--space-3, 12px);
	}

	/* Section Header */
	.section-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--space-3, 12px);
	}

	.section-header .card-form-title {
		margin: 0;
	}

	.header-actions {
		display: flex;
		gap: var(--space-3, 12px);
	}

	.text-btn {
		background: transparent;
		border: none;
		font-size: var(--text-xs, 0.75rem);
		cursor: pointer;
		transition: color var(--transition-fast, 100ms);
	}

	.text-btn.muted {
		color: var(--text-secondary);
	}

	.text-btn.muted:hover {
		color: var(--text-primary);
	}

	.text-btn.primary {
		color: var(--primary);
	}

	.text-btn.primary:hover {
		text-decoration: underline;
	}

	/* Checkboxes */
	.checkbox-group {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 12px);
	}

	.checkbox-item {
		display: flex;
		align-items: flex-start;
		gap: var(--space-3, 12px);
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all var(--transition-smooth, 200ms);
	}

	.checkbox-item:hover {
		background: var(--surface-hover);
		border-color: var(--border-emphasis);
	}

	.checkbox-item.highlighted {
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		margin-top: var(--space-3, 12px);
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
		font-size: var(--text-md, 0.875rem);
		color: var(--text-primary);
	}

	.checkbox-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: 2px;
	}

	.inline-checkboxes {
		display: flex;
		flex-wrap: wrap;
		gap: var(--space-4, 16px);
	}

	.inline-checkbox {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		cursor: pointer;
	}

	/* Mode Selector */
	.card-mode-selector.large .card-mode-btn {
		padding: var(--space-3, 12px) var(--space-4, 16px);
		font-size: var(--text-md, 0.875rem);
	}

	/* Tools List */
	.tools-list {
		max-height: 256px;
		overflow-y: auto;
		padding-right: var(--space-2, 8px);
		margin-top: var(--space-3, 12px);
	}

	.tool-category {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-bottom: var(--space-2, 8px);
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: var(--space-3, 12px);
		width: 100%;
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		transition: all var(--transition-smooth, 200ms);
	}

	.category-header:hover {
		background: var(--surface-hover);
	}

	.category-checkbox {
		width: 16px;
		height: 16px;
		border-radius: 4px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--surface-1);
		border: 1px solid var(--border-default);
		transition: all var(--transition-fast, 100ms);
	}

	.category-checkbox.checked {
		background: var(--primary);
		border-color: var(--primary);
	}

	.category-checkbox.partial {
		background: var(--primary);
		border-color: var(--primary);
	}

	.check-icon {
		width: 12px;
		height: 12px;
		color: var(--primary-foreground);
	}

	.check-icon.small {
		width: 12px;
		height: 12px;
		color: var(--primary);
	}

	.category-name {
		flex: 1;
		text-align: left;
		font-weight: 500;
	}

	.category-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.category-tools {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 4px;
		padding: var(--space-2, 8px);
		background: var(--surface-0);
	}

	@media (max-width: 480px) {
		.category-tools {
			grid-template-columns: 1fr;
		}
	}

	.tool-item {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		padding: 6px var(--space-2, 8px);
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background var(--transition-fast, 100ms);
	}

	.tool-item:hover {
		background: var(--surface-1);
	}

	.tool-checkbox {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	.tool-name {
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.selection-count {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: var(--space-3, 12px);
		padding-top: var(--space-3, 12px);
		border-top: 1px solid var(--border-subtle);
	}

	/* AI Tools */
	.ai-tools-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-3, 12px);
		margin-top: var(--space-3, 12px);
	}

	.ai-category {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		padding: var(--space-3, 12px);
		background: var(--surface-1);
	}

	.ai-category-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		margin-bottom: var(--space-3, 12px);
	}

	.ai-category-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.toggle-all-btn {
		background: transparent;
		border: none;
		font-size: var(--text-sm, 0.75rem);
		color: var(--primary);
		cursor: pointer;
	}

	.toggle-all-btn:hover {
		text-decoration: underline;
	}

	.ai-tools-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-2, 8px);
	}

	@media (max-width: 480px) {
		.ai-tools-grid {
			grid-template-columns: 1fr;
		}
	}

	.ai-tool-item {
		display: flex;
		align-items: flex-start;
		gap: var(--space-2, 8px);
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all var(--transition-smooth, 200ms);
	}

	.ai-tool-item:hover {
		background: var(--surface-hover);
		border-color: var(--border-emphasis);
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
		font-size: var(--text-md, 0.875rem);
		color: var(--text-primary);
	}

	.ai-tool-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: 2px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	/* Agents Grid */
	.agents-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: var(--space-2, 8px);
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
		gap: var(--space-3, 12px);
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all var(--transition-smooth, 200ms);
	}

	.agent-item:hover {
		border-color: var(--border-emphasis);
		transform: translateY(-1px);
	}

	.agent-item.selected {
		border-color: var(--primary);
		background: color-mix(in oklch, var(--primary) 8%, transparent);
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
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.agent-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: 4px;
		overflow: hidden;
		text-overflow: ellipsis;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		-webkit-box-orient: vertical;
	}

	/* Loading State */
	.loading-state {
		text-align: center;
		padding: var(--space-8, 32px);
		color: var(--text-secondary);
		font-size: var(--text-md, 0.875rem);
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
		background: var(--surface-0);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-md, 10px);
		box-shadow: var(--shadow-lg);
		opacity: 0;
		visibility: hidden;
		transition: all var(--transition-fast, 100ms);
		z-index: 50;
		max-height: 200px;
		overflow-y: auto;
		backdrop-filter: blur(8px);
	}

	.dropdown-container:hover .dropdown-menu {
		opacity: 1;
		visibility: visible;
	}

	.dropdown-item {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		width: 100%;
		padding: var(--space-2, 8px) var(--space-3, 12px);
		background: transparent;
		border: none;
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary);
		cursor: pointer;
		text-align: left;
		transition: background var(--transition-fast, 100ms);
	}

	.dropdown-item:hover {
		background: var(--surface-1);
	}

	.dropdown-item.muted {
		color: var(--text-secondary);
	}

	.dropdown-divider {
		height: 1px;
		background: var(--border-subtle);
		margin: 4px 0;
	}

	.spacer {
		width: 12px;
	}

	/* Plugin Agent Styles */
	.plugin-agents-list {
		display: flex;
		flex-direction: column;
		gap: var(--space-2, 8px);
		max-height: 200px;
		overflow-y: auto;
	}

	.plugin-agent-item {
		display: flex;
		flex-direction: column;
		gap: 6px;
		padding: var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		transition: all var(--transition-smooth, 200ms);
	}

	.plugin-agent-item:hover {
		border-color: var(--border-emphasis);
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
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.plugin-agent-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
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
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 500;
		background: var(--surface-1);
		color: var(--text-secondary);
		border-radius: 4px;
	}

	.tool-badge.more {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
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
		padding: var(--space-2, 8px) var(--space-3, 12px);
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-sm, 6px);
		transition: all var(--transition-smooth, 200ms);
	}

	.plugin-compact-item:hover {
		border-color: var(--border-emphasis);
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
		font-size: var(--text-base, 0.8125rem);
		color: var(--text-primary);
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
		background: var(--primary);
	}

	.feature-dot.commands {
		background: hsl(142 76% 36%);
	}

	.feature-dot.skills {
		background: hsl(280 80% 55%);
	}

	.plugin-status {
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 500;
		padding: 2px 6px;
		border-radius: 4px;
		background: var(--surface-1);
		color: var(--text-secondary);
	}

	.plugin-status.enabled {
		background: color-mix(in oklch, hsl(142 76% 36%) 20%, transparent);
		color: hsl(142 76% 36%);
	}

	.show-more-btn {
		grid-column: 1 / -1;
		padding: var(--space-2, 8px);
		background: transparent;
		border: 1px dashed var(--border-subtle);
		border-radius: var(--radius-sm, 6px);
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast, 100ms);
	}

	.show-more-btn:hover {
		color: var(--text-primary);
		border-color: var(--border-emphasis);
	}

	.plugins-footer {
		margin-top: var(--space-3, 12px);
		padding-top: var(--space-3, 12px);
		border-top: 1px solid var(--border-subtle);
		display: flex;
		justify-content: center;
	}

	/* Button modifiers */
	.card-btn-secondary.small,
	.card-btn-primary.small {
		padding: 6px var(--space-3, 12px);
		font-size: var(--text-sm, 0.75rem);
	}

	/* Required indicator */
	.required {
		color: var(--destructive);
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
		background: var(--surface-0);
		border: 1px solid var(--border-default);
		border-radius: var(--radius-xl, 18px);
		display: flex;
		flex-direction: column;
		overflow: hidden;
		box-shadow: var(--shadow-lg), 0 20px 60px rgba(0, 0, 0, 0.4);
	}

	.plugin-modal-header {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: var(--space-4, 16px) var(--space-5, 20px);
		border-bottom: 1px solid var(--border-subtle);
		flex-shrink: 0;
	}

	.plugin-modal-header h2 {
		font-size: var(--text-xl, 1.125rem);
		font-weight: 600;
		color: var(--text-primary);
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
		border-radius: var(--radius-sm, 6px);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all var(--transition-fast, 100ms);
	}

	.close-btn:hover {
		background: var(--surface-1);
		color: var(--text-primary);
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

	/* Scrollbar styling */
	.tools-list,
	.agents-grid,
	.plugin-agents-list,
	.ai-tools-list {
		scrollbar-width: thin;
		scrollbar-color: var(--border-default) transparent;
	}

	.tools-list::-webkit-scrollbar,
	.agents-grid::-webkit-scrollbar,
	.plugin-agents-list::-webkit-scrollbar,
	.ai-tools-list::-webkit-scrollbar {
		width: 6px;
	}

	.tools-list::-webkit-scrollbar-track,
	.agents-grid::-webkit-scrollbar-track,
	.plugin-agents-list::-webkit-scrollbar-track,
	.ai-tools-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.tools-list::-webkit-scrollbar-thumb,
	.agents-grid::-webkit-scrollbar-thumb,
	.plugin-agents-list::-webkit-scrollbar-thumb,
	.ai-tools-list::-webkit-scrollbar-thumb {
		background-color: var(--border-default);
		border-radius: 3px;
	}

	.tools-list::-webkit-scrollbar-thumb:hover,
	.agents-grid::-webkit-scrollbar-thumb:hover,
	.plugin-agents-list::-webkit-scrollbar-thumb:hover,
	.ai-tools-list::-webkit-scrollbar-thumb:hover {
		background-color: var(--text-tertiary);
	}
</style>
