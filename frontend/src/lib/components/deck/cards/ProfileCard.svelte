<script lang="ts">
	/**
	 * ProfileCard - Profile management in a card format
	 *
	 * Replaces ProfileModal with two view modes:
	 * - List view: Browse, search, and manage profiles
	 * - Form view: Edit or create profiles with tabbed interface
	 */
	import AIGenerateDialog from './AIGenerateDialog.svelte';
	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile, type AgentExport } from '$lib/api/client';
	import { Users, Upload, Sparkles, Plus, Wrench, Bot, Settings, FileText, SlidersHorizontal, Check } from 'lucide-svelte';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data';
	import { groups as groupsStore } from '$lib/stores/groups';
	import {
		DEFAULT_MODEL,
		getModelBadgeClass as getModelBadgeClassShared,
		getModelLabel,
		isPresetModel,
		normalizeModel
	} from '$lib/constants/models';
	import './card-design-system.css';

	// Sub-components
	import ProfileList from './profiles/ProfileList.svelte';
	import ProfileForm from './profiles/ProfileForm.svelte';

	// Icon map for tabs
	const tabIcons = { general: Settings, tools: Wrench, agents: Bot, prompt: FileText, advanced: SlidersHorizontal } as const;

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

	interface ToolInfo {
		name: string;
		description: string;
		mcp_server?: string;
	}

	interface ToolCategory {
		id: string;
		name: string;
		tools: ToolInfo[];
	}

	interface ToolsResponse {
		all_tools: ToolInfo[];
		categories: ToolCategory[];
	}

	interface Subagent {
		id: string;
		name: string;
		description: string;
		model?: string;
		is_builtin: boolean;
	}

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Props {
		mobile?: boolean;
	}

	let {
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
				api.get<Subagent[]>('/subagents').catch((err) => {
					console.warn('Failed to load subagents:', err);
					return [];
				}),
				api.get<ToolsResponse>('/tools?refresh=true').catch((err) => {
					console.warn('Failed to load tools:', err);
					return { all_tools: [], categories: [] };
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

	// Profile form state
	let profileForm = $state({
		id: '',
		name: '',
		description: '',
		model: DEFAULT_MODEL,
		permission_mode: 'default',
		max_turns: null as number | null,
		allowed_tools: [] as string[],
		disallowed_tools: [] as string[],
		enabled_agents: [] as string[],
		include_partial_messages: true,
		thinking_mode: 'default' as 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think',
		effort: null as 'low' | 'medium' | 'high' | 'max' | null,
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
		max_buffer_size: null as number | null,
		env_vars: [] as { key: string; value: string }[],
		enable_file_checkpointing: false,
		app_config_tools: false
	});

	// Common environment variable presets with valid ranges
	const envVarPresets = [
		{ key: 'CLAUDE_CODE_MAX_OUTPUT_TOKENS', placeholder: '1 - 64000' },
		{ key: 'MAX_THINKING_TOKENS', placeholder: '1024 - 128000' },
		{ key: 'MAX_MCP_OUTPUT_TOKENS', placeholder: '1 - 25000 (default)' },
		{ key: 'BASH_DEFAULT_TIMEOUT_MS', placeholder: 'ms (default: 120000)' },
		{ key: 'BASH_MAX_TIMEOUT_MS', placeholder: 'ms (default: 600000)' },
		{ key: 'MCP_TIMEOUT', placeholder: 'ms for server startup' },
		{ key: 'MCP_TOOL_TIMEOUT', placeholder: 'ms for tool execution' },
		{ key: 'CLAUDE_CODE_STREAM_CLOSE_TIMEOUT', placeholder: 'ms for stream close (SDK 0.1.10+)' }
	];

	// Tool selection mode
	let toolSelectionMode = $state<'all' | 'none' | 'allow' | 'disallow'>('all');
	let aiToolSelectionMode = $state<'all' | 'custom'>('custom');
	let customModelMode = $state(false);

	// Collapsible tool source sections
	let expandedSections = $state<Record<string, boolean>>({
		sdk: true,
		ai: true
	});

	function toggleSection(id: string) {
		expandedSections = { ...expandedSections, [id]: !expandedSections[id] };
	}

	// SDK-only categories (exclude MCP-sourced tools)
	const sdkToolCategories = $derived(
		availableTools.categories.filter((c: ToolCategory) => c.id !== 'mcp' && c.id !== 'browser')
	);

	// AI Tools
	let aiToolCategories = $state<AIToolCategory[]>([]);
	let aiToolsLoading = $state(false);

	// Import handling
	let profileImportInput: HTMLInputElement | undefined = $state();
	let profileImporting = $state(false);
	let showGenerateDialog = $state(false);

	// Tab definitions
	const tabs = [
		{ id: 'general', label: 'General', icon: 'settings' },
		{ id: 'tools', label: 'Tools', icon: 'wrench' },
		{ id: 'agents', label: 'Agents', icon: 'users' },
		{ id: 'prompt', label: 'Prompt', icon: 'message' },
		{ id: 'advanced', label: 'Advanced', icon: 'cog' }
	];

	// Tool counts
	const sdkToolTotal = $derived(sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0));

	const enabledSdkToolCount = $derived.by(() => {
		if (toolSelectionMode === 'none') return 0;
		if (toolSelectionMode === 'allow') {
			const sdkToolNames = new Set(sdkToolCategories.flatMap(c => c.tools.map(t => t.name)));
			return profileForm.allowed_tools.filter(t => sdkToolNames.has(t)).length;
		}
		if (toolSelectionMode === 'disallow') {
			return sdkToolTotal - profileForm.disallowed_tools.length;
		}
		return sdkToolTotal;
	});

	const aiToolCount = $derived.by(() => {
		if (toolSelectionMode === 'none') return 0;
		if (toolSelectionMode === 'allow' || toolSelectionMode === 'disallow') {
			return profileForm.enabled_ai_tools.length;
		}
		return aiToolSelectionMode === 'all' ? getAllAIToolIds().length : profileForm.enabled_ai_tools.length;
	});

	const effectiveToolCount = $derived(enabledSdkToolCount + aiToolCount);

	/**
	 * Compute the effective tool count for a profile based on its selection mode.
	 */
	function getProfileToolCount(profile: Profile): number {
		const total = availableTools.all_tools.length;
		const allowed = profile.config?.allowed_tools;
		const disallowed = profile.config?.disallowed_tools;

		if (Array.isArray(allowed) && allowed.length === 0) {
			return 0;
		}
		if (allowed && allowed.length > 0) {
			return allowed.length;
		} else if (disallowed && disallowed.length > 0) {
			return Math.max(0, total - disallowed.length);
		}
		return total;
	}

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
			model: DEFAULT_MODEL,
			permission_mode: 'default',
			max_turns: null,
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			include_partial_messages: true,
			thinking_mode: 'default' as 'default' | 'disabled' | 'adaptive' | 'think' | 'think_harder' | 'ultra_think',
			effort: null,
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
			max_buffer_size: null,
			env_vars: [],
			enable_file_checkpointing: false,
			app_config_tools: false
		};
		toolSelectionMode = 'all';
		aiToolSelectionMode = 'custom';
		customModelMode = false;
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
		const aiTools = config.ai_tools;
		const hasExplicitAIToolConfig = aiTools !== undefined && aiTools !== null && Object.keys(aiTools).length > 0;

		const allowedTools = config.allowed_tools || [];
		const disallowedTools = config.disallowed_tools || [];

		const enabledAITools: string[] = hasExplicitAIToolConfig
			? Object.entries(aiTools).filter(([_, enabled]) => enabled).map(([key]) => key)
			: [];

		if (Array.isArray(config.allowed_tools) && config.allowed_tools.length === 0) {
			toolSelectionMode = 'none';
		} else if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		customModelMode = !isPresetModel(config.model || DEFAULT_MODEL);

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: normalizeModel(config.model) || DEFAULT_MODEL,
			permission_mode: config.permission_mode || 'default',
			max_turns: config.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: ((config as any).enabled_agents || []).filter((id: string) => allSubagents.some(a => a.id === id)),
			include_partial_messages: config.include_partial_messages !== false,
			thinking_mode: (config as any).thinking_mode || 'default',
			effort: (config as any).effort || null,
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
			max_buffer_size: (config as any).max_buffer_size || null,
			env_vars: Object.entries((config as any).env || {}).map(([key, value]) => ({ key, value: value as string })),
			enable_file_checkpointing: (config as any).enable_file_checkpointing || false,
			app_config_tools: (config as any).app_config_tools || false
		};

		viewMode = 'form';
		activeTab = 'general';

		loadAvailableAITools().then(() => {
			if (!hasExplicitAIToolConfig) {
				aiToolSelectionMode = 'all';
			} else {
				const allIds = getAllAIToolIds();
				if (
					enabledAITools.length === allIds.length &&
					allIds.every((id) => enabledAITools.includes(id))
				) {
					aiToolSelectionMode = 'all';
				} else {
					aiToolSelectionMode = 'custom';
				}
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

	function getAICategorySelectionState(category: AIToolCategory): 'all' | 'some' | 'none' {
		const ids = category.tools.map((t) => t.id);
		const selectedCount = ids.filter((id) => profileForm.enabled_ai_tools.includes(id)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === ids.length) return 'all';
		return 'some';
	}

	// Agent helpers
	const builtinAgentIds = $derived(allSubagents.filter(a => a.is_builtin).map(a => a.id));
	const customAgents = $derived(allSubagents.filter(a => !a.is_builtin));

	function toggleAgent(agentId: string) {
		if (builtinAgentIds.includes(agentId)) return;
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

	// Model display delegates to the shared registry so dropdowns, badges
	// and labels stay in sync everywhere.
	const getModelDisplay = getModelLabel;
	const getModelBadgeClass = getModelBadgeClassShared;

	// Handle form field changes from sub-component
	function handleFormChange(field: string, value: any) {
		(profileForm as any)[field] = value;
	}

	// Handle tool selection mode changes from sub-component
	function handleToolSelectionModeChange(mode: 'all' | 'none' | 'allow' | 'disallow') {
		toolSelectionMode = mode;
		if (mode === 'all') {
			profileForm.allowed_tools = [];
			profileForm.disallowed_tools = [];
		} else if (mode === 'none') {
			profileForm.allowed_tools = [];
			profileForm.disallowed_tools = [];
		} else if (mode === 'allow') {
			profileForm.disallowed_tools = [];
		} else {
			profileForm.allowed_tools = [];
		}
	}

	// Handle custom model mode change
	function handleCustomModelModeChange(custom: boolean, model: string) {
		customModelMode = custom;
		profileForm.model = model;
	}

	// Save profile
	async function saveProfile() {
		if (!profileForm.name) return;

		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			include_partial_messages: profileForm.include_partial_messages,
			enable_file_checkpointing: profileForm.enable_file_checkpointing,
			app_config_tools: profileForm.app_config_tools
		};

		config.thinking_mode = profileForm.thinking_mode;
		if (profileForm.effort) {
			config.effort = profileForm.effort;
		}

		if (profileForm.max_turns) config.max_turns = profileForm.max_turns;
		if (toolSelectionMode === 'none') {
			config.allowed_tools = [];
		} else if (toolSelectionMode === 'allow' && profileForm.allowed_tools.length > 0) {
			config.allowed_tools = profileForm.allowed_tools;
		} else if (toolSelectionMode === 'disallow' && profileForm.disallowed_tools.length > 0) {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		const allEnabledAgents = [...new Set([...builtinAgentIds, ...profileForm.enabled_agents])];
		config.enabled_agents = allEnabledAgents;
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

		const validEnvVars = profileForm.env_vars.filter(v => v.key.trim());
		if (validEnvVars.length > 0) {
			config.env = validEnvVars.reduce((acc, { key, value }) => {
				acc[key.trim()] = value;
				return acc;
			}, {} as Record<string, string>);
		}

		let effectiveAITools: string[];
		if (aiToolSelectionMode === 'all') {
			const allIds = getAllAIToolIds();
			effectiveAITools = allIds.length > 0 ? allIds : [...profileForm.enabled_ai_tools];
		} else {
			effectiveAITools = [...profileForm.enabled_ai_tools];
		}
		config.ai_tools = effectiveAITools.length > 0
			? Object.fromEntries(effectiveAITools.map((id) => [id, true]))
			: {};

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

		const derivedId = profileForm.name.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
		const data = {
			id: editingProfile ? editingProfile.id : derivedId,
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

	async function handleRevertProfile(profileId: string) {
		const profile = profiles.find(p => p.id === profileId);
		if (!profile?.is_builtin) return;
		if (!confirm('Restore this profile to its original default settings? Your customizations will be lost.')) return;

		try {
			await tabsStore.restoreProfile(profileId);
		} catch (e: any) {
			alert('Failed to restore defaults: ' + (e.detail || e.message || 'Unknown error'));
		}
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

	/**
	 * Opens the form view populated with AI-generated profile data.
	 */
	function openFormFromGenerated(data: AgentExport) {
		editingProfile = null;

		const agent = data.agent;

		const allowedTools = agent.allowed_tools || [];
		const disallowedTools = agent.disallowed_tools || [];

		if (Array.isArray(agent.allowed_tools) && agent.allowed_tools.length === 0) {
			toolSelectionMode = 'none';
		} else if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		const aiTools = agent.ai_tools || {};
		const enabledAITools: string[] = Object.entries(aiTools)
			.filter(([_, enabled]) => enabled)
			.map(([key]) => key);

		if (enabledAITools.length > 0) {
			aiToolSelectionMode = 'custom';
		} else {
			aiToolSelectionMode = 'custom';
		}

		let spType = 'preset';
		let spPreset = 'claude_code';
		let spAppend = '';
		let spContent = '';
		let spInjectEnv = false;

		if (agent.system_prompt_type === 'custom') {
			spType = 'custom';
			spContent = agent.system_prompt_content || '';
			spInjectEnv = agent.system_prompt_inject_env || false;
		} else {
			spType = 'preset';
			spPreset = agent.system_prompt_preset || 'claude_code';
			spAppend = agent.system_prompt_append || '';
		}

		customModelMode = !isPresetModel(agent.model || DEFAULT_MODEL);

		profileForm = {
			id: '',
			name: agent.name,
			description: agent.description || '',
			model: normalizeModel(agent.model) || DEFAULT_MODEL,
			permission_mode: agent.permission_mode || 'default',
			max_turns: agent.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: agent.enabled_agents || [],
			include_partial_messages: agent.include_partial_messages !== false,
			thinking_mode: (agent as any).thinking_mode || 'default',
			effort: (agent as any).effort || null,
			system_prompt_type: spType,
			system_prompt_preset: spPreset,
			system_prompt_append: spAppend,
			system_prompt_content: spContent,
			system_prompt_inject_env: spInjectEnv,
			enabled_ai_tools: enabledAITools,
			setting_sources: agent.setting_sources || [],
			cwd: '',
			add_dirs: '',
			user: '',
			max_buffer_size: agent.max_buffer_size || null,
			env_vars: agent.env ? Object.entries(agent.env).map(([key, value]) => ({ key, value: value as string })) : [],
			enable_file_checkpointing: false,
			app_config_tools: false
		};

		viewMode = 'form';
		activeTab = 'general';
	}

	function handleGeneratedProfile(data: AgentExport | unknown) {
		showGenerateDialog = false;
		openFormFromGenerated(data as AgentExport);
	}

	async function handleDeleteProfile(profileId: string) {
		await tabsStore.deleteProfile(profileId);
	}

	function assignToGroup(profileId: string, groupName: string) {
		groupsStore.assignToGroup('profiles', profileId, groupName);
	}

	function removeFromGroup(profileId: string) {
		groupsStore.removeFromGroup('profiles', profileId);
	}

	function createGroup(category: string, name: string) {
		groupsStore.createGroup(category as any, name);
	}

	function deleteGroup(category: string, name: string) {
		groupsStore.deleteGroup(category as any, name);
	}
</script>


{#if viewMode === 'form'}
		<FeaturePageLayout
			icon={Users}
			title="Profiles"
			breadcrumbs={[
				{ label: 'Profiles', onclick: backToList },
				{ label: editingProfile ? editingProfile.name : 'New Profile' }
			]}
			noPadding={true}
		>
			{#snippet headerTabs()}
				<div class="fp-tab-bar">
					{#each tabs as tab}
						{@const IconComponent = tabIcons[tab.id as keyof typeof tabIcons]}
						<button class="fp-tab" class:active={activeTab === tab.id} onclick={() => (activeTab = tab.id)}>
							<IconComponent size={14} />
							<span class="tab-label">{tab.label}</span>
						</button>
					{/each}
				</div>
			{/snippet}

			<ProfileForm
				{activeTab}
				{editingProfile}
				{profileForm}
				{toolSelectionMode}
				{customModelMode}
				{groups}
				{allSubagents}
				{builtinAgentIds}
				{customAgents}
				{sdkToolCategories}
				{aiToolCategories}
				{aiToolsLoading}
				{enabledSdkToolCount}
				{aiToolCount}
				{effectiveToolCount}
				{expandedSections}
				{envVarPresets}
				onFormChange={handleFormChange}
				onToolSelectionModeChange={handleToolSelectionModeChange}
				onCustomModelModeChange={handleCustomModelModeChange}
				onToggleTool={toggleTool}
				onToggleCategory={toggleCategory}
				onToggleAITool={toggleAITool}
				onToggleAIToolCategory={toggleAIToolCategory}
				onToggleSection={toggleSection}
				onToggleAgent={toggleAgent}
				onToggleSettingSource={toggleSettingSource}
				onAssignToGroup={assignToGroup}
				onRemoveFromGroup={removeFromGroup}
				onCreateGroup={createGroup}
				onDeleteGroup={deleteGroup}
				{getCategorySelectionState}
				{getAICategorySelectionState}
				{getModelDisplay}
			/>

			{#snippet footer()}
				<div class="pf-form-footer">
					<button type="button" class="pf-btn-save" onclick={saveProfile} disabled={!profileForm.name}>
						<Check size={14} />
						<span>Save Profile</span>
					</button>
				</div>
			{/snippet}
		</FeaturePageLayout>
	{:else}
		<!-- Embedded List View -->
		<FeaturePageLayout
			icon={Users}
			title="Profiles"
			itemCount={profiles.length}
			itemLabel="profiles"
		>
			{#snippet footer()}
				<div class="pf-footer">
					<div class="pf-footer-left">
						<button class="fp-btn fp-btn--secondary" onclick={triggerImport} disabled={profileImporting} title="Import">
							<Upload size={14} /> <span class="btn-label">Import</span>
						</button>
						<button class="fp-btn fp-btn--secondary" onclick={() => showGenerateDialog = true} title="AI Generate">
							<Sparkles size={14} /> <span class="btn-label">Generate</span>
						</button>
					</div>
					<div class="pf-footer-right">
						<button class="fp-btn fp-btn--primary" onclick={openNewForm} title="New Profile">
							<Plus size={14} /> <span class="btn-label">New Profile</span>
						</button>
					</div>
				</div>
			{/snippet}

			<input type="file" accept=".json" bind:this={profileImportInput} onchange={handleImport} class="hidden" />

			<ProfileList
				{profiles}
				{groups}
				{allSubagents}
				{builtinAgentIds}
				onEditProfile={openEditForm}
				onExportProfile={handleExportProfile}
				onDeleteProfile={handleDeleteProfile}
				onRevertProfile={handleRevertProfile}
				{getModelDisplay}
				{getProfileToolCount}
			/>
		</FeaturePageLayout>
	{/if}

<!-- AI Generate Dialog -->
<AIGenerateDialog
	type="profile"
	isOpen={showGenerateDialog}
	onClose={() => showGenerateDialog = false}
	onGenerated={handleGeneratedProfile}
/>


<style>
	.hidden {
		display: none;
	}

	.btn-label {
		display: inline;
	}

	/* Tab labels — hide on small screens, icons only */
	.tab-label {
		display: none;
	}

	@media (min-width: 480px) {
		.tab-label {
			display: inline;
		}
	}

	/* Footer */
	.pf-footer {
		display: flex;
		align-items: center;
		justify-content: space-between;
		width: 100%;
	}

	.pf-footer-left {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-footer-right {
		display: flex;
		align-items: center;
		gap: 10px;
	}

	.pf-btn-save {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 22px;
		background: linear-gradient(135deg, var(--primary), color-mix(in oklch, var(--primary) 80%, #2FA88D));
		border: none;
		border-radius: var(--radius-md, 10px);
		color: var(--primary-foreground);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 600;
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-save:hover {
		filter: brightness(1.1);
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.pf-btn-save:disabled {
		opacity: 0.5;
		cursor: not-allowed;
		filter: none;
		box-shadow: none;
	}

	.pf-form-footer {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 8px;
		width: 100%;
	}

</style>
