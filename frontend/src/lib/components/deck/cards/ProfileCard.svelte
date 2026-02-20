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
	import { Users, Search, Upload, Sparkles, Plus, Download, Trash2, Wrench, Bot, MessageSquare, Settings, FileText, SlidersHorizontal, ChevronRight, Check, Info } from 'lucide-svelte';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data';
	import { groups as groupsStore } from '$lib/stores/groups';
	import { plugins, fileBasedAgents } from '$lib/stores/plugins';
	import type { PluginInfo, FileBasedAgent } from '$lib/api/plugins';
	import GroupDropdown from '$lib/components/GroupDropdown.svelte';
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
			case 'opus':
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
			// Plan mode exit behavior
			plan_exit_behavior: profileForm.plan_exit_behavior
		};

		// Thinking and effort are independent controls
		config.thinking_mode = profileForm.thinking_mode;
		if (profileForm.effort) {
			config.effort = profileForm.effort;
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
								<label class="pf-label">Name <span class="required">*</span></label>
								<input bind:value={profileForm.name} class="pf-input" placeholder="My Custom Agent" />
							</div>
							<div class="pf-field">
								<label class="pf-label">Description</label>
								<input bind:value={profileForm.description} class="pf-input" placeholder="A coding assistant for frontend work" />
							</div>
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
								<label class="pf-label">Model</label>
								<select class="pf-select" value={['haiku','sonnet','opus'].includes(profileForm.model) ? profileForm.model : '__custom__'} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; profileForm.model = v === '__custom__' ? '' : v; }}>
									<option value="haiku">Haiku</option>
									<option value="sonnet">Sonnet</option>
									<option value="opus">Opus</option>
									<option value="__custom__">Custom...</option>
								</select>
								{#if !['haiku','sonnet','opus'].includes(profileForm.model)}
									<input type="text" bind:value={profileForm.model} class="pf-input pf-input--mono" placeholder="e.g. claude-opus-4-6" style="margin-top: 6px;" />
								{/if}
							</div>
							<div class="pf-field">
								<label class="pf-label">Permission Mode</label>
								<select bind:value={profileForm.permission_mode} class="pf-select">
									<option value="default">Default</option>
									<option value="acceptEdits">Accept Edits</option>
									<option value="plan">Plan</option>
									<option value="bypassPermissions">Bypass Permissions</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label">Thinking Mode</label>
								<select bind:value={profileForm.thinking_mode} class="pf-select">
									<option value="default">Default</option>
									<option value="disabled">Off</option>
									<option value="adaptive">Adaptive</option>
									<option value="think">8K Budget</option>
									<option value="think_harder">24K Budget</option>
									<option value="ultra_think">64K Budget</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label">Effort Level</label>
								<select class="pf-select" value={profileForm.effort || ''} onchange={(e) => { const v = (e.target as HTMLSelectElement).value; profileForm.effort = v ? v as 'low' | 'medium' | 'high' | 'max' : null; }}>
									<option value="">Default</option>
									<option value="low">Low</option>
									<option value="medium">Medium</option>
									<option value="high">High</option>
									<option value="max">Max</option>
								</select>
							</div>
							<div class="pf-field">
								<label class="pf-label">Max Turns</label>
								<input type="number" bind:value={profileForm.max_turns} class="pf-input" placeholder="Unlimited" />
							</div>
							<div class="pf-field">
								<label class="pf-label">Plan Exit Behavior</label>
								<select bind:value={profileForm.plan_exit_behavior} class="pf-select">
									<option value="normal">Normal</option>
									<option value="clear_context">Clear Context</option>
								</select>
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
								onclick={() => profileForm.include_partial_messages = !profileForm.include_partial_messages}
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
						<select class="pf-select" bind:value={toolSelectionMode} onchange={() => { if (toolSelectionMode === 'all') { profileForm.allowed_tools = []; profileForm.disallowed_tools = []; } else if (toolSelectionMode === 'allow') { profileForm.disallowed_tools = []; } else { profileForm.allowed_tools = []; } }}>
							<option value="all">All Tools Allowed</option>
							<option value="allow">Allow Only Selected</option>
							<option value="disallow">Disallow Selected</option>
						</select>
					</div>

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
								<span class="pf-category-count">{sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0)}</span>
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

							<!-- MCP Server Tools -->
							<button type="button" class="pf-category-card" onclick={() => toggleSection('mcp')}>
								<div class="pf-category-icon">
									<Bot size={18} />
								</div>
								<div class="pf-category-info">
									<span class="pf-category-name">MCP Server Tools</span>
									<span class="pf-category-desc">Browser, Pencil, external integrations</span>
								</div>
								<span class="pf-category-count">{mcpToolCount}</span>
								<ChevronRight size={16} class="pf-category-chevron" />
							</button>

							{#if expandedSections.mcp}
								<div class="pf-category-expanded">
									{#if mcpServerNames.length === 0}
										<p class="pf-desc" style="padding: 12px 16px;">No MCP tools discovered. Configure MCP servers in Settings.</p>
									{:else}
										<p class="pf-desc" style="padding: 12px 16px;">MCP tools are globally configured. Subject to the tool access mode above.</p>
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

							<!-- Plugin Tools -->
							<button type="button" class="pf-category-card" onclick={() => toggleSection('plugins')}>
								<div class="pf-category-icon">
									<Sparkles size={18} />
								</div>
								<div class="pf-category-info">
									<span class="pf-category-name">Plugin Tools</span>
									<span class="pf-category-desc">Community extensions and custom plugins</span>
								</div>
								<span class="pf-category-count">{pluginAgents.reduce((sum, a) => sum + (a.tools?.length || 0), 0)}</span>
								<ChevronRight size={16} class="pf-category-chevron" />
							</button>

							{#if expandedSections.plugins}
								<div class="pf-category-expanded">
									<div class="source-section-actions">
										<button type="button" class="text-btn primary" onclick={() => onOpenPlugins?.()}>Manage Plugins</button>
									</div>
									{#if pluginAgents.length === 0}
										<p class="pf-desc" style="padding: 12px 16px;">No plugin tools available. Install plugins to get tools.</p>
									{:else}
										<div class="plugin-agents-list" style="padding: 0 16px 12px;">
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
					</div>

					<!-- Tools Summary Bar -->
					<div class="pf-summary-bar">
						<div class="pf-summary-left">
							<Wrench size={14} />
							<span>{sdkToolCategories.reduce((sum, c) => sum + c.tools.length, 0) + aiToolCount + mcpToolCount + pluginAgents.reduce((sum, a) => sum + (a.tools?.length || 0), 0)} tools enabled across {4} categories</span>
						</div>
						<span class="pf-summary-link">Manage individually →</span>
					</div>

				{:else if activeTab === 'agents'}
					<!-- ═══ AGENTS TAB ═══ -->
					<div class="pf-section">
						<div class="pf-section-header">
							<Bot size={16} />
							<h3>Sub-Agents</h3>
							{#if allSubagents.length > 0}
								<div class="pf-section-actions">
									<button type="button" class="text-btn primary" onclick={() => (profileForm.enabled_agents = allSubagents.map((a) => a.id))}>Select All</button>
									<button type="button" class="text-btn muted" onclick={() => (profileForm.enabled_agents = [])}>Clear All</button>
								</div>
							{/if}
						</div>
						<p class="pf-desc">Choose which sub-agents this profile can delegate tasks to.</p>
					</div>

					{#if allSubagents.length === 0}
						<div class="card-empty-state"><p class="card-empty-description">No database subagents configured</p></div>
					{:else}
						<div class="pf-agent-list">
							{#each allSubagents as agent (agent.id)}
								{@const isSelected = profileForm.enabled_agents.includes(agent.id)}
								<label class="pf-agent-card" class:selected={isSelected}>
									<input type="checkbox" checked={isSelected} onchange={() => toggleAgent(agent.id)} class="pf-agent-checkbox" />
									<div class="pf-agent-icon" class:active={isSelected}>
										{#if agent.model === 'opus'}
											<Bot size={18} />
										{:else if agent.model === 'haiku'}
											<Sparkles size={18} />
										{:else}
											<MessageSquare size={18} />
										{/if}
									</div>
									<div class="pf-agent-info">
										<div class="pf-agent-header">
											<span class="pf-agent-name">{agent.name}</span>
											<span class="card-badge {getModelBadgeClass(agent.model)}">{getModelDisplay(agent.model)}</span>
										</div>
										<p class="pf-agent-desc">{agent.description}</p>
									</div>
								</label>
							{/each}
						</div>

						<!-- Agents Summary Bar -->
						<div class="pf-summary-bar">
							<div class="pf-summary-left">
								<Bot size={14} />
								<span>{profileForm.enabled_agents.length} of {allSubagents.length} agents enabled</span>
							</div>
							<span class="pf-summary-link">Configure agents →</span>
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
								<label class="pf-label">Working Directory</label>
								<input bind:value={profileForm.cwd} class="pf-input pf-input--mono" placeholder="/home/user/projects/my-api" />
							</div>
							<div class="pf-field">
								<label class="pf-label">Run as User</label>
								<input bind:value={profileForm.user} class="pf-input" placeholder="default" />
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
								<label class="pf-label">Max Output Buffer</label>
								<input type="number" bind:value={profileForm.max_buffer_size} class="pf-input" placeholder="200,000 chars" />
							</div>
						</div>
						<div class="pf-toggle-row" style="margin-top: 12px;">
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
								onclick={() => profileForm.enable_file_checkpointing = !profileForm.enable_file_checkpointing}
							>
								<span class="pf-toggle-knob"></span>
							</button>
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
				<div class="pf-footer">
					<div class="pf-footer-left">
						{#if editingProfile && !editingProfile.is_builtin}
							<button type="button" class="pf-btn-delete" onclick={() => { deletingId = editingProfile?.id ?? null; }}>
								<Trash2 size={14} />
								<span>Delete</span>
							</button>
						{/if}
					</div>
					<div class="pf-footer-right">
						<button type="button" class="pf-btn-cancel" onclick={backToList}>Cancel</button>
						<button type="button" class="pf-btn-save" onclick={saveProfile} disabled={!profileForm.name}>
							<Check size={14} />
							<span>Save Profile</span>
						</button>
					</div>
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
										<span class="fp-meta-chip"><Wrench size={12} /> {getProfileToolCount(profile)} tools</span>
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><Bot size={12} /> {profile.config?.enabled_agents?.length || 0} agents</span>
										<span class="fp-meta-dot"></span>
										<span class="fp-meta-chip"><MessageSquare size={12} /> {profile.session_count || 0}</span>
									</div>
								</div>
								<div class="fp-item-actions" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()} role="toolbar" tabindex="-1">
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
	/* ═══════════════════════════════════════════════════════════════════════════
	   PROFILE CARD REDESIGN — Clean, card-based editing interface
	   ═══════════════════════════════════════════════════════════════════════════ */

	.hidden {
		display: none;
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

	/* ─── Info Banner ─── */
	.pf-info-banner {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 10px 14px;
		background: color-mix(in oklch, var(--info, #4488DD) 5%, transparent);
		border: 1px solid color-mix(in oklch, var(--info, #4488DD) 20%, transparent);
		border-radius: 8px;
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
	}

	.pf-info-banner :global(svg) {
		flex-shrink: 0;
		color: var(--info, #4488DD);
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
	}

	/* ─── Agent Cards ─── */
	.pf-agent-list {
		display: flex;
		flex-direction: column;
		gap: 8px;
		padding: 0 20px;
	}

	.pf-agent-card {
		display: flex;
		align-items: center;
		gap: 12px;
		padding: 14px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-agent-card:hover {
		border-color: var(--border-emphasis);
	}

	.pf-agent-card.selected {
		border-color: color-mix(in oklch, var(--primary) 40%, transparent);
		background: color-mix(in oklch, var(--primary) 5%, transparent);
	}

	.pf-agent-checkbox {
		width: 18px;
		height: 18px;
		flex-shrink: 0;
		accent-color: var(--primary);
	}

	.pf-agent-icon {
		width: 36px;
		height: 36px;
		display: flex;
		align-items: center;
		justify-content: center;
		background: var(--surface-0);
		border-radius: 8px;
		color: var(--text-secondary);
		flex-shrink: 0;
		border: 1px solid var(--border-subtle);
	}

	.pf-agent-icon.active {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 30%, transparent);
	}

	.pf-agent-info {
		flex: 1;
		min-width: 0;
	}

	.pf-agent-header {
		display: flex;
		align-items: center;
		gap: 8px;
	}

	.pf-agent-name {
		font-size: var(--text-md, 0.875rem);
		font-weight: 500;
		color: var(--text-primary);
	}

	.pf-agent-desc {
		font-size: var(--text-sm, 0.75rem);
		color: var(--text-secondary);
		margin-top: 2px;
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
			flex-direction: column;
		}

		.pf-env-key {
			width: 100%;
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

	.pf-btn-delete {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 14px;
		background: transparent;
		border: 1px solid color-mix(in oklch, var(--destructive) 25%, transparent);
		border-radius: var(--radius-md, 10px);
		color: var(--destructive);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-delete:hover {
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 40%, transparent);
	}

	.pf-btn-cancel {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 8px 18px;
		background: var(--secondary);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		color: var(--text-primary);
		font-size: var(--text-sm, 0.75rem);
		font-weight: 500;
		cursor: pointer;
		transition: all 200ms;
	}

	.pf-btn-cancel:hover {
		background: var(--surface-hover);
		border-color: var(--border-emphasis);
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

	.source-section-controls {
		padding: 8px 16px;
	}

	.source-section-actions {
		display: flex;
		justify-content: flex-end;
		padding: 8px 16px 0;
	}

	.category-header.readonly {
		cursor: default;
	}

	.category-header.readonly:hover {
		background: var(--surface-1);
	}

	.tool-item.readonly {
		cursor: default;
		padding-left: 16px;
	}

	.tool-item.readonly:hover {
		background: transparent;
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

	/* Plugin agents */
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
		padding: 12px;
		background: var(--surface-1);
		border: 1px solid var(--border-subtle);
		border-radius: var(--radius-md, 10px);
		transition: all 200ms;
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

	/* Loading state */
	.loading-state {
		text-align: center;
		padding: 32px;
		color: var(--text-secondary);
		font-size: var(--text-md, 0.875rem);
	}

	/* Scrollbar styling */
	.tools-list,
	.pf-agent-list,
	.plugin-agents-list {
		scrollbar-width: thin;
		scrollbar-color: var(--border-default) transparent;
	}

	.tools-list::-webkit-scrollbar,
	.pf-agent-list::-webkit-scrollbar,
	.plugin-agents-list::-webkit-scrollbar {
		width: 6px;
	}

	.tools-list::-webkit-scrollbar-track,
	.pf-agent-list::-webkit-scrollbar-track,
	.plugin-agents-list::-webkit-scrollbar-track {
		background: transparent;
	}

	.tools-list::-webkit-scrollbar-thumb,
	.pf-agent-list::-webkit-scrollbar-thumb,
	.plugin-agents-list::-webkit-scrollbar-thumb {
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

	@media (max-width: 639px) {
		.fp-model-initial {
			font-size: 0.875rem;
		}
	}
</style>
