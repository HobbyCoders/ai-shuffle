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
	import { layout } from '$lib/stores/layout';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile, type AgentExport } from '$lib/api/client';
	import { Users, Search, Upload, Sparkles, Plus, Download, Trash2, Wrench, Bot, MessageSquare, Settings, FileText, SlidersHorizontal, ChevronRight, Check, Info, Lock, Cpu, RotateCcw } from 'lucide-svelte';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data';
	import { groups as groupsStore } from '$lib/stores/groups';
	import './card-design-system.css';

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
	let searchQuery = $state('');

	// Delete confirmation state
	let deletingId = $state<string | null>(null);
	let deleteLoading = $state(false);

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
		// SDK 0.1.15+ file checkpointing for rewind
		enable_file_checkpointing: false,
		// App configuration tools — allow AI to manage hooks, automations, MCP, profiles via chat
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
	const sdkToolTotal = $derived(sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0));

	const enabledSdkToolCount = $derived.by(() => {
		if (toolSelectionMode === 'none') return 0;
		if (toolSelectionMode === 'allow') {
			// Count only allowed SDK tools (exclude AI tool IDs)
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
	 * - allowed_tools non-empty → "allow only" mode → count = allowed_tools.length
	 * - disallowed_tools non-empty → "disallow" mode → count = total - disallowed_tools.length
	 * - neither → "all" mode → count = total available tools
	 */
	function getProfileToolCount(profile: Profile): number {
		const total = availableTools.all_tools.length;
		const allowed = profile.config?.allowed_tools;
		const disallowed = profile.config?.disallowed_tools;

		// Empty allowed_tools array = "none" mode (all tools disabled)
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
			model: 'sonnet',
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

		// Build enabled AI tools array dynamically from config keys
		// If no ai_tools config exists, treat as "all enabled"
		const enabledAITools: string[] = hasExplicitAIToolConfig
			? Object.entries(aiTools).filter(([_, enabled]) => enabled).map(([key]) => key)
			: [];

		// Determine tool selection mode
		if (Array.isArray(config.allowed_tools) && config.allowed_tools.length === 0) {
			toolSelectionMode = 'none';
		} else if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		const presetModels = ['haiku', 'sonnet', 'opus', 'opus[1m]'];
		customModelMode = !presetModels.includes(config.model || 'sonnet');

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: config.model || 'sonnet',
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

		// Check if all AI tools are enabled after loading
		loadAvailableAITools().then(() => {
			if (!hasExplicitAIToolConfig) {
				// No explicit config = all AI tools enabled by default
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

	// Agent helpers — built-in agents are always enabled and cannot be toggled
	const builtinAgentIds = $derived(allSubagents.filter(a => a.is_builtin).map(a => a.id));
	const customAgents = $derived(allSubagents.filter(a => !a.is_builtin));

	function toggleAgent(agentId: string) {
		// Built-in agents cannot be toggled
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

	function getModelDisplay(model?: string): string {
		switch (model) {
			case 'haiku':
				return 'Haiku';
			case 'sonnet':
				return 'Sonnet';
			case 'opus':
				return 'Opus';
			case 'opus[1m]':
				return 'Opus 1M';
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
			case 'opus':
			case 'opus[1m]':
				return 'card-badge--opus';
			default:
				return 'card-badge--muted';
		}
	}

	// Save profile
	async function saveProfile() {
		if (!profileForm.name) return;

		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			include_partial_messages: profileForm.include_partial_messages,
			// SDK 0.1.15+ file checkpointing for rewind
			enable_file_checkpointing: profileForm.enable_file_checkpointing,
			// App configuration tools
			app_config_tools: profileForm.app_config_tools
		};

		// Thinking and effort are independent controls
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
		// Always include built-in agents + any user-selected custom agents
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

		// Environment variables - convert array to dict
		const validEnvVars = profileForm.env_vars.filter(v => v.key.trim());
		if (validEnvVars.length > 0) {
			config.env = validEnvVars.reduce((acc, { key, value }) => {
				acc[key.trim()] = value;
				return acc;
			}, {} as Record<string, string>);
		}

		// AI tools - always include ai_tools to explicitly save the selection
		let effectiveAITools: string[];
		if (aiToolSelectionMode === 'all') {
			const allIds = getAllAIToolIds();
			// Fallback: if categories haven't loaded yet, use the form's current selection
			effectiveAITools = allIds.length > 0 ? allIds : [...profileForm.enabled_ai_tools];
		} else {
			// Spread to ensure plain array (not Svelte proxy)
			effectiveAITools = [...profileForm.enabled_ai_tools];
		}
		// Always include ai_tools in config - even empty means "no AI tools enabled"
		config.ai_tools = effectiveAITools.length > 0
			? Object.fromEntries(effectiveAITools.map((id) => [id, true]))
			: {};

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

	async function restoreProfileDefaults() {
		if (!editingProfile?.is_builtin) return;
		if (!confirm('Restore this profile to its original default settings? Your customizations will be lost.')) return;

		try {
			await tabsStore.restoreProfile(editingProfile.id);
			// Re-open the edit form with restored data
			const restored = profiles.find(p => p.id === editingProfile!.id);
			if (restored) {
				openEditForm(restored);
			} else {
				viewMode = 'list';
				resetForm();
			}
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
	 * User can review/edit the values before saving.
	 */
	function openFormFromGenerated(data: AgentExport) {
		editingProfile = null; // Creating new, not editing existing

		const agent = data.agent;

		// Map AgentExportData fields to form fields
		const allowedTools = agent.allowed_tools || [];
		const disallowedTools = agent.disallowed_tools || [];

		// Determine tool selection mode
		if (Array.isArray(agent.allowed_tools) && agent.allowed_tools.length === 0) {
			toolSelectionMode = 'none';
		} else if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		// Build enabled AI tools array dynamically from config keys
		const aiTools = agent.ai_tools || {};
		const enabledAITools: string[] = Object.entries(aiTools)
			.filter(([_, enabled]) => enabled)
			.map(([key]) => key);

		// Determine AI tool selection mode
		if (enabledAITools.length > 0) {
			aiToolSelectionMode = 'custom';
		} else {
			aiToolSelectionMode = 'custom'; // Default to custom so user can add if needed
		}

		// Parse system prompt
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

		const presetModelsAgent = ['haiku', 'sonnet', 'opus', 'opus[1m]'];
		customModelMode = !presetModelsAgent.includes(agent.model || 'sonnet');

		profileForm = {
			id: '', // User must provide unique ID for new profile
			name: agent.name,
			description: agent.description || '',
			model: agent.model || 'sonnet',
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
		// Close the dialog and open the form with generated data
		showGenerateDialog = false;
		openFormFromGenerated(data as AgentExport);
	}

	async function confirmDeleteProfile() {
		if (!deletingId) return;
		deleteLoading = true;
		try {
			await tabsStore.deleteProfile(deletingId);
			deletingId = null;
		} catch (err: any) {
			console.error('Failed to delete profile:', err);
		} finally {
			deleteLoading = false;
		}
	}

	function assignToGroup(profileId: string, groupName: string) {
		groupsStore.assignToGroup('profiles', profileId, groupName);
	}

	function removeFromGroup(profileId: string) {
		groupsStore.removeFromGroup('profiles', profileId);
	}

	let creatingGroup = $state(false);
	let newGroupName = $state('');

	function startGroupCreate() {
		creatingGroup = true;
		newGroupName = '';
	}

	function confirmGroupCreate(profileId: string) {
		const name = newGroupName.trim();
		if (name) {
			groupsStore.createGroup('profiles', name);
			groupsStore.assignToGroup('profiles', profileId, name);
		}
		creatingGroup = false;
		newGroupName = '';
	}

	function cancelGroupCreate() {
		creatingGroup = false;
		newGroupName = '';
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

			<!-- Tab Content -->
			<div class="card-system pf-content">
				{#if activeTab === 'general'}
				<!-- ═══ GENERAL TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<Users size={16} />
							<h3>Profile Information</h3>
						</div>
						<div class="pf-field-grid">
							<div class="pf-field">
								<label class="pf-label" for="profile-name">Name <span class="required">*</span></label>
								<input id="profile-name" bind:value={profileForm.name} class="pf-input" placeholder="My Custom Agent" />
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-description">Description</label>
								<input id="profile-description" bind:value={profileForm.description} class="pf-input" placeholder="A coding assistant for frontend work" />
							</div>
							{#if editingProfile}
								<div class="pf-field">
									<label class="pf-label" for="profile-group">Group</label>
									<div class="pf-group-wrapper">
									<div class="pf-group-row">
										<select
											id="profile-group"
											class="pf-select"
											value={groups?.profiles?.assignments?.[editingProfile.id] ?? ''}
											onchange={(e) => {
												const v = (e.target as HTMLSelectElement).value;
												if (editingProfile) {
													if (v) assignToGroup(editingProfile.id, v);
													else removeFromGroup(editingProfile.id);
												}
											}}
										>
											<option value="">No group</option>
											{#each groups?.profiles?.groups ?? [] as group}
												<option value={group.name}>{group.name}</option>
											{/each}
										</select>
										{#if creatingGroup}
											<button type="button" class="pf-btn-new-group" onclick={() => editingProfile && confirmGroupCreate(editingProfile.id)} title="Create">
												<Check size={14} />
											</button>
										{:else}
											<button type="button" class="pf-btn-new-group" onclick={startGroupCreate} title="New group">
												<Plus size={14} />
											</button>
											{#if editingProfile && (groups?.profiles?.assignments?.[editingProfile.id] ?? "")}
												<button type="button" class="pf-btn-new-group pf-btn-new-group--delete" onclick={() => { if (!editingProfile) return; const g = groups?.profiles?.assignments?.[editingProfile.id]; if (g) { removeFromGroup(editingProfile.id); groupsStore.deleteGroup("profiles", g); } }} title="Delete group">
													<Trash2 size={14} />
												</button>
											{/if}
										{/if}
									</div>
									{#if creatingGroup}
										<!-- svelte-ignore a11y_autofocus -->
										<input
											type="text"
											class="pf-input pf-group-input"
											placeholder="Group name..."
											bind:value={newGroupName}
											onkeydown={(e) => {
												if (e.key === 'Enter') { e.preventDefault(); editingProfile && confirmGroupCreate(editingProfile.id); }
												if (e.key === 'Escape') { e.preventDefault(); cancelGroupCreate(); }
											}}
											autofocus
										/>
									{/if}
								</div>
								</div>
							{/if}
						</div>
					</div>

					<div class="pf-divider"></div>

					<div class="pf-section">
						<div class="pf-section-header">
							<Settings size={16} />
							<h3>Model & AI Settings</h3>
						</div>
						<div class="pf-field-grid">
							<div class="pf-field">
								<label class="pf-label" for="profile-model">Model</label>
								<select id="profile-model" class="pf-select" value={customModelMode ? '__custom__' : profileForm.model} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; if (v === '__custom__') { customModelMode = true; profileForm.model = ''; } else { customModelMode = false; profileForm.model = v; } }}>
									<option value="haiku">Haiku</option>
									<option value="sonnet">Sonnet</option>
									<option value="opus">Opus</option>
									<option value="opus[1m]">Opus 1M</option>
									<option value="__custom__">Custom...</option>
								</select>
								{#if customModelMode}
									<input type="text" bind:value={profileForm.model} class="pf-input pf-input--mono" placeholder="e.g. claude-opus-4-6" style="margin-top: 6px;" />
								{/if}
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-permission">Permission Mode</label>
								<select id="profile-permission" bind:value={profileForm.permission_mode} class="pf-select">
									<option value="default">Default</option>
									<option value="acceptEdits">Accept Edits</option>
									<option value="plan">Plan</option>
									<option value="bypassPermissions">Bypass Permissions</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-thinking">Thinking Mode</label>
								<select id="profile-thinking" bind:value={profileForm.thinking_mode} class="pf-select">
									<option value="default">Default</option>
									<option value="disabled">Off</option>
									<option value="adaptive">Adaptive</option>
									<option value="think">8K Budget</option>
									<option value="think_harder">24K Budget</option>
									<option value="ultra_think">64K Budget</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-effort">Effort Level</label>
								<select id="profile-effort" class="pf-select" value={profileForm.effort || ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; profileForm.effort = v ? v as 'low' | 'medium' | 'high' | 'max' : null; }}>
									<option value="">Default</option>
									<option value="low">Low</option>
									<option value="medium">Medium</option>
									<option value="high">High</option>
									<option value="max">Max</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-max-turns">Max Turns</label>
								<input id="profile-max-turns" type="number" bind:value={profileForm.max_turns} class="pf-input" placeholder="Unlimited" />
							</div>
							</div>
					</div>

					<div class="pf-divider"></div>

					<div class="pf-section">
						<div class="pf-section-header">
							<SlidersHorizontal size={16} />
							<h3>Behavior</h3>
						</div>
						<div class="pf-toggle-row">
							<div class="pf-toggle-info">
								<span class="pf-toggle-title">Include Partial Messages</span>
								<span class="pf-toggle-desc">Stream partial text during AI generation for real-time feedback</span>
							</div>
							<button
								type="button"
								class="pf-toggle"
								class:active={profileForm.include_partial_messages}
								role="switch"
								aria-checked={profileForm.include_partial_messages}
								aria-label="Toggle include partial messages"
								onclick={() => profileForm.include_partial_messages = !profileForm.include_partial_messages}
							>
								<span class="pf-toggle-knob"></span>
							</button>
						</div>
						<div class="pf-toggle-row">
							<div class="pf-toggle-info">
								<span class="pf-toggle-title">File Checkpointing</span>
								<span class="pf-toggle-desc">Create restore points before file modifications.</span>
							</div>
							<button
								type="button"
								class="pf-toggle"
								class:active={profileForm.enable_file_checkpointing}
								role="switch"
								aria-checked={profileForm.enable_file_checkpointing}
								aria-label="Toggle file checkpointing"
								onclick={() => profileForm.enable_file_checkpointing = !profileForm.enable_file_checkpointing}
							>
								<span class="pf-toggle-knob"></span>
							</button>
						</div>
						<div class="pf-toggle-row">
							<div class="pf-toggle-info">
								<span class="pf-toggle-title">App Config via Chat</span>
								<span class="pf-toggle-desc">Allow AI to manage hooks, automations, MCP servers, and profiles through conversation. Admin only.</span>
							</div>
							<button
								type="button"
								class="pf-toggle"
								class:active={profileForm.app_config_tools}
								role="switch"
								aria-checked={profileForm.app_config_tools}
								aria-label="Toggle app configuration tools"
								onclick={() => profileForm.app_config_tools = !profileForm.app_config_tools}
							>
								<span class="pf-toggle-knob"></span>
							</button>
						</div>
					</div>

				{:else if activeTab === 'tools'}
					<!-- ═══ TOOLS TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<Wrench size={16} />
							<h3>Tool Access</h3>
						</div>
						<p class="pf-desc">Control which tools this profile can access during conversations.</p>
						<select class="pf-select" bind:value={toolSelectionMode} onchange={() => { if (toolSelectionMode === 'all') { profileForm.allowed_tools = []; profileForm.disallowed_tools = []; } else if (toolSelectionMode === 'none') { profileForm.allowed_tools = []; profileForm.disallowed_tools = []; } else if (toolSelectionMode === 'allow') { profileForm.disallowed_tools = []; } else { profileForm.allowed_tools = []; } }}>
							<option value="all">All Tools Allowed</option>
							<option value="none">Disable All Tools</option>
							<option value="allow">Allow Only Selected</option>
							<option value="disallow">Disallow Selected</option>
						</select>
					</div>

					{#if toolSelectionMode !== 'none'}
					<div class="pf-section">
						<div class="pf-section-header">
							<Settings size={16} />
							<h3>Tool Categories</h3>
						</div>
						<p class="pf-desc">Browse and manage tools by category. Toggle entire categories on or off.</p>
						<div class="pf-category-list">
							<!-- Built-in SDK Tools -->
							<button type="button" class="pf-category-card" onclick={() => toggleSection('sdk')}>
								<div class="pf-category-icon">
									<Wrench size={18} />
								</div>
								<div class="pf-category-info">
									<span class="pf-category-name">Built-in Tools</span>
									<span class="pf-category-desc">{sdkToolCategories.map(c => c.name).join(', ')}</span>
								</div>
								<span class="pf-category-count">{enabledSdkToolCount}</span>
								<ChevronRight size={16} class="pf-category-chevron" />
							</button>

							{#if expandedSections.sdk}
								<div class="pf-category-expanded">
									{#if toolSelectionMode !== 'all'}
										<div class="tools-list">
											{#each sdkToolCategories as category}
												{#if category.tools.length > 0}
													{@const state = getCategorySelectionState(category)}
													<div class="tool-category">
														<button type="button" class="category-header" onclick={() => toggleCategory(category)}>
															<div class="category-checkbox" class:checked={state !== 'none'} class:partial={state === 'some'}>
																{#if state === 'all'}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
																{:else if state === 'some'}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" /></svg>
																{/if}
															</div>
															<span class="category-name">{category.name}</span>
															<span class="category-count">{category.tools.length}</span>
														</button>
														<div class="category-tools">
															{#each category.tools as tool}
																{@const isSelected = toolSelectionMode === 'allow' ? profileForm.allowed_tools.includes(tool.name) : profileForm.disallowed_tools.includes(tool.name)}
																<label class="tool-item">
																	<input type="checkbox" checked={isSelected} onchange={() => toggleTool(tool.name)} class="tool-checkbox" />
																	<span class="tool-name" title={tool.description}>{tool.name}</span>
																</label>
															{/each}
														</div>
													</div>
												{/if}
											{/each}
										</div>
										<div class="selection-count">
											{#if toolSelectionMode === 'allow'}{profileForm.allowed_tools.length} tool{profileForm.allowed_tools.length !== 1 ? 's' : ''} allowed
											{:else}{profileForm.disallowed_tools.length} tool{profileForm.disallowed_tools.length !== 1 ? 's' : ''} blocked{/if}
										</div>
									{:else}
										<p class="pf-desc" style="padding: 12px 16px;">All built-in tools are available.</p>
									{/if}
								</div>
							{/if}

							<!-- AI Tools -->
							<button type="button" class="pf-category-card" onclick={() => toggleSection('ai')}>
								<div class="pf-category-icon">
									<Sparkles size={18} />
								</div>
								<div class="pf-category-info">
									<span class="pf-category-name">AI Tools</span>
									<span class="pf-category-desc">Image generation, video, 3D models, web search</span>
								</div>
								<span class="pf-category-count">{aiToolCount}</span>
								<ChevronRight size={16} class="pf-category-chevron" />
							</button>

							{#if expandedSections.ai}
								<div class="pf-category-expanded">
									{#if aiToolsLoading}
										<div class="loading-state">Loading AI tools...</div>
									{:else if aiToolCategories.length === 0}
										<div class="card-empty-state"><p class="card-empty-description">No AI tools available. Configure API keys in Settings.</p></div>
									{:else if toolSelectionMode !== 'all'}
										<div class="tools-list">
											{#each aiToolCategories as category}
												{#if category.tools.length > 0}
													{@const state = getAICategorySelectionState(category)}
													<div class="tool-category">
														<button type="button" class="category-header" onclick={() => toggleAIToolCategory(category)}>
															<div class="category-checkbox" class:checked={state !== 'none'} class:partial={state === 'some'}>
																{#if state === 'all'}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7" /></svg>
																{:else if state === 'some'}
																	<svg class="check-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 12h14" /></svg>
																{/if}
															</div>
															<span class="category-name">{category.name}</span>
															<span class="category-count">{category.tools.length}</span>
														</button>
														<div class="ai-category-tools">
															{#each category.tools as tool}
																{@const isSelected = profileForm.enabled_ai_tools.includes(tool.id)}
																<label class="ai-tool-row" class:disabled={!tool.available}>
																	<input type="checkbox" checked={isSelected} disabled={!tool.available} onchange={() => toggleAITool(tool.id)} class="tool-checkbox" />
																	<div class="ai-tool-content">
																		<span class="tool-name">{tool.name}</span>
																		{#if !tool.available}<span class="card-badge card-badge--warning">No API key</span>{/if}
																		<p class="ai-tool-description">{tool.description}</p>
																	</div>
																</label>
															{/each}
														</div>
													</div>
												{/if}
											{/each}
										</div>
									{:else}
										<p class="pf-desc" style="padding: 12px 16px;">All AI tools are available.</p>
									{/if}
								</div>
							{/if}

						</div>
					</div>
					{/if}

					<!-- Tools Summary Bar -->
					<div class="pf-summary-bar">
						<div class="pf-summary-left">
							<Wrench size={14} />
							<span>{effectiveToolCount} tools enabled</span>
						</div>
						<span class="pf-summary-link">Manage individually →</span>
					</div>

				{:else if activeTab === 'agents'}
					<!-- ═══ AGENTS TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<Bot size={16} />
							<h3>Sub-Agents</h3>
							{#if customAgents.length > 0}
								<div class="pf-section-actions">
									<button type="button" class="text-btn primary" onclick={() => (profileForm.enabled_agents = [...builtinAgentIds, ...customAgents.map((a) => a.id)])}>Select All</button>
									<button type="button" class="text-btn muted" onclick={() => (profileForm.enabled_agents = [...builtinAgentIds])}>Clear Custom</button>
								</div>
							{/if}
						</div>
						<p class="pf-desc">Built-in agents are always available. Choose which custom agents to enable.</p>
					</div>

					{#if allSubagents.length === 0}
						<div class="card-empty-state"><p class="card-empty-description">No agents configured</p></div>
					{:else}
						<div class="fp-list" style="padding: 0 20px;">
							{#each allSubagents as agent, index (agent.id)}
								{@const isBuiltin = agent.is_builtin}
								{@const isSelected = isBuiltin || profileForm.enabled_agents.includes(agent.id)}
								{#if index > 0}<div class="fp-list-divider"></div>{/if}
								<label class="fp-list-item pf-agent-row" class:pf-agent-row--selected={isSelected} class:pf-agent-row--builtin={isBuiltin}>
									<input type="checkbox" checked={isSelected} disabled={isBuiltin} onchange={() => toggleAgent(agent.id)} class="pf-agent-checkbox" />
									<div class="fp-item-indicator" style={isSelected ? 'background: var(--primary)' : 'background: var(--text-tertiary); opacity: 0.3'}></div>
									<div class="fp-item-content">
										<div class="fp-item-header">
											<span class="fp-item-name">{agent.name}</span>
											{#if isBuiltin}
												<span class="fp-item-badge">Always on</span>
											{/if}
										</div>
										<div class="fp-item-meta">
											<span class="fp-meta-model">{getModelDisplay(agent.model)}</span>
										</div>
									</div>
								</label>
							{/each}
						</div>

						<!-- Agents Summary Bar -->
						<div class="pf-summary-bar">
							<div class="pf-summary-left">
								<Bot size={14} />
									<span>{builtinAgentIds.length} built-in{#if customAgents.length > 0} + {profileForm.enabled_agents.filter(id => !builtinAgentIds.includes(id) && customAgents.some(a => a.id === id)).length} of {customAgents.length} custom{/if}</span>
							</div>
							<button type="button" class="pf-summary-link" onclick={() => layout.openFeaturePage('subagent')}>Configure agents →</button>
						</div>
					{/if}

				{:else if activeTab === 'prompt'}
					<!-- ═══ PROMPT TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<FileText size={16} />
							<h3>System Prompt Mode</h3>
						</div>
						<p class="pf-desc">Choose how the system prompt is configured for this profile.</p>
						<select class="pf-select" bind:value={profileForm.system_prompt_type}>
							<option value="preset">Claude Code Default + Append</option>
							<option value="custom">Custom Prompt</option>
						</select>
					</div>

					<div class="pf-section">
						<div class="pf-section-header">
							<MessageSquare size={16} />
							<h3>Custom Prompt</h3>
							<span class="pf-char-count">
								{(profileForm.system_prompt_type === 'preset' ? profileForm.system_prompt_append : profileForm.system_prompt_content).length.toLocaleString()} / 10,000
							</span>
						</div>
						{#if profileForm.system_prompt_type === 'preset'}
							<textarea
								bind:value={profileForm.system_prompt_append}
								class="pf-textarea"
								rows="8"
								placeholder="Additional instructions to append to Claude Code's system prompt..."
							></textarea>
						{:else}
							<textarea
								bind:value={profileForm.system_prompt_content}
								class="pf-textarea"
								rows="10"
								placeholder="Enter your custom system prompt..."
							></textarea>
						{/if}
					</div>

					<div class="pf-toggle-row">
						<div class="pf-toggle-info">
							<span class="pf-toggle-title">Inject Environment Details</span>
							<span class="pf-toggle-desc">Automatically inject OS, platform, and working directory info into the prompt.</span>
						</div>
						<button
							type="button"
							class="pf-toggle"
							class:active={profileForm.system_prompt_inject_env}
							role="switch"
							aria-checked={profileForm.system_prompt_inject_env}
							aria-label="Toggle inject environment details"
							onclick={() => profileForm.system_prompt_inject_env = !profileForm.system_prompt_inject_env}
						>
							<span class="pf-toggle-knob"></span>
						</button>
					</div>

				{:else if activeTab === 'advanced'}
					<!-- ═══ ADVANCED TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<SlidersHorizontal size={16} />
							<h3>Environment</h3>
						</div>
						<p class="pf-desc">Configure the working environment for this profile.</p>
						<div class="pf-field-grid">
							<div class="pf-field">
								<label class="pf-label" for="profile-cwd">Working Directory</label>
								<input id="profile-cwd" bind:value={profileForm.cwd} class="pf-input pf-input--mono" placeholder="/home/user/projects/my-api" />
							</div>
							<div class="pf-field">
								<label class="pf-label" for="profile-user">Run as User</label>
								<input id="profile-user" bind:value={profileForm.user} class="pf-input" placeholder="default" />
							</div>
						</div>
					</div>

					<div class="pf-section">
						<div class="pf-section-header">
							<Settings size={16} />
							<h3>Performance & Limits</h3>
						</div>
						<div class="pf-field-grid">
							<div class="pf-field">
								<label class="pf-label" for="profile-max-buffer">Max Output Buffer</label>
								<input id="profile-max-buffer" type="number" bind:value={profileForm.max_buffer_size} class="pf-input" placeholder="200,000 chars" />
							</div>
						</div>
					</div>

					<div class="pf-section">
						<div class="pf-section-header">
							<FileText size={16} />
							<h3>Settings Sources</h3>
						</div>
						<p class="pf-desc">Load settings from filesystem locations</p>
						<div class="inline-checkboxes">
							{#each [{ id: 'user', label: 'User (~/.claude)' }, { id: 'project', label: 'Project (.claude)' }, { id: 'local', label: 'Local' }] as source}
								<label class="inline-checkbox">
									<input type="checkbox" checked={profileForm.setting_sources.includes(source.id)} onchange={() => toggleSettingSource(source.id)} class="checkbox-input" />
									<span class="checkbox-label">{source.label}</span>
								</label>
							{/each}
						</div>
					</div>

					<div class="pf-section">
						<div class="pf-section-header">
							<SlidersHorizontal size={16} />
							<h3>Environment Variables</h3>
							<button type="button" class="pf-add-var-btn" onclick={() => { profileForm.env_vars = [...profileForm.env_vars, { key: '', value: '' }]; }}>
								+ Add Variable
							</button>
						</div>
						<select class="pf-select" onchange={(e) => { const target = e.target as HTMLSelectElement; const key = target.value; if (key && !profileForm.env_vars.some(v => v.key === key)) { profileForm.env_vars = [...profileForm.env_vars, { key, value: '' }]; } target.value = ''; }}>
							<option value="">Select a known variable to add...</option>
							{#each envVarPresets as preset}
								{#if !profileForm.env_vars.some(v => v.key === preset.key)}
									<option value={preset.key}>{preset.key}</option>
								{/if}
							{/each}
						</select>
						{#if profileForm.env_vars.length > 0}
							<div class="pf-env-list">
								{#each profileForm.env_vars as envVar, index}
									{@const preset = envVarPresets.find(p => p.key === envVar.key)}
									<div class="pf-env-row">
										<input type="text" bind:value={envVar.key} class="pf-input pf-input--mono pf-env-key" placeholder="VARIABLE_NAME" oninput={(e) => { const target = e.target as HTMLInputElement; if (target.value.includes(',')) { target.value = target.value.replace(/,/g, ''); envVar.key = target.value; } }} />
										<input type="text" bind:value={envVar.value} class="pf-input pf-input--mono pf-env-value" placeholder={preset?.placeholder || 'value'} oninput={(e) => { const target = e.target as HTMLInputElement; if (target.value.includes(',')) { target.value = target.value.replace(/,/g, ''); envVar.value = target.value; } }} />
										<button type="button" class="pf-env-remove" title="Remove variable" onclick={() => { profileForm.env_vars = profileForm.env_vars.filter((_, i) => i !== index); }}>
											<Trash2 size={14} />
										</button>
									</div>
								{/each}
							</div>
						{:else}
							<p class="pf-desc">No environment variables configured.</p>
						{/if}
					</div>
				{/if}
			</div>

			{#snippet footer()}
				<div class="pf-form-footer">
					{#if editingProfile?.is_builtin}
						<button type="button" class="pf-btn-restore" onclick={restoreProfileDefaults} title="Restore this profile to its original default settings">
							<RotateCcw size={14} />
							<span>Restore Defaults</span>
						</button>
					{/if}
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
							<Sparkles size={14} /> <span class="btn-label">AI Generate</span>
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

			<div class="fp-search-bar">
				<div class="fph-search-wrapper">
					<Search size={14} />
					<input type="text" bind:value={searchQuery} placeholder="Search profiles..." class="fph-search-input" />
				</div>
			</div>

			{#if filteredProfiles.length === 0}
				<div class="card-empty-state">
					{#if searchQuery}
						<p class="card-empty-title">No profiles match your search</p>
					{:else}
						<svg class="card-empty-icon" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
						</svg>
						<p class="card-empty-title">No profiles yet</p>
						<p class="card-empty-description">Create your first profile to get started</p>
					{/if}
				</div>
			{:else}
				<div class="fp-list">
					{#each filteredProfiles as profile, index}
						{#if index > 0}<div class="fp-list-divider"></div>{/if}
						{#if deletingId === profile.id}
							<!-- Inline delete confirmation -->
							<div class="fp-list-item fp-list-item--deleting">
								<div class="fp-item-content">
									<p class="delete-confirm-text">
										Delete <strong>{profile.name}</strong>? This cannot be undone.
									</p>
									<div class="delete-confirm-actions">
										<button onclick={() => (deletingId = null)} disabled={deleteLoading} class="fp-btn fp-btn--secondary fp-btn--sm">Cancel</button>
										<button onclick={confirmDeleteProfile} disabled={deleteLoading} class="fp-btn fp-btn--destructive fp-btn--sm">
											{#if deleteLoading}<div class="card-spinner card-spinner--small"></div>{/if}
											Delete
										</button>
									</div>
								</div>
							</div>
						{:else}
							<div
								class="fp-list-item"
								onclick={() => openEditForm(profile)}
								role="button"
								tabindex="0"
								onkeydown={(e) => e.key === 'Enter' && openEditForm(profile)}
							>
								<div class="fp-item-indicator"></div>
								<div class="fp-item-content">
									<div class="fp-item-header">
										<span class="fp-item-name">{profile.name}</span>
										{#if profile.is_builtin}
											<span class="fp-item-badge">Built-in</span>
										{/if}
										{#if groups?.profiles?.assignments?.[profile.id]}
											<span class="fp-item-badge fp-item-badge--accent">{groups.profiles.assignments[profile.id]}</span>
										{/if}
									</div>
									<div class="fp-item-meta">
										<span class="fp-meta-model">{getModelDisplay(profile.config?.model)}</span>
										<span class="fp-meta-sep">·</span>
										<span class="fp-meta-chip">{getProfileToolCount(profile)} tools</span>
										<span class="fp-meta-sep">·</span>
										<span class="fp-meta-chip">{Math.max((profile.config?.enabled_agents?.filter((id: string) => allSubagents.some(a => a.id === id))?.length) || 0, builtinAgentIds.length)} agents</span>
										<span class="fp-meta-sep">·</span>
										<span class="fp-meta-chip">{profile.session_count || 0} sessions</span>
									</div>
								</div>
								<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
									<button
										class="card-action-btn"
										title="Export"
										onclick={() => handleExportProfile(profile.id)}
									>
										<Download size={16} />
									</button>
									{#if !profile.is_builtin}
										<button
											class="card-action-btn card-action-btn--destructive"
											title="Delete"
											onclick={() => { deletingId = profile.id; }}
										>
											<Trash2 size={16} />
										</button>
									{/if}
								</div>
								<div class="fp-item-chevron"><ChevronRight size={16} /></div>
							</div>
						{/if}
					{/each}
				</div>
			{/if}
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
	/* ═══════════════════════════════════════════════════════════════════════════
	   PROFILE CARD REDESIGN — Clean, card-based editing interface
	   ═══════════════════════════════════════════════════════════════════════════ */

	.hidden {
		display: none;
	}

	.btn-label {
		display: inline;
	}


	/* Search bar between header and list */
	.fp-search-bar {
		max-width: 640px;
		width: 100%;
		margin: 0 auto;
	}

	.fp-search-bar :global(.fph-search-wrapper) {
		width: 100%;
	}

	.fp-search-bar :global(.fph-search-input) {
		width: 100% !important;
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

	/* ─── Content Area ─── */
	.pf-content {
		padding: 24px 20px;
		display: flex;
		flex-direction: column;
		gap: 28px;
	}

	/* ─── Sections ─── */
	.pf-section {
		display: flex;
		flex-direction: column;
		gap: 12px;
	}

	.pf-section-header {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--text-primary);
	}

	.pf-section-header h3 {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		margin: 0;
		color: var(--text-primary);
	}

	.pf-section-actions {
		margin-left: auto;
		display: flex;
		gap: 12px;
	}

	.pf-divider {
		height: 1px;
		background: var(--border-subtle);
		width: 100%;
	}

	.pf-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin: 0;
	}

	.pf-char-count {
		margin-left: auto;
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
		font-weight: 400;
	}

	/* ─── Field Grid (two-column) ─── */
	.pf-field-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 16px;
	}

	@media (max-width: 480px) {
		.pf-field-grid {
			grid-template-columns: 1fr;
		}
	}

	.pf-field {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-label {
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		color: var(--text-secondary);
	}

	/* ─── Inputs ─── */
	.pf-input {
		width: 100%;
		padding: 10px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		transition: border-color 200ms, box-shadow 200ms;
		outline: none;
	}

	.pf-input:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-input::placeholder {
		color: var(--text-tertiary);
	}

	.pf-input--mono {
		font-family: var(--card-font-mono, 'JetBrains Mono', monospace);
		font-size: var(--text-sm, 0.75rem);
	}

	/* ─── Selects ─── */
	.pf-select {
		width: 100%;
		padding: 10px 14px;
		padding-right: 36px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		cursor: pointer;
		outline: none;
		appearance: none;
		background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='16' height='16' viewBox='0 0 24 24' fill='none' stroke='%239090A8' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E");
		background-repeat: no-repeat;
		background-position: right 12px center;
		background-size: 16px;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-select:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	/* ─── Textarea ─── */
	.pf-textarea {
		width: 100%;
		min-height: 200px;
		padding: 12px 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		font-family: inherit;
		resize: vertical;
		outline: none;
		transition: border-color 200ms, box-shadow 200ms;
	}

	.pf-textarea:focus {
		border-color: var(--primary);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent);
	}

	.pf-textarea::placeholder {
		color: var(--text-tertiary);
	}

	/* ─── Toggle Switch ─── */
	.pf-toggle-row {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 14px 16px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
	}

	.pf-toggle-info {
		display: flex;
		flex-direction: column;
		gap: 2px;
		flex: 1;
		min-width: 0;
	}

	.pf-toggle-title {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.pf-toggle-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
	}

	.pf-toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border-subtle);
		border-radius: 12px;
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
		margin-left: 16px;
	}

	.pf-toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.pf-toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
	}

	.pf-toggle.active .pf-toggle-knob {
		transform: translateX(20px);
	}

	/* ─── Category Cards (Tools Tab) ─── */
	.pf-category-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
	}

	.pf-category-card {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all 200ms;
		color: var(--text-primary);
		text-align: left;
	}

	.pf-category-card:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
		background: var(--surface-hover);
	}

	.pf-category-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 8px;
		color: var(--primary);
		flex-shrink: 0;
	}

	.pf-category-info {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.pf-category-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
	}

	.pf-category-desc {
		font-size: var(--text-xs, 0.6875rem);
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.pf-category-count {
		font-size: var(--text-md, 0.875rem);
		font-weight: 600;
		color: var(--primary);
		padding: 2px 8px;
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-radius: 6px;
	}

	.pf-category-card :global(.pf-category-chevron) {
		color: var(--text-secondary);
		flex-shrink: 0;
	}

	.pf-category-expanded {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-top: -4px;
	}

	/* ─── Summary Bar ─── */
	.pf-summary-bar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		padding: 12px 16px;
		background: color-mix(in oklch, var(--primary) 3%, transparent);
		border: 1px solid color-mix(in oklch, var(--primary) 20%, transparent);
		border-radius: var(--radius-md, 10px);
	}

	.pf-summary-left {
		display: flex;
		align-items: center;
		gap: 6px;
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.pf-summary-left :global(svg) {
		color: var(--primary);
	}

	.pf-summary-link {
		font-size: var(--text-xs, 0.6875rem);
		font-weight: 500;
		color: var(--text-secondary);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		transition: color 120ms;
	}

	.pf-summary-link:hover {
		color: var(--primary);
	}

	/* ─── Agent Cards ─── */
	.pf-agent-row {
		cursor: pointer;
	}

	.pf-agent-row--builtin {
		cursor: default;
		opacity: 0.85;
	}

	.pf-agent-checkbox {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		accent-color: var(--primary);
	}

	.pf-agent-checkbox:disabled {
		opacity: 0.5;
		cursor: default;
	}

	/* ─── Environment Variables ─── */
	.pf-env-list {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-env-row {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-env-key {
		width: 200px;
		flex-shrink: 0;
	}

	.pf-env-value {
		flex: 1;
	}

	.pf-env-remove {
		width: 32px;
		height: 32px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: none;
		border-radius: 6px;
		color: var(--destructive);
		cursor: pointer;
		transition: all 150ms;
		flex-shrink: 0;
	}

	.pf-env-remove:hover {
		background: color-mix(in oklch, var(--destructive) 20%, transparent);
	}

	.pf-add-var-btn {
		margin-left: auto;
		background: none;
		border: none;
		color: var(--primary);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
	}

	.pf-add-var-btn:hover {
		text-decoration: underline;
	}

	@media (max-width: 480px) {
		.pf-env-row {
			display: grid;
			grid-template-columns: 1fr auto;
			grid-template-rows: auto auto;
		}

		.pf-env-key {
			width: 100%;
			grid-column: 1;
			grid-row: 1;
		}

		.pf-env-value {
			grid-column: 1;
			grid-row: 2;
		}

		.pf-env-remove {
			grid-column: 2;
			grid-row: 1 / 3;
			align-self: center;
		}
	}

	/* ─── Footer ─── */
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

	.pf-group-wrapper {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.pf-group-row {
		display: flex;
		gap: 6px;
		align-items: center;
	}

	.pf-group-row .pf-select {
		flex: 1;
	}

	.pf-btn-new-group {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 36px;
		height: 36px;
		flex-shrink: 0;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary);
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-new-group:hover {
		background: var(--surface-hover);
		color: var(--text-primary);
		border-color: var(--border-default);
	}

	.pf-btn-new-group--delete {
		color: var(--destructive);
	}

	.pf-btn-new-group--delete:hover {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.pf-group-input {
		width: 100%;
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

	.pf-btn-restore {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 16px;
		background: transparent;
		border: 1px solid var(--border-default);
		border-radius: var(--radius-md, 10px);
		color: var(--text-secondary);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-restore:hover {
		background: color-mix(in oklch, var(--warning, #f59e0b) 10%, transparent);
		border-color: color-mix(in oklch, var(--warning, #f59e0b) 40%, transparent);
		color: var(--warning, #f59e0b);
	}

	/* ─── Existing styles retained ─── */

	.text-btn {
		background: transparent;
		border: none;
		font-size: var(--text-xs, 0.75rem);
		cursor: pointer;
		transition: color 100ms;
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

	.required {
		color: var(--destructive);
	}

	/* Inline checkboxes (Advanced tab: Settings Sources) */
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

	.checkbox-input {
		width: 16px;
		height: 16px;
		accent-color: var(--primary);
		color-scheme: dark;
	}

	.checkbox-label {
		font-size: var(--text-md, 0.875rem);
		color: var(--text-primary);
	}

	/* Tool details (reused from existing) */
	.tools-list {
		max-height: 256px;
		overflow-y: auto;
		padding-right: 8px;
		margin-top: 12px;
	}

	.tool-category {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-bottom: 8px;
	}

	.category-header {
		display: flex;
		align-items: center;
		gap: 12px;
		width: 100%;
		padding: 12px;
		background: var(--surface-1);
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		transition: background 200ms;
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
		transition: all 100ms;
	}

	.category-checkbox.checked,
	.category-checkbox.partial {
		background: var(--primary);
		border-color: var(--primary);
	}

	.check-icon {
		width: 12px;
		height: 12px;
		color: var(--primary-foreground);
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
		padding: 8px;
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
		gap: 8px;
		padding: 6px 8px;
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background 100ms;
	}

	.tool-item:hover {
		background: var(--surface-1);
	}

	.tool-checkbox {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
		accent-color: var(--primary);
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
		margin-top: 12px;
		padding: 12px 16px 0;
		border-top: 1px solid var(--border-subtle);
	}

	/* AI tool rows */
	.ai-category-tools {
		display: flex;
		flex-direction: column;
		padding: 8px;
		background: var(--surface-0);
	}

	.ai-tool-row {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 6px 8px;
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background 100ms;
	}

	.ai-tool-row:hover {
		background: var(--surface-1);
	}

	.ai-tool-row.disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.ai-tool-content {
		flex: 1;
		min-width: 0;
		display: flex;
		align-items: center;
		gap: 6px;
		overflow: hidden;
	}

	.ai-tool-description {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		flex: 1;
		min-width: 0;
	}

	.ai-tool-description::before {
		content: "—";
		margin-right: 6px;
	}

	/* Loading state */
	.loading-state {
		text-align: center;
		padding: 32px;
		color: var(--text-secondary);
		font-size: var(--text-md, 0.875rem);
	}

	/* Scrollbar styling */
	.tools-list {
		scrollbar-width: thin;
		scrollbar-color: var(--border-default) transparent;
	}

	.tools-list::-webkit-scrollbar {
		width: 6px;
	}

	.tools-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.tools-list::-webkit-scrollbar-thumb {
		background-color: var(--border-default);
		border-radius: 3px;
	}

	/* ═══════════════════════════════════════════════════════════════════════════
	   LIST VIEW STYLES (unchanged)
	   ═══════════════════════════════════════════════════════════════════════════ */

	.fp-btn--sm {
		padding: 4px 10px;
		font-size: 0.75rem;
	}

</style>
