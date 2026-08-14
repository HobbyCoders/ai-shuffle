<script lang="ts">
	/**
	 * ProfileCard - Profile management in a card format
	 *
	 * Replaces ProfileModal with two view modes:
	 * - List view: Browse, search, and manage profiles
	 * - Form view: Edit or create profiles with tabbed interface
	 */
	import AIGenerateDialog from './AIGenerateDialog.svelte';
	import ConfirmDialog from '$lib/components/ui/ConfirmDialog.svelte';
	import StudioEditorShell from '$lib/components/layout/StudioEditorShell.svelte';
	import { toast } from '$lib/stores/toast';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile, type AgentExport } from '$lib/api/client';
	import { Users, Wrench, Bot, Settings, FileText, SlidersHorizontal, Check } from 'lucide-svelte';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import {
		getDefaultModel,
		getModelBadgeClass as getModelBadgeClassShared,
		getModelLabel,
		isPresetModel,
		normalizeModel
	} from '$lib/constants/models';
	import './card-design-system.css';

	// Sub-components
	import ProfileForm from './profiles/ProfileForm.svelte';
	import BuildProfileGrid from '$lib/components/layout/build/BuildProfileGrid.svelte';
	import { computeAiToolCount, computeEffectiveAiTools } from './profiles/toolAccess';

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

	interface AgentTemplate {
		id: string;
		name: string;
		description: string;
		prompt: string;
		tools?: string[];
	}

	interface GroupState {
		profiles?: {
			groups?: { name: string }[];
			assignments?: Record<string, string>;
		};
	}

	interface Props {
		mobile?: boolean;
		/** When embedded in the Build page, render the header-less card grid
		 *  instead of the FeaturePageLayout list. The editor (form) is unchanged. */
		embedded?: boolean;
		/** Reports whether the section is in a full-canvas detail/editor state so
		 *  the Build page can hide its heading + sub-nav while editing. */
		onDetailChange?: (detail: boolean) => void;
		/** One-shot deep-link: open this profile's editor once, after data loads. */
		initialSelectedId?: string | null;
	}

	let {
		mobile = false,
		embedded = false,
		onDetailChange,
		initialSelectedId
	}: Props = $props();

	// Consume the deep-link exactly once, after the target profile has loaded.
	let deepLinkConsumed = false;
	$effect(() => {
		if (!initialSelectedId || deepLinkConsumed) return;
		const target = profiles.find((p) => p.id === initialSelectedId);
		if (!target) return; // profiles not loaded yet — wait for the next run
		deepLinkConsumed = true;
		openEditForm(target);
		layout.clearFeatureItemId();
	});

	// Internal data state - loaded from stores/API
	let allSubagents = $state<Subagent[]>([]);
	let availableTools = $state<ToolsResponse>({ all_tools: [], categories: [] });
	// Built-in system-prompt templates (GET /agents/templates) — offered when
	// creating a new agent so the prompt can start from a curated preset.
	let templates = $state<AgentTemplate[]>([]);
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
			const [subagentsResult, toolsResult, templatesResult] = await Promise.all([
				api.get<Subagent[]>('/agents').catch((err) => {
					console.warn('Failed to load agents:', err);
					return [];
				}),
				api.get<ToolsResponse>('/tools?refresh=true').catch((err) => {
					console.warn('Failed to load tools:', err);
					return { all_tools: [], categories: [] };
				}),
				api.get<AgentTemplate[]>('/agents/templates').catch((err) => {
					console.warn('Failed to load agent templates:', err);
					return [] as AgentTemplate[];
				})
			]);
			allSubagents = subagentsResult;
			availableTools = toolsResult;
			templates = templatesResult;
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

	// Tell the Build page when we enter/leave the editor (form takes the canvas)
	// so it can hide its heading + sub-nav while editing.
	$effect(() => {
		if (embedded) onDetailChange?.(viewMode === 'form');
	});

	// Profile form state — omni harness options only (ruling R3). SDK-era
	// fields (thinking mode, effort, setting sources, checkpointing, …) are
	// gone: the omni harness ignores them.
	let profileForm = $state({
		id: '',
		name: '',
		description: '',
		model: getDefaultModel(),
		provider: null as string | null,
		permission_mode: 'default',
		allowed_tools: [] as string[],
		disallowed_tools: [] as string[],
		enabled_agents: [] as string[],
		memory: '',
		include_partial_messages: true,
		system_prompt_content: '',
		enabled_ai_tools: [] as string[],
		cwd: '',
		add_dirs: '',
		env_vars: [] as { key: string; value: string }[],
		app_config_tools: false,
		computer_use_enabled: false
	});

	// Common environment variable presets with valid ranges
	const envVarPresets = [
		{ key: 'CLAUDE_CODE_MAX_OUTPUT_TOKENS', placeholder: '1 - 64000' },
		{ key: 'MAX_THINKING_TOKENS', placeholder: '1024 - 128000' },
		{ key: 'CLAUDE_CODE_AUTO_COMPACT_WINDOW', placeholder: 'tokens (context-window threshold to auto-compact)' },
		{ key: 'MAX_MCP_OUTPUT_TOKENS', placeholder: '1 - 25000 (default)' },
		{ key: 'BASH_DEFAULT_TIMEOUT_MS', placeholder: 'ms (default: 120000)' },
		{ key: 'BASH_MAX_TIMEOUT_MS', placeholder: 'ms (default: 600000)' },
		{ key: 'MCP_TIMEOUT', placeholder: 'ms for server startup' },
		{ key: 'MCP_TOOL_TIMEOUT', placeholder: 'ms for tool execution' },
		{ key: 'CLAUDE_CODE_STREAM_CLOSE_TIMEOUT', placeholder: 'ms for stream close (SDK 0.1.10+)' }
	];

	// Tool selection mode. The single visible dropdown drives both built-in and
	// AI tool behavior — there is no separate AI-tool mode (see ./profiles/toolAccess).
	let toolSelectionMode = $state<'all' | 'none' | 'allow' | 'disallow'>('all');
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

	// A pending "restore defaults" awaiting confirmation (replaces native confirm()).
	let pendingRevert = $state<string | null>(null);

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

	const aiToolCount = $derived(
		computeAiToolCount(toolSelectionMode, getAllAIToolIds(), profileForm.enabled_ai_tools)
	);

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
			model: getDefaultModel(),
			provider: null,
			permission_mode: 'default',
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			memory: '',
			include_partial_messages: true,
			system_prompt_content: '',
			enabled_ai_tools: [],
			cwd: '',
			add_dirs: '',
			env_vars: [],
			app_config_tools: false,
			computer_use_enabled: false
		};
		toolSelectionMode = 'all';
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

		// Prefer the explicitly persisted mode when present (round-trips even when
		// the tool list is empty, e.g. "Allow Only Selected" with nothing picked).
		// Fall back to inference for profiles saved before tool_selection_mode existed.
		const savedMode = config.tool_selection_mode;
		if (savedMode === 'all' || savedMode === 'none' || savedMode === 'allow' || savedMode === 'disallow') {
			toolSelectionMode = savedMode;
		} else if (Array.isArray(config.allowed_tools) && config.allowed_tools.length === 0) {
			toolSelectionMode = 'none';
		} else if (allowedTools.length > 0) {
			toolSelectionMode = 'allow';
		} else if (disallowedTools.length > 0) {
			toolSelectionMode = 'disallow';
		} else {
			toolSelectionMode = 'all';
		}

		// A provider model (e.g. "openai/gpt-5.6") isn't a Claude preset, but it's
		// also not free-text custom — it lives in its own optgroup. Only fall back
		// to custom-model mode when no provider is set.
		customModelMode = !config.provider && !isPresetModel(config.model || getDefaultModel());

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			model: normalizeModel(config.model) || getDefaultModel(),
			provider: config.provider ?? null,
			permission_mode: config.permission_mode || 'default',
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			// Invoke allowlist: hydrate the stored ids as-is (minus self) — never
			// filter against allSubagents here, it may not have loaded yet and an
			// empty match would wipe the allowlist on save. Stale ids of deleted
			// agents are pruned at save time instead.
			enabled_agents: ((config as any).enabled_agents || []).filter(
				(id: string) => id !== profile.id
			),
			memory: (config as any).memory || '',
			include_partial_messages: config.include_partial_messages !== false,
			// Legacy preset-shaped prompts (`{type:'preset', append}`) surface their
			// append text as the prompt — per ruling A12 the configured prompt IS
			// the base, so saving converts them to the custom shape.
			system_prompt_content: sp.content || sp.append || '',
			enabled_ai_tools: enabledAITools,
			cwd: (config as any).cwd || '',
			add_dirs: ((config as any).add_dirs || []).join(', '),
			env_vars: Object.entries((config as any).env || {}).map(([key, value]) => ({ key, value: value as string })),
			app_config_tools: (config as any).app_config_tools || false,
			computer_use_enabled: !!(config as any).computer_use?.enabled
		};

		viewMode = 'form';
		activeTab = 'general';

		loadAvailableAITools();
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
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter((id) => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
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
		// Free-text custom models are always native Anthropic — a provider
		// selection only comes from the dedicated provider optgroup.
		if (custom) profileForm.provider = null;
	}

	// Apply a built-in template to the (new) agent's prompt. Explicit user action
	// only — never auto-applied. Fills the custom-prompt content and, when the
	// template carries a tool list, the allow-list; the user can edit freely after.
	function applyTemplate(templateId: string) {
		const tpl = templates.find((t) => t.id === templateId);
		if (!tpl) return;
		profileForm.system_prompt_content = tpl.prompt;
		if (Array.isArray(tpl.tools) && tpl.tools.length > 0) {
			toolSelectionMode = 'allow';
			profileForm.allowed_tools = [...tpl.tools];
			profileForm.disallowed_tools = [];
		}
	}

	// Save profile
	async function saveProfile() {
		if (!profileForm.name) return;

		const config: any = {
			model: profileForm.model,
			permission_mode: profileForm.permission_mode,
			// model_providers.id, or omitted (→ native Anthropic) when unset.
			...(profileForm.provider ? { provider: profileForm.provider } : {}),
			include_partial_messages: profileForm.include_partial_messages,
			app_config_tools: profileForm.app_config_tools
		};

		// Persist the dropdown mode explicitly so it round-trips even when the tool
		// list is empty (e.g. "Allow Only Selected" before any tool is picked).
		// The runtime still derives behavior from allowed_tools/disallowed_tools.
		config.tool_selection_mode = toolSelectionMode;
		if (toolSelectionMode === 'none') {
			config.allowed_tools = [];
		} else if (toolSelectionMode === 'allow') {
			config.allowed_tools = profileForm.allowed_tools;
		} else if (toolSelectionMode === 'disallow') {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		// Invoke allowlist (empty = may invoke any agent). Prune ids of deleted
		// agents, but only when the agent list actually loaded — pruning against
		// an empty/unloaded list would wipe the allowlist.
		config.enabled_agents = allSubagents.length > 0
			? profileForm.enabled_agents.filter((id) => allSubagents.some((a) => a.id === id))
			: profileForm.enabled_agents;
		// Per-agent memory (plain text). Omit when empty so clearing it persists.
		if (profileForm.memory.trim()) config.memory = profileForm.memory;
		if (profileForm.cwd.trim()) config.cwd = profileForm.cwd;
		if (profileForm.add_dirs.trim()) {
			config.add_dirs = profileForm.add_dirs
				.split(',')
				.map((d) => d.trim())
				.filter(Boolean);
		}

		const validEnvVars = profileForm.env_vars.filter(v => v.key.trim());
		if (validEnvVars.length > 0) {
			config.env = validEnvVars.reduce((acc, { key, value }) => {
				acc[key.trim()] = value;
				return acc;
			}, {} as Record<string, string>);
		}

		const effectiveAITools = computeEffectiveAiTools(
			toolSelectionMode,
			getAllAIToolIds(),
			profileForm.enabled_ai_tools
		);
		config.ai_tools = effectiveAITools.length > 0
			? Object.fromEntries(effectiveAITools.map((id) => [id, true]))
			: {};

		config.computer_use = { enabled: profileForm.computer_use_enabled };

		// The configured prompt IS the base (ruling A12) — always the custom
		// shape. Env/instructions/security are compiled in at runtime by omni,
		// so there is no preset mode and no inject_env_details flag anymore.
		config.system_prompt = {
			type: 'custom',
			content: profileForm.system_prompt_content
		};

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
			toast.push('Failed to save agent: ' + (e.detail || e.message || 'Unknown error'), { tone: 'error' });
			return;
		}

		viewMode = 'list';
		resetForm();
	}

	function handleRevertProfile(profileId: string) {
		const profile = profiles.find(p => p.id === profileId);
		if (!profile?.is_builtin) return;
		pendingRevert = profileId;
	}

	async function confirmRevert() {
		const profileId = pendingRevert;
		pendingRevert = null;
		if (!profileId) return;

		try {
			await tabsStore.restoreProfile(profileId);
		} catch (e: any) {
			toast.push('Failed to restore defaults: ' + (e.detail || e.message || 'Unknown error'), { tone: 'error' });
		}
	}

	// Export / Import
	async function handleExportProfile(profileId: string) {
		try {
			await exportAgent(profileId);
		} catch (e: any) {
			toast.push('Export failed: ' + (e.detail || 'Unknown error'), { tone: 'error' });
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
			toast.push(`Successfully imported agent: ${profile.name}`, { tone: 'success' });
		} catch (err: any) {
			toast.push('Import failed: ' + (err.detail || err.message || 'Invalid file format'), { tone: 'error' });
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

		// Legacy exports may carry a preset-shaped prompt — its append text
		// becomes the configured prompt (ruling A12). SDK-era fields in the
		// export (thinking_mode, setting_sources, …) are ignored.
		const spContent =
			agent.system_prompt_type === 'custom'
				? agent.system_prompt_content || ''
				: agent.system_prompt_append || '';

		customModelMode = !isPresetModel(agent.model || getDefaultModel());

		profileForm = {
			id: '',
			name: agent.name,
			description: agent.description || '',
			model: normalizeModel(agent.model) || getDefaultModel(),
			// AI-generated/imported profiles are always native Anthropic today —
			// the export format doesn't carry a provider id.
			provider: null,
			permission_mode: agent.permission_mode || 'default',
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: agent.enabled_agents || [],
			memory: agent.memory || '',
			include_partial_messages: agent.include_partial_messages !== false,
			system_prompt_content: spContent,
			enabled_ai_tools: enabledAITools,
			cwd: '',
			add_dirs: '',
			env_vars: agent.env ? Object.entries(agent.env).map(([key, value]) => ({ key, value: value as string })) : [],
			app_config_tools: false,
			computer_use_enabled: false
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


<!-- Shared hidden file input (used by both the embedded grid and the list view) -->
<input type="file" accept=".json" bind:this={profileImportInput} onchange={handleImport} class="hidden" />

{#if viewMode === 'form'}
		<StudioEditorShell
			title={editingProfile ? editingProfile.name : 'New Agent'}
			subtitle={editingProfile ? `Agent · ${getModelDisplay(profileForm.model)}` : 'Create a new agent'}
			backLabel="Agents"
			onBack={backToList}
			sections={tabs.map((t) => ({ id: t.id, label: t.label, icon: tabIcons[t.id as keyof typeof tabIcons] }))}
			active={activeTab}
			onSelect={(id) => (activeTab = id)}
			railLabel="Agent sections"
		>

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
				{templates}
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
				onApplyTemplate={applyTemplate}
				onAssignToGroup={assignToGroup}
				onRemoveFromGroup={removeFromGroup}
				onCreateGroup={createGroup}
				onDeleteGroup={deleteGroup}
				{getCategorySelectionState}
				{getAICategorySelectionState}
				{getModelDisplay}
			/>

			{#snippet actions()}
				<button type="button" class="ed-btn primary" onclick={saveProfile} disabled={!profileForm.name}>
					<Check size={14} />
					<span>Save</span>
				</button>
			{/snippet}
		</StudioEditorShell>
	{:else}
		<!-- Build page: header-less card grid (heading + sub-nav live in BuildPage) -->
		<BuildProfileGrid
			{profiles}
			{groups}
			{allSubagents}
			{builtinAgentIds}
			importing={profileImporting}
			onEditProfile={openEditForm}
			onNewProfile={openNewForm}
			onGenerate={() => showGenerateDialog = true}
			onImport={triggerImport}
			onExportProfile={handleExportProfile}
			onDeleteProfile={handleDeleteProfile}
			onRevertProfile={handleRevertProfile}
			{getModelDisplay}
			{getProfileToolCount}
		/>
	{/if}

<!-- AI Generate Dialog -->
<AIGenerateDialog
	type="profile"
	isOpen={showGenerateDialog}
	onClose={() => showGenerateDialog = false}
	onGenerated={handleGeneratedProfile}
/>

<!-- Restore-defaults confirmation -->
<ConfirmDialog
	open={!!pendingRevert}
	title="Restore defaults"
	message="Restore this agent to its original default settings? Your customizations will be lost."
	confirmLabel="Restore"
	danger
	onConfirm={confirmRevert}
	onCancel={() => (pendingRevert = null)}
/>


<style>
	.hidden {
		display: none;
	}
</style>
