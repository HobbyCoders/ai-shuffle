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
	import { Users, Search, Upload, Sparkles, Plus, Download, Trash2, Wrench, Bot, MessageSquare } from 'lucide-svelte';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data';
	import { groups as groupsStore } from '$lib/stores/groups';
	import { plugins, fileBasedAgents } from '$lib/stores/plugins';
	import type { PluginInfo, FileBasedAgent } from '$lib/api/plugins';
	import GroupDropdown from '$lib/components/GroupDropdown.svelte';
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
	}

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Props {
		onOpenPlugins?: () => void;
		mobile?: boolean;
	}

	let {
		onOpenPlugins,
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
				api.get<ToolsResponse>('/tools?refresh=true').catch((err) => {
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
		thinking_mode: 'default' as 'default' | 'think' | 'think_harder' | 'ultra_think',
		// Opus 4.6+ uses effort-based reasoning instead of token budgets
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
		// Plan mode exit behavior
		plan_exit_behavior: 'normal' as 'normal' | 'clear_context'
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
	let toolSelectionMode = $state<'all' | 'allow' | 'disallow'>('all');
	let aiToolSelectionMode = $state<'all' | 'custom'>('custom');

	// Collapsible tool source sections
	let expandedSections = $state<Record<string, boolean>>({
		sdk: true,
		ai: true,
		mcp: false,
		plugins: false
	});

	function toggleSection(id: string) {
		expandedSections = { ...expandedSections, [id]: !expandedSections[id] };
	}

	// SDK-only categories (exclude MCP-sourced tools)
	const sdkToolCategories = $derived(
		availableTools.categories.filter((c: ToolCategory) => c.id !== 'mcp' && c.id !== 'browser')
	);

	// MCP tools grouped by server name
	const mcpToolsByServer = $derived.by(() => {
		const groups: Record<string, { name: string; description: string }[]> = {};
		for (const tool of availableTools.all_tools) {
			if (tool.name.startsWith('mcp__')) {
				const parts = tool.name.split('__');
				const server = parts[1];
				const toolName = parts.slice(2).join('__');
				if (!groups[server]) groups[server] = [];
				groups[server].push({ name: toolName, description: tool.description });
			}
		}
		return groups;
	});

	const mcpServerNames = $derived(Object.keys(mcpToolsByServer));
	const mcpToolCount = $derived(Object.values(mcpToolsByServer).reduce((sum, tools) => sum + tools.length, 0));

	// AI Tools
	let aiToolCategories = $state<AIToolCategory[]>([]);
	let aiToolsLoading = $state(false);

	// Import handling
	let profileImportInput: HTMLInputElement | undefined = $state();
	let profileImporting = $state(false);
	let showGenerateDialog = $state(false);

	// Plugin management
	let pluginAgents = $derived($fileBasedAgents);

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
			thinking_mode: 'default' as 'default' | 'think' | 'think_harder' | 'ultra_think',
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
			plan_exit_behavior: 'normal' as 'normal' | 'clear_context'
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
		const aiTools = config.ai_tools || {};

		const allowedTools = config.allowed_tools || [];
		const disallowedTools = config.disallowed_tools || [];

		// Build enabled AI tools array dynamically from config keys
		const enabledAITools: string[] = Object.entries(aiTools)
			.filter(([_, enabled]) => enabled)
			.map(([key]) => key);

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
			plan_exit_behavior: (config as any).plan_exit_behavior || 'normal'
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

	function getAICategorySelectionState(category: AIToolCategory): 'all' | 'some' | 'none' {
		const ids = category.tools.map((t) => t.id);
		const selectedCount = ids.filter((id) => profileForm.enabled_ai_tools.includes(id)).length;
		if (selectedCount === 0) return 'none';
		if (selectedCount === ids.length) return 'all';
		return 'some';
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

	// Token budgets for thinking modes
	const THINKING_TOKEN_BUDGETS = {
		think: '8000',
		think_harder: '24000', 
		ultra_think: '64000'
	};

	// Update thinking mode and sync env var
	function updateThinkingMode(mode: 'default' | 'think' | 'think_harder' | 'ultra_think') {
		profileForm.thinking_mode = mode;
		
		// Find existing MAX_THINKING_TOKENS env var index
		const existingIndex = profileForm.env_vars.findIndex(v => v.key === 'MAX_THINKING_TOKENS');
		
		if (mode === 'default') {
			// Remove the env var if it exists
			if (existingIndex !== -1) {
				profileForm.env_vars = profileForm.env_vars.filter((_, i) => i !== existingIndex);
			}
		} else {
			// Add or update the env var
			const tokenBudget = THINKING_TOKEN_BUDGETS[mode];
			if (existingIndex !== -1) {
				// Update existing
				profileForm.env_vars[existingIndex].value = tokenBudget;
				profileForm.env_vars = [...profileForm.env_vars]; // Trigger reactivity
			} else {
				// Add new
				profileForm.env_vars = [...profileForm.env_vars, { key: 'MAX_THINKING_TOKENS', value: tokenBudget }];
			}
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
			// Plan mode exit behavior
			plan_exit_behavior: profileForm.plan_exit_behavior
		};

		// Opus uses effort-based reasoning, other models use thinking_mode with token budgets
		if (profileForm.model === 'opus') {
			if (profileForm.effort) {
				config.effort = profileForm.effort;
			}
		} else {
			config.thinking_mode = profileForm.thinking_mode;
		}

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
		if (allowedTools.length > 0) {
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
			plan_exit_behavior: (agent as any).plan_exit_behavior || 'normal'
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

	function createGroup(profileId: string) {
		const name = prompt('New group name:');
		if (name?.trim()) {
			groupsStore.createGroup('profiles', name.trim());
			groupsStore.assignToGroup('profiles', profileId, name.trim());
		}
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
		>
			{#snippet headerTabs()}
				<div class="fp-tab-bar">
					{#each tabs as tab}
						<button class="fp-tab" class:active={activeTab === tab.id} onclick={() => (activeTab = tab.id)}>
							{tab.label}
						</button>
					{/each}
				</div>
			{/snippet}

			<!-- Tab Content (reuses card-system classes) -->
			<div class="card-system">
				{#if activeTab === 'general'}
					<!-- General Tab -->
					<div class="card-form-section">
						<h3 class="card-form-title">Profile Information</h3>
						<div class="form-field">
							<label class="card-form-label">Name <span class="required">*</span></label>
							<input bind:value={profileForm.name} class="card-form-input" placeholder="My Profile" />
						</div>
						<div class="form-field full-width">
							<label class="card-form-label">Description</label>
							<input bind:value={profileForm.description} class="card-form-input" placeholder="Optional description" />
						</div>
					</div>

					<div class="card-form-section">
						<h3 class="card-form-title">Core Settings</h3>
						<div class="form-field">
							<label class="card-form-label">Model — {#if profileForm.model === 'haiku'}fastest and most compact, ideal for simple tasks{:else if profileForm.model === 'sonnet'}balanced performance, recommended for most use cases{:else if profileForm.model === 'sonnet-1m'}extended context (1M tokens) for large codebases{:else if profileForm.model === 'opus'}most capable, best for complex reasoning{/if}</label>
							<div class="card-mode-selector">
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.model === 'haiku'} onclick={() => profileForm.model = 'haiku'}>Haiku</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.model === 'sonnet'} onclick={() => profileForm.model = 'sonnet'}>Sonnet</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.model === 'sonnet-1m'} onclick={() => profileForm.model = 'sonnet-1m'}>Sonnet 1M</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.model === 'opus'} onclick={() => profileForm.model = 'opus'}>Opus</button>
							</div>
						</div>
						<div class="form-field">
							<label class="card-form-label">Permission Mode — {#if profileForm.permission_mode === 'default'}ask for confirmation before executing tools{:else if profileForm.permission_mode === 'acceptEdits'}auto-approve file edits, ask for other tools{:else if profileForm.permission_mode === 'plan'}create plans without executing, for review workflows{:else if profileForm.permission_mode === 'bypassPermissions'}execute all tools without confirmation{/if}</label>
							<div class="card-mode-selector">
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.permission_mode === 'default'} onclick={() => profileForm.permission_mode = 'default'}>Default</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.permission_mode === 'acceptEdits'} onclick={() => profileForm.permission_mode = 'acceptEdits'}>Accept Edits</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.permission_mode === 'plan'} onclick={() => profileForm.permission_mode = 'plan'}>Plan</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.permission_mode === 'bypassPermissions'} onclick={() => profileForm.permission_mode = 'bypassPermissions'}>Bypass</button>
							</div>
						</div>
						<div class="form-field">
							<label class="card-form-label">Plan Exit Behavior — {#if profileForm.plan_exit_behavior === 'normal'}keep conversation context when plan is approved{:else}clear conversation context when plan is approved for a fresh execution window{/if}</label>
							<div class="card-mode-selector">
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.plan_exit_behavior === 'normal'} onclick={() => profileForm.plan_exit_behavior = 'normal'}>Normal</button>
								<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.plan_exit_behavior === 'clear_context'} onclick={() => profileForm.plan_exit_behavior = 'clear_context'}>Clear Context</button>
							</div>
						</div>
						{#if profileForm.model === 'opus'}
							<!-- Opus 4.6+ uses effort-based reasoning -->
							<div class="form-field">
								<label class="card-form-label">Reasoning Effort — {#if !profileForm.effort}no extended reasoning, standard response mode{:else if profileForm.effort === 'low'}light reasoning, good for simple tasks{:else if profileForm.effort === 'medium'}balanced reasoning, good for most tasks{:else if profileForm.effort === 'high'}deep reasoning, good for complex problems{:else if profileForm.effort === 'max'}maximum reasoning depth, for challenging problems{/if}</label>
								<div class="card-mode-selector">
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={!profileForm.effort} onclick={() => profileForm.effort = null}>Default</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.effort === 'low'} onclick={() => profileForm.effort = 'low'}>Low</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.effort === 'medium'} onclick={() => profileForm.effort = 'medium'}>Medium</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.effort === 'high'} onclick={() => profileForm.effort = 'high'}>High</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.effort === 'max'} onclick={() => profileForm.effort = 'max'}>Max</button>
								</div>
							</div>
						{:else}
							<!-- Sonnet/Haiku use token-budget based thinking -->
							<div class="form-field">
								<label class="card-form-label">Extended Thinking — {#if profileForm.thinking_mode === 'default'}no extended thinking, standard response mode{:else if profileForm.thinking_mode === 'think'}light reasoning (8K tokens), good for simple tasks{:else if profileForm.thinking_mode === 'think_harder'}substantial reasoning (24K tokens), good for complex tasks{:else if profileForm.thinking_mode === 'ultra_think'}deep reasoning (64K tokens), for multi-step problem solving{/if}</label>
								<div class="card-mode-selector">
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.thinking_mode === 'default'} onclick={() => updateThinkingMode('default')}>Default</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.thinking_mode === 'think'} onclick={() => updateThinkingMode('think')}>Think</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.thinking_mode === 'think_harder'} onclick={() => updateThinkingMode('think_harder')}>Think Harder</button>
									<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.thinking_mode === 'ultra_think'} onclick={() => updateThinkingMode('ultra_think')}>Ultra Think</button>
								</div>
							</div>
						{/if}
						<div class="form-field">
							<label class="card-form-label">Max Turns — maximum agentic turns per query (leave empty for unlimited)</label>
							<input type="number" bind:value={profileForm.max_turns} class="card-form-input" placeholder="Unlimited" />
						</div>
					</div>

					<div class="card-form-section">
						<h3 class="card-form-title">Behavior</h3>
						<div class="checkbox-group">
							<label class="checkbox-item">
								<input type="checkbox" bind:checked={profileForm.include_partial_messages} class="checkbox-input" />
								<div class="checkbox-content">
									<span class="checkbox-label">Include Partial Messages</span>
									<p class="checkbox-desc">Stream partial text as it's being generated</p>
								</div>
							</label>
						</div>
					</div>
				{:else if activeTab === 'tools'}
					<!-- Unified Tools Tab -->

					<!-- SDK Tool Access Mode -->
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Tool Access Mode</h3>
							<span class="card-badge card-badge--primary">{availableTools.all_tools.length} total</span>
						</div>
						<div class="card-mode-selector">
							<button type="button" class="card-mode-btn" class:card-mode-btn--active={toolSelectionMode === 'all'} onclick={() => { toolSelectionMode = 'all'; profileForm.allowed_tools = []; profileForm.disallowed_tools = []; }}>All Tools</button>
							<button type="button" class="card-mode-btn" class:card-mode-btn--active={toolSelectionMode === 'allow'} onclick={() => { toolSelectionMode = 'allow'; profileForm.disallowed_tools = []; }}>Allow Only</button>
							<button type="button" class="card-mode-btn" class:card-mode-btn--active={toolSelectionMode === 'disallow'} onclick={() => { toolSelectionMode = 'disallow'; profileForm.allowed_tools = []; }}>Disallow</button>
						</div>
						<p class="card-form-hint">
							{#if toolSelectionMode === 'all'}Agent can use all available SDK and MCP tools.
							{:else if toolSelectionMode === 'allow'}Agent can ONLY use selected SDK and MCP tools.
							{:else}Agent can use all SDK and MCP tools EXCEPT selected ones.{/if}
						</p>
					</div>

					<!-- Section 1: Built-in SDK Tools -->
					<div class="tool-source-section">
						<button type="button" class="source-section-header" onclick={() => toggleSection('sdk')}>
							<span class="source-section-title">Built-in Tools</span>
							<span class="card-badge card-badge--muted">{sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0)}</span>
							<svg class="chevron" class:expanded={expandedSections.sdk} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
						</button>
						{#if expandedSections.sdk}
							<div class="source-section-body">
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
									<p class="card-form-hint" style="padding: 0 var(--space-4, 16px) var(--space-3, 12px);">All built-in tools are available.</p>
								{/if}
							</div>
						{/if}
					</div>

					<!-- Section 2: AI Tools -->
					<div class="tool-source-section">
						<button type="button" class="source-section-header" onclick={() => toggleSection('ai')}>
							<span class="source-section-title">AI Tools</span>
							{#if !aiToolsLoading}
								<span class="card-badge card-badge--primary">{aiToolCount} enabled</span>
							{/if}
							<svg class="chevron" class:expanded={expandedSections.ai} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
						</button>
						{#if expandedSections.ai}
							<div class="source-section-body">
								<div class="source-section-controls">
									<div class="card-mode-selector">
										<button type="button" class="card-mode-btn" class:card-mode-btn--active={aiToolSelectionMode === 'all'} onclick={() => (aiToolSelectionMode = 'all')}>All Tools</button>
										<button type="button" class="card-mode-btn" class:card-mode-btn--active={aiToolSelectionMode === 'custom'} onclick={() => (aiToolSelectionMode = 'custom')}>Custom</button>
									</div>
								</div>
								{#if aiToolSelectionMode === 'custom'}
									{#if aiToolsLoading}
										<div class="loading-state">Loading AI tools...</div>
									{:else if aiToolCategories.length === 0}
										<div class="card-empty-state"><p class="card-empty-description">No AI tools available. Configure API keys in Settings.</p></div>
									{:else}
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
									{/if}
								{:else}
									<p class="card-form-hint" style="padding: 0 var(--space-4, 16px) var(--space-3, 12px);">All configured AI tools are enabled.</p>
								{/if}
							</div>
						{/if}
					</div>

					<!-- Section 3: MCP Server Tools (read-only) -->
					<div class="tool-source-section">
						<button type="button" class="source-section-header" onclick={() => toggleSection('mcp')}>
							<span class="source-section-title">MCP Tools</span>
							<span class="card-badge card-badge--muted">{mcpToolCount}</span>
							<span class="card-badge card-badge--info">read-only</span>
							<svg class="chevron" class:expanded={expandedSections.mcp} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
						</button>
						{#if expandedSections.mcp}
							<div class="source-section-body">
								{#if mcpServerNames.length === 0}
									<div class="card-empty-state" style="padding: var(--space-3, 12px) var(--space-4, 16px);">
										<p class="card-empty-description">No MCP tools discovered.</p>
										<p class="card-empty-description small">Configure MCP servers in Settings.</p>
									</div>
								{:else}
									<p class="card-form-hint" style="padding: 0 var(--space-4, 16px) var(--space-2, 8px);">MCP tools are globally configured. Subject to the tool access mode above.</p>
									<div class="tools-list">
										{#each mcpServerNames as server}
											{@const tools = mcpToolsByServer[server]}
											<div class="tool-category">
												<div class="category-header readonly">
													<span class="category-name">{server}</span>
													<span class="category-count">{tools.length}</span>
												</div>
												<div class="category-tools">
													{#each tools as tool}
														<div class="tool-item readonly">
															<span class="tool-name" title={tool.description}>{tool.name}</span>
														</div>
													{/each}
												</div>
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/if}
					</div>

					<!-- Section 4: Plugin Tools (read-only) -->
					<div class="tool-source-section">
						<button type="button" class="source-section-header" onclick={() => toggleSection('plugins')}>
							<span class="source-section-title">Plugin Tools</span>
							<span class="card-badge card-badge--muted">{pluginAgents.reduce((sum, a) => sum + (a.tools?.length || 0), 0)}</span>
							<span class="card-badge card-badge--info">read-only</span>
							<svg class="chevron" class:expanded={expandedSections.plugins} width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"></polyline></svg>
						</button>
						{#if expandedSections.plugins}
							<div class="source-section-body">
								<div class="source-section-actions">
									<button type="button" class="text-btn primary" onclick={() => onOpenPlugins?.()}>Manage Plugins</button>
								</div>
								{#if pluginAgents.length === 0}
									<div class="card-empty-state" style="padding: var(--space-3, 12px) var(--space-4, 16px);">
										<p class="card-empty-description">No plugin tools available.</p>
										<p class="card-empty-description small">Install and enable plugins to get tools.</p>
									</div>
								{:else}
									<p class="card-form-hint" style="padding: 0 var(--space-4, 16px) var(--space-2, 8px);">Tools from enabled plugins (managed via plugin settings)</p>
									<div class="plugin-agents-list" style="padding: 0 var(--space-4, 16px) var(--space-3, 12px);">
										{#each pluginAgents as agent (agent.id)}
											<div class="plugin-agent-item">
												<div class="plugin-agent-info">
													<div class="plugin-agent-header">
														<span class="plugin-agent-name">{agent.name}</span>
														<span class="card-badge card-badge--muted">{agent.plugin_name}</span>
													</div>
												</div>
												{#if agent.tools && agent.tools.length > 0}
													<div class="plugin-agent-tools">
														{#each agent.tools as tool}<span class="tool-badge">{tool}</span>{/each}
													</div>
												{/if}
											</div>
										{/each}
									</div>
								{/if}
							</div>
						{/if}
					</div>

					<!-- Tools Summary -->
					<div class="tools-summary">
						{sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0)} built-in &middot;
						{aiToolCount} AI &middot;
						{mcpToolCount} MCP &middot;
						{pluginAgents.reduce((sum, a) => sum + (a.tools?.length || 0), 0)} plugin
					</div>
				{:else if activeTab === 'agents'}
					<!-- Agents Tab -->
					<div class="card-form-section">
						<div class="section-header">
							<h3 class="card-form-title">Database Subagents</h3>
							{#if allSubagents.length > 0}
								<div class="header-actions">
									<button type="button" class="text-btn muted" onclick={() => (profileForm.enabled_agents = [])}>Clear</button>
									<button type="button" class="text-btn primary" onclick={() => (profileForm.enabled_agents = allSubagents.map((a) => a.id))}>Select All</button>
								</div>
							{/if}
						</div>
						{#if allSubagents.length === 0}
							<div class="card-empty-state"><p class="card-empty-description">No database subagents configured</p></div>
						{:else}
							<p class="card-form-hint">Select which subagents this profile can use</p>
							<div class="agents-grid">
								{#each allSubagents as agent (agent.id)}
									{@const isSelected = profileForm.enabled_agents.includes(agent.id)}
									<label class="agent-item" class:selected={isSelected}>
										<input type="checkbox" checked={isSelected} onchange={() => toggleAgent(agent.id)} class="agent-checkbox" />
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
							<div class="selection-count">{profileForm.enabled_agents.length} of {allSubagents.length} enabled</div>
						{/if}
					</div>
					<!-- Plugin agents now shown in the unified Tools tab -->
				{:else if activeTab === 'prompt'}
					<!-- System Prompt Tab -->
					<div class="card-form-section">
						<h3 class="card-form-title">System Prompt Configuration</h3>
						<div class="card-mode-selector large">
							<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.system_prompt_type === 'preset'} onclick={() => (profileForm.system_prompt_type = 'preset')}>Claude Code + Append</button>
							<button type="button" class="card-mode-btn" class:card-mode-btn--active={profileForm.system_prompt_type === 'custom'} onclick={() => (profileForm.system_prompt_type = 'custom')}>Custom Prompt</button>
						</div>
						{#if profileForm.system_prompt_type === 'preset'}
							<div class="form-field full-width">
								<label class="card-form-label">Append Instructions</label>
								<textarea bind:value={profileForm.system_prompt_append} class="card-form-input card-form-textarea" rows="8" placeholder="Additional instructions to append to Claude Code's system prompt..."></textarea>
								<p class="card-form-hint">These instructions will be added after Claude Code's built-in system prompt.</p>
							</div>
						{:else}
							<div class="form-field full-width">
								<label class="card-form-label">Custom System Prompt</label>
								<textarea bind:value={profileForm.system_prompt_content} class="card-form-input card-form-textarea" rows="10" placeholder="Enter your custom system prompt (can be blank)..."></textarea>
							</div>
							<label class="checkbox-item highlighted">
								<input type="checkbox" bind:checked={profileForm.system_prompt_inject_env} class="checkbox-input" />
								<div class="checkbox-content">
									<span class="checkbox-label">Inject Environment Details</span>
									<p class="checkbox-desc">Adds working directory, platform, git status, and today's date</p>
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
							{#each [{ id: 'user', label: 'User (~/.claude)' }, { id: 'project', label: 'Project (.claude)' }, { id: 'local', label: 'Local' }] as source}
								<label class="inline-checkbox">
									<input type="checkbox" checked={profileForm.setting_sources.includes(source.id)} onchange={() => toggleSettingSource(source.id)} class="checkbox-input" />
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
								<input bind:value={profileForm.cwd} class="card-form-input card-form-input--mono" placeholder="/workspace/my-project" />
							</div>
							<div class="form-field">
								<label class="card-form-label">User Identifier</label>
								<input bind:value={profileForm.user} class="card-form-input" placeholder="user@example.com" />
							</div>
						</div>
						<div class="form-field full-width">
							<label class="card-form-label">Additional Directories</label>
							<input bind:value={profileForm.add_dirs} class="card-form-input card-form-input--mono" placeholder="/extra/dir1, /extra/dir2 (comma-separated)" />
						</div>
					</div>
					<div class="card-form-section">
						<h3 class="card-form-title">Performance</h3>
						<div class="form-field" style="max-width: 200px;">
							<label class="card-form-label">Max Buffer Size (bytes)</label>
							<input type="number" bind:value={profileForm.max_buffer_size} class="card-form-input" placeholder="Default" />
						</div>
					</div>
					<div class="card-form-section">
						<h3 class="card-form-title">Rewind & Recovery</h3>
						<div class="checkbox-group">
							<label class="checkbox-item">
								<input type="checkbox" bind:checked={profileForm.enable_file_checkpointing} class="checkbox-input" />
								<div class="checkbox-content">
									<span class="checkbox-label">Enable File Checkpointing</span>
									<p class="checkbox-desc">Create file snapshots for rewind operations. Allows restoring both conversation history and file changes. (SDK 0.1.15+)</p>
								</div>
							</label>
						</div>
					</div>
					<div class="card-form-section">
						<h3 class="card-form-title">Environment Variables</h3>
						<p class="card-form-description">Set environment variables for Claude Code sessions using this profile.</p>
						<div class="env-var-add">
							<select class="card-form-input" onchange={(e) => { const target = e.target as HTMLSelectElement; const key = target.value; if (key === '__custom__') { profileForm.env_vars = [...profileForm.env_vars, { key: '', value: '' }]; } else if (key && !profileForm.env_vars.some(v => v.key === key)) { profileForm.env_vars = [...profileForm.env_vars, { key, value: '' }]; } target.value = ''; }}>
								<option value="">+ Add variable...</option>
								{#each envVarPresets as preset}
									{#if !profileForm.env_vars.some(v => v.key === preset.key)}
										<option value={preset.key}>{preset.key}</option>
									{/if}
								{/each}
								<option value="__custom__">Custom variable...</option>
							</select>
						</div>
						{#if profileForm.env_vars.length > 0}
							<div class="env-var-list">
								{#each profileForm.env_vars as envVar, index}
									{@const preset = envVarPresets.find(p => p.key === envVar.key)}
									<div class="env-var-row">
										<input type="text" bind:value={envVar.key} class="card-form-input card-form-input--mono env-var-key" placeholder="VARIABLE_NAME" oninput={(e) => { const target = e.target as HTMLInputElement; if (target.value.includes(',')) { target.value = target.value.replace(/,/g, ''); envVar.key = target.value; } }} />
										<input type="text" bind:value={envVar.value} class="card-form-input card-form-input--mono env-var-value" placeholder={preset?.placeholder || 'value'} oninput={(e) => { const target = e.target as HTMLInputElement; if (target.value.includes(',')) { target.value = target.value.replace(/,/g, ''); envVar.value = target.value; } }} />
										<button type="button" class="env-var-remove" title="Remove variable" onclick={() => { profileForm.env_vars = profileForm.env_vars.filter((_, i) => i !== index); }}>
											<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" width="16" height="16"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" /></svg>
										</button>
									</div>
								{/each}
							</div>
						{:else}
							<p class="env-var-empty">No environment variables configured.</p>
						{/if}
					</div>
				{/if}
			</div>

			{#snippet footer()}
				<button class="fp-btn fp-btn--secondary" onclick={backToList}>Cancel</button>
				<button class="fp-btn fp-btn--primary" onclick={saveProfile} disabled={!profileForm.name}>
					{editingProfile ? 'Save Changes' : 'Create Profile'}
				</button>
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
			{#snippet headerActions()}
				<div class="fph-search-wrapper">
					<Search size={14} />
					<input type="text" bind:value={searchQuery} placeholder="Search..." class="fph-search-input" />
				</div>
				<button class="fp-btn fp-btn--ghost" onclick={triggerImport} disabled={profileImporting} title="Import">
					<Upload size={14} />
				</button>
				<button class="fp-btn fp-btn--ghost" onclick={() => showGenerateDialog = true} title="AI Generate">
					<Sparkles size={14} />
				</button>
				<button class="fp-btn fp-btn--primary" onclick={openNewForm} title="New Profile">
					<Plus size={14} /> <span class="hidden sm:inline">New Profile</span>
				</button>
			{/snippet}

			<input type="file" accept=".json" bind:this={profileImportInput} onchange={handleImport} class="hidden" />

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
					{#each filteredProfiles as profile}
						{#if deletingId === profile.id}
							<!-- Inline delete confirmation -->
							<div class="fp-list-item fp-list-item--deleting">
								<div class="fp-item-icon fp-item-icon--destructive">
									<Trash2 size={18} />
								</div>
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
								onclick={() => !profile.is_builtin && openEditForm(profile)}
								role="button"
								tabindex="0"
								onkeydown={(e) => e.key === 'Enter' && !profile.is_builtin && openEditForm(profile)}
							>
								<div class="fp-item-icon fp-item-icon--{profile.config?.model || 'sonnet'}">
									<span class="fp-model-initial">{getModelDisplay(profile.config?.model).charAt(0)}</span>
								</div>
								<div class="fp-item-content">
									<div class="fp-item-header">
										<span class="fp-item-name">{profile.name}</span>
										<span class="card-badge {getModelBadgeClass(profile.config?.model)}">{getModelDisplay(profile.config?.model)}</span>
										{#if profile.is_builtin}
											<span class="card-badge card-badge--muted">Built-in</span>
										{/if}
										{#if groups?.profiles?.assignments?.[profile.id]}
											<span class="card-badge card-badge--primary">{groups.profiles.assignments[profile.id]}</span>
										{/if}
									</div>
									{#if profile.description}
										<span class="fp-item-description">{profile.description}</span>
									{/if}
									<div class="fp-item-meta">
										<span class="fp-meta-chip"><Wrench size={12} /> {profile.config?.allowed_tools?.length || profile.config?.disallowed_tools?.length || 0} tools</span>
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><Bot size={12} /> {profile.config?.enabled_agents?.length || 0} agents</span>
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><MessageSquare size={12} /> {profile.session_count || 0}</span>
									</div>
								</div>
								<!-- svelte-ignore a11y_click_events_have_key_events a11y_no_static_element_interactions -->
								<div class="fp-item-actions" onclick={(e) => e.stopPropagation()}>
									<GroupDropdown entityType="profiles" entityId={profile.id} />
									<button class="fp-action-btn" onclick={(e) => { e.stopPropagation(); handleExportProfile(profile.id); }} title="Export">
										<Download size={14} />
									</button>
									{#if !profile.is_builtin}
										<button class="fp-action-btn fp-action-btn--destructive" onclick={(e) => { e.stopPropagation(); deletingId = profile.id; }} title="Delete">
											<Trash2 size={14} />
										</button>
									{/if}
								</div>
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

	/* Centered content wrapper */
	.centered-content {
		width: 100%;
		display: flex;
		flex-direction: column;
		gap: var(--space-4, 16px);
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

	/* Tool Source Sections (collapsible accordion) */
	.tool-source-section {
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		overflow: hidden;
		margin-top: var(--space-2, 8px);
	}

	.source-section-header {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		width: 100%;
		padding: var(--space-3, 12px) var(--space-4, 16px);
		background: var(--surface-1);
		border: none;
		color: var(--text-primary);
		font-size: var(--text-md, 0.875rem);
		cursor: pointer;
		transition: background var(--transition-smooth, 200ms);
	}

	.source-section-header:hover {
		background: var(--surface-hover);
	}

	.source-section-title {
		flex: 1;
		text-align: left;
		font-weight: 600;
	}

	.source-section-body {
		border-top: 1px solid var(--border-subtle);
	}

	.source-section-controls {
		padding: var(--space-2, 8px) var(--space-4, 16px);
	}

	.source-section-actions {
		display: flex;
		justify-content: flex-end;
		padding: var(--space-2, 8px) var(--space-4, 16px) 0;
	}

	.chevron {
		color: var(--text-secondary);
		transform: rotate(0deg);
		transition: transform var(--transition-fast, 100ms);
		flex-shrink: 0;
	}

	.chevron.expanded {
		transform: rotate(180deg);
	}

	.category-header.readonly {
		cursor: default;
	}

	.category-header.readonly:hover {
		background: var(--surface-1);
	}

	.tool-item.readonly {
		cursor: default;
		padding-left: var(--space-4, 16px);
	}

	.tool-item.readonly:hover {
		background: transparent;
	}

	.tools-summary {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		padding: var(--space-3, 12px) 0;
		text-align: center;
	}

	.card-badge--info {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
		font-size: var(--text-xs, 0.6875rem);
	}

	/* AI Tools - Category Tools with descriptions */
	.ai-category-tools {
		display: flex;
		flex-direction: column;
		padding: var(--space-2, 8px);
		background: var(--surface-0);
	}

	.ai-tool-row {
		display: flex;
		align-items: center;
		gap: var(--space-2, 8px);
		padding: 6px var(--space-2, 8px);
		border-radius: var(--radius-sm, 6px);
		cursor: pointer;
		transition: background var(--transition-fast, 100ms);
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
		color: var(--text-muted);
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
		border-color: var(--selection-ring);
		background: var(--selection-bg);
		box-shadow: var(--selection-shadow-subtle);
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

	/* ═══════════════════════════════════════════════════════════════════════════
	   FEATURE PAGE (fp-) EMBEDDED MODE STYLES — ProfileCard-specific overrides
	   ═══════════════════════════════════════════════════════════════════════════ */

	.fp-btn--sm {
		padding: 4px 10px;
		font-size: 0.75rem;
	}

	/* --- Model item icons — unified theme primary --- */
	.fp-item-icon--haiku,
	.fp-item-icon--sonnet,
	.fp-item-icon--sonnet-1m,
	.fp-item-icon--opus {
		background: color-mix(in oklch, var(--primary) 15%, transparent);
		color: var(--primary);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.fp-model-initial {
		font-size: 1rem;
		font-weight: 700;
		line-height: 1;
	}

	/* --- Responsive adjustments for ProfileCard-specific classes --- */
	@media (max-width: 639px) {
		.fp-model-initial {
			font-size: 0.875rem;
		}
	}
</style>
