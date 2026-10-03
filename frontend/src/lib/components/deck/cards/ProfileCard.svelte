<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ProfileCard: the Agents page, a ledger. One list of agents; the open agent
	 * expands in place into its editor (essentials, then collapsible sections).
	 * Edits save as you type. Delete and restore confirm inline in the row.
	 */
	import AIGenerateDialog from './AIGenerateDialog.svelte';
	import { notify } from '$lib/stores/notifications';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile, type AgentExport } from '$lib/api/client';
	import { Download, RotateCcw, Trash2 } from 'lucide-svelte';
	import { onMount, tick } from 'svelte';
	import { refreshAssistant } from '$lib/stores/assistant';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { isAdmin as isAdminStore } from '$lib/stores/auth.svelte';
	import {
		EMPTY_TOOL_SURFACE,
		fetchToolSurface,
		type ToolSurface,
		type ToolSurfaceGroup
	} from '$lib/api/toolSurface';
	import {
		getDefaultModel,
		normalizeModel,
		NATIVE_PROVIDER_KEY
	} from '$lib/constants/models';
	import { computerUse } from '$lib/stores/computerUse.svelte';

	// Sub-components
	import ProfileForm, { type AgentSection } from './profiles/ProfileForm.svelte';
	import AgentList, { type AgentSpec, type PendingConfirm } from '$lib/components/layout/build/AgentList.svelte';
	import {
		type SelectabilityContext,
		type ToolSelectionMode,
		countAllowedTools,
		profileToolCount,
		resolveToolMode,
		toggleGroupSelection,
		toggleToolSelection
	} from './profiles/toolSurface';

	// Types
	interface Subagent {
		id: string;
		name: string;
		description: string;
		config?: {
			model?: string;
			provider?: string | null;
			provider_models?: Record<string, string>;
		};
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
		/** One-shot deep-link: open this profile's editor once, after data loads. */
		initialSelectedId?: string | null;
	}

	let { initialSelectedId }: Props = $props();

	// The list marks the personal assistant and shows each agent's avatar.
	onMount(() => {
		if ($isAdminStore) void refreshAssistant().catch(() => {});
	});

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
	// The live tool surface (T1). One inventory for the picker and the card
	// badges — never a hardcoded list, never split across two cards.
	let toolSurface = $state<ToolSurface>(EMPTY_TOOL_SURFACE);
	let toolsLoading = $state(true);
	let toolsRefreshing = $state(false);
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

	async function loadData(quiet = false) {
		if (!quiet) loading = true;
		try {
			const [subagentsResult, surfaceResult, templatesResult] = await Promise.all([
				api.get<Subagent[]>('/agents').catch((err) => {
					console.warn('Failed to load agents:', err);
					return [];
				}),
				fetchToolSurface().catch((err) => {
					console.warn('Failed to load tool surface:', err);
					return EMPTY_TOOL_SURFACE;
				}),
				api.get<AgentTemplate[]>('/agents/templates').catch((err) => {
					console.warn('Failed to load agent templates:', err);
					return [] as AgentTemplate[];
				})
			]);
			allSubagents = subagentsResult;
			toolSurface = surfaceResult;
			templates = templatesResult;
		} catch (e) {
			console.error('Failed to load profile card data:', e);
		} finally {
			loading = false;
			toolsLoading = false;
		}
	}

	/** Header Refresh: re-discover the surface, busting the MCP enumeration cache. */
	async function refreshToolSurface() {
		if (toolsRefreshing) return;
		toolsRefreshing = true;
		try {
			toolSurface = await fetchToolSurface(true);
		} catch (e: any) {
			notify('Failed to refresh tools: ' + (e?.detail || e?.message || 'Unknown error'), {
				severity: 'error'
			});
		} finally {
			toolsRefreshing = false;
		}
	}

	// View state
	let viewMode = $state<'list' | 'form'>('list');
	let editingProfile = $state<Profile | null>(null);
	/** Editor sections expanded under the open agent. */
	let openSections = $state<AgentSection[]>([]);
	/**
	 * Id of an agent created from the draft row in this visit. It stays in the
	 * draft slot (not jumping into its sorted place) so typing keeps focus.
	 */
	let draftId = $state<string | null>(null);

	/** The open agent's id; '' while drafting a new one; null when none is open. */
	const selectedId = $derived(
		viewMode !== 'form' ? null : !editingProfile || editingProfile.id === draftId ? '' : editingProfile.id
	);
	const listProfiles = $derived(draftId ? profiles.filter((p) => p.id !== draftId) : profiles);
	let saveState = $state<'idle' | 'saving' | 'saved' | 'error'>('idle');

	// Profile form state — fluid harness options only (ruling R3). SDK-era
	// fields (thinking mode, checkpointing, …) are gone: the fluid harness
	// ignores them. effort, max_turns, and setting_sources are harness-honored
	// (setting_sources maps onto fluid's user/project/local scope filter).
	let profileForm = $state({
		id: '',
		name: '',
		description: '',
		provider_models: { [NATIVE_PROVIDER_KEY]: getDefaultModel() } as Record<string, string>,
		max_turns: null as number | null,
		allowed_tools: [] as string[],
		disallowed_tools: [] as string[],
		enabled_agents: [] as string[],
		// 'all' = unrestricted; 'custom' = enabled_agents is the exact allowlist.
		agent_invoke_mode: 'all' as 'all' | 'custom',
		include_partial_messages: true,
		system_prompt_content: '',
		cwd: '',
		add_dirs: '',
		env_vars: [] as { key: string; value: string }[],
		// null = all scopes (default, key omitted); [] = filesystem-isolated.
		setting_sources: null as string[] | null
	});

	// Tool selection mode (T2) — the runtime authority for what the saved list
	// means. 'all'/'none' carry no list at all; 'allow'/'disallow' each own one.
	let toolSelectionMode = $state<ToolSelectionMode>('all');

	let saveTimer: ReturnType<typeof setTimeout> | null = null;
	let saveInFlight = false;
	let persistDirty = false;
	let inFlightPromise: Promise<void> | null = null;
	let lastPersisted = '';

	function formSnapshot() {
		return JSON.stringify({ form: profileForm, toolSelectionMode });
	}

	function markFormClean() {
		lastPersisted = formSnapshot();
	}

	// Import handling
	let profileImportInput: HTMLInputElement | undefined = $state();
	let profileImporting = $state(false);
	let showGenerateDialog = $state(false);

	// A delete or "restore defaults" awaiting the inline confirm in its row.
	let confirm = $state<PendingConfirm | null>(null);

	// Which rows the editor may actually write into a list: computer use follows
	// the global toggle, admin-only groups follow the signed-in user.
	const selectability = $derived<SelectabilityContext>({
		computerUseEnabled: computerUse.enabled,
		isAdmin: $isAdminStore
	});

	const activeToolList = $derived(
		toolSelectionMode === 'allow'
			? profileForm.allowed_tools
			: toolSelectionMode === 'disallow'
				? profileForm.disallowed_tools
				: []
	);

	const effectiveToolCount = $derived(
		countAllowedTools(toolSelectionMode, toolSurface.groups, activeToolList, selectability)
	);

	/**
	 * The count badge on an agent card. Reads the saved mode and its list against
	 * the live surface (T6) — pre-migration rows without a mode fall back to the
	 * legacy reading T2 spells out, so they count as what they actually were.
	 */
	function getProfileToolCount(profile: Profile): number {
		return profileToolCount(profile.config, toolSurface, computerUse.enabled, $isAdminStore);
	}

	/**
	 * How many agents this one may dispatch as subagents. 'custom' declares the
	 * allowlist pruned to agents that still exist (0 really means none); legacy
	 * rows without a mode and a non-empty list declare exactly it; everything
	 * else declares every agent but itself.
	 */
	function invokableCount(id: string, mode: string | undefined, enabledIds: string[] | undefined): number {
		const enabled = (enabledIds ?? []).filter((x) => allSubagents.some((a) => a.id === x)).length;
		if (mode === 'custom') return enabled;
		if (!mode && enabled > 0) return enabled;
		return allSubagents.filter((a) => a.id !== id).length;
	}

	/** Spec line for a row: live from the form for the open agent, saved config otherwise. */
	function getSpec(profile: Profile): AgentSpec {
		if (viewMode === 'form' && editingProfile?.id === profile.id) {
			return {
				tools: effectiveToolCount,
				agents: invokableCount(profile.id, profileForm.agent_invoke_mode, profileForm.enabled_agents)
			};
		}
		const config = (profile.config ?? {}) as any;
		return {
			tools: getProfileToolCount(profile),
			agents: invokableCount(profile.id, config.agent_invoke_mode, config.enabled_agents)
		};
	}

	function resetForm() {
		profileForm = {
			id: '',
			name: '',
			description: '',
			provider_models: { [NATIVE_PROVIDER_KEY]: getDefaultModel() },
			max_turns: null,
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			agent_invoke_mode: 'all',
			include_partial_messages: true,
			system_prompt_content: '',
			cwd: '',
			add_dirs: '',
			env_vars: [],
			setting_sources: null
		};
		toolSelectionMode = 'all';
		openSections = [];
	}

	async function openNewForm() {
		await flushPendingPersist();
		remoteChange = '';
		saveState = 'idle';
		confirm = null;
		draftId = null;
		resetForm();
		editingProfile = null;
		viewMode = 'form';
		markFormClean();
		await revealRow('');
	}

	/** Scroll an agent's row (or one of its sections) into view once rendered. */
	async function revealRow(id: string, section?: AgentSection) {
		await tick();
		const target = section
			? document.getElementById(`agx-sec-${section}`)
			: document.querySelector(`[data-agent-row="${id.replace(/["\\]/g, '\\$&')}"]`);
		target?.scrollIntoView?.({ block: section ? 'start' : 'nearest', behavior: 'smooth' });
	}

	function toggleSection(id: AgentSection) {
		openSections = openSections.includes(id) ? openSections.filter((s) => s !== id) : [...openSections, id];
	}

	/**
	 * Open an agent in place, saving the open one's pending edits first. With a
	 * section (a click on "12 tools" in its spec line), open straight to it.
	 */
	async function selectProfile(profile: Profile, section?: string) {
		const sec = section as AgentSection | undefined;
		const alreadyOpen = viewMode === 'form' && editingProfile?.id === profile.id;
		if (!alreadyOpen) {
			await flushPendingPersist();
			remoteChange = '';
			saveState = 'idle';
			confirm = null;
			draftId = null;
			openEditForm(profile);
		}
		if (sec && !openSections.includes(sec)) openSections = [...openSections, sec];
		await revealRow(profile.id, sec);
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
		const allowedTools = config.allowed_tools || [];
		const disallowedTools = config.disallowed_tools || [];

		// The persisted mode wins outright so the dropdown round-trips even when
		// its list is empty ("Allow Only Selected" with nothing picked). Rows saved
		// before tool_selection_mode existed fall back to T2's legacy reading.
		toolSelectionMode = resolveToolMode(config);

		const providerModels = { ...((config as any).provider_models || {}) } as Record<string, string>;
		const legacyProviderKey = config.provider || NATIVE_PROVIDER_KEY;
		if (config.model && !providerModels[legacyProviderKey]) {
			providerModels[legacyProviderKey] = normalizeModel(config.model) || config.model;
		}

		profileForm = {
			id: profile.id,
			name: profile.name,
			description: profile.description || '',
			provider_models: providerModels,
			max_turns: config.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			// Invoke allowlist: hydrate the stored ids as-is (minus self) — never
			// filter against allSubagents here, it may not have loaded yet and an
			// empty match would wipe the allowlist on save. Stale ids of deleted
			// agents are pruned at save time instead.
			enabled_agents: ((config as any).enabled_agents || []).filter(
				(id: string) => id !== profile.id
			),
			// Stored mode wins; legacy rows (no mode) infer it from the allowlist —
			// a non-empty list meant "restrict to these", empty meant "any agent".
			agent_invoke_mode:
				(config as any).agent_invoke_mode === 'custom'
					? 'custom'
					: (config as any).agent_invoke_mode === 'all'
						? 'all'
						: ((config as any).enabled_agents?.length ?? 0) > 0
							? 'custom'
							: 'all',
			include_partial_messages: config.include_partial_messages !== false,
			// Legacy preset-shaped prompts (`{type:'preset', append}`) surface their
			// append text as the prompt — per ruling A12 the configured prompt IS
			// the base, so saving converts them to the custom shape.
			system_prompt_content: sp.content || sp.append || '',
			cwd: (config as any).cwd || '',
			add_dirs: ((config as any).add_dirs || []).join(', '),
			env_vars: Object.entries((config as any).env || {}).map(([key, value]) => ({ key, value: value as string })),
			setting_sources: Array.isArray((config as any).setting_sources)
				? ((config as any).setting_sources as string[])
				: null
		};

		viewMode = 'form';
		openSections = [];
		markFormClean();
	}

	/** Collapse the open agent, saving pending edits first. */
	async function closeEditor() {
		await flushPendingPersist();
		viewMode = 'list';
		confirm = null;
		draftId = null;
		resetForm();
		editingProfile = null;
	}

	// Tool helpers. Both write into whichever list the current mode owns; in
	// 'all'/'none' the picker is read-only and never calls them.
	function setActiveToolList(next: string[]) {
		if (toolSelectionMode === 'allow') profileForm.allowed_tools = next;
		else if (toolSelectionMode === 'disallow') profileForm.disallowed_tools = next;
	}

	function toggleTool(group: ToolSurfaceGroup, toolName: string) {
		setActiveToolList(
			toggleToolSelection(activeToolList, group, toolName, selectability)
		);
	}

	function toggleGroup(group: ToolSurfaceGroup) {
		setActiveToolList(toggleGroupSelection(activeToolList, group, selectability));
	}

	// Agent helpers
	function toggleAgent(agentId: string) {
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter((id) => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
		}
	}

	// Handle form field changes from sub-component
	function handleFormChange(field: string, value: any) {
		(profileForm as any)[field] = value;
	}

	// Handle tool selection mode changes from sub-component
	function handleToolSelectionModeChange(mode: ToolSelectionMode) {
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

	function buildProfilePayload() {
		// `PUT /agents/{id}` replaces the typed config, so a schema key left out
		// here is genuinely cleared — that is how 'all'/'none' drop a stale list and
		// how hidden legacy bindings (model, provider, permission mode, effort) are
		// retired. The backend keeps keys outside the schema, including the avatar
		// (owned by the identity PATCH), so neither is sent from this snapshot.
		const config: any = {
			include_partial_messages: profileForm.include_partial_messages
		};
		// An agent carries no model, provider, permission mode or effort: a chat
		// picks those when it starts, and internal one-shot generation reads the
		// text-generation setting under Settings > Default models.
		//
		// Subagent runtime map — the ONE model binding an agent still owns, used
		// when another agent dispatches it as a Task subagent. Blank values mean
		// unavailable and are pruned; stale assignments for temporarily absent
		// providers stay intact. Nothing is injected from a top-level binding any
		// more: the map is exactly what the Agents tab shows.
		const providerModels = Object.fromEntries(
			Object.entries(profileForm.provider_models)
				.map(([key, value]) => [key.trim(), value.trim()])
				.filter(([key, value]) => key && value)
		);
		config.provider_models = providerModels;
		// Per-turn model-call cap (native legs).
		if (profileForm.max_turns) config.max_turns = profileForm.max_turns;
		// Setting sources (fluid scope filter). null = default (all scopes, key
		// omitted); an explicit list — including [] — round-trips as-is.
		if (profileForm.setting_sources !== null) config.setting_sources = profileForm.setting_sources;

		// Tool access (T2): the mode IS the authority, so 'all' and 'none' write no
		// list at all — the full-replace PUT clears whatever was there. 'allow' and
		// 'disallow' each write only their own list. Entries may be exact names or
		// the `mcp__<server>__*` pattern a whole-server selection saves.
		config.tool_selection_mode = toolSelectionMode;
		if (toolSelectionMode === 'allow') {
			config.allowed_tools = profileForm.allowed_tools;
		} else if (toolSelectionMode === 'disallow') {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		// Invoke allowlist. Prune ids of deleted agents, but only when the agent
		// list actually loaded — pruning against an empty/unloaded list would wipe
		// the allowlist.
		config.enabled_agents = allSubagents.length > 0
			? profileForm.enabled_agents.filter((id) => allSubagents.some((a) => a.id === id))
			: profileForm.enabled_agents;
		// Persist the invoke-grant mode explicitly so "custom with nothing selected"
		// (= may invoke no agents) round-trips instead of reading as unrestricted.
		config.agent_invoke_mode = profileForm.agent_invoke_mode;
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

		// `ai_tools` and `app_config_tools` are no longer independent switches (T5):
		// the AI-tools and app-config groups are permitted through the one picker,
		// so the editor stops writing either field. Computer use is a GLOBAL
		// toggle (Settings > Security), so nothing is written per-agent here.

		// The configured prompt IS the base (ruling A12) — always the custom
		// shape. Env/instructions/security are compiled in at runtime by fluid,
		// so there is no preset mode and no inject_env_details flag anymore.
		config.system_prompt = {
			type: 'custom',
			content: profileForm.system_prompt_content
		};

		const derivedId = profileForm.name.toLowerCase().replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
		return {
			id: editingProfile ? editingProfile.id : derivedId,
			name: profileForm.name,
			description: profileForm.description || undefined,
			config
		};
	}

	async function writeProfile() {
		const writtenSnapshot = formSnapshot();
		const data = buildProfilePayload();
		if (editingProfile) {
			await tabsStore.updateProfile(data.id, {
				name: data.name,
				description: data.description,
				config: data.config
			});
		} else {
			await tabsStore.createProfile(data);
			draftId = data.id;
			editingProfile = {
				id: data.id,
				name: data.name,
				description: data.description ?? null,
				is_builtin: false,
				is_modified: false,
				config: data.config,
				created_at: '',
				updated_at: ''
			};
		}
		if (writtenSnapshot === formSnapshot()) lastPersisted = writtenSnapshot;
	}

	function schedulePersist() {
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = setTimeout(() => {
			saveTimer = null;
			void persistNow();
		}, 500);
	}

	function persistNow(): Promise<void> {
		persistDirty = true;
		if (saveInFlight && inFlightPromise) return inFlightPromise;
		saveInFlight = true;
		inFlightPromise = (async () => {
			try {
				while (persistDirty) {
					persistDirty = false;
					if (!profileForm.name) continue;
					saveState = 'saving';
					try {
						await writeProfile();
						saveState = 'saved';
					} catch (e: any) {
						saveState = 'error';
						notify('Failed to save agent: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
					}
				}
			} finally {
				saveInFlight = false;
				inFlightPromise = null;
				if (persistDirty) await persistNow();
			}
		})();
		return inFlightPromise;
	}

	async function flushPendingPersist() {
		const hadTimer = !!saveTimer;
		if (saveTimer) {
			clearTimeout(saveTimer);
			saveTimer = null;
		}
		if (hadTimer || saveInFlight) {
			await persistNow();
		}
	}

	$effect(() => {
		const snap = formSnapshot();
		if (viewMode !== 'form') return;
		if (snap === lastPersisted) return;
		schedulePersist();
	});

	function askRevert(profile: Profile) {
		if (!profile.is_builtin) return;
		confirm = { id: profile.id, kind: 'revert' };
	}

	function askDelete(profile: Profile) {
		if (profile.is_builtin) return;
		confirm = { id: profile.id, kind: 'delete' };
	}

	function runConfirm() {
		const pending = confirm;
		confirm = null;
		if (!pending) return;
		void (pending.kind === 'delete' ? deleteProfile(pending.id) : revertProfile(pending.id));
	}

	async function revertProfile(profileId: string) {
		try {
			// Drop unsaved edits first so autosave can't re-apply them over the defaults.
			if (editingProfile?.id === profileId) {
				if (saveTimer) clearTimeout(saveTimer);
				saveTimer = null;
				persistDirty = false;
				if (inFlightPromise) await inFlightPromise;
			}
			await tabsStore.restoreProfile(profileId);
			const fresh = profiles.find((p) => p.id === profileId);
			if (fresh && viewMode === 'form' && editingProfile?.id === profileId) {
				const sections = openSections;
				openEditForm(fresh);
				openSections = sections;
			}
		} catch (e: any) {
			notify('Failed to restore defaults: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
		}
	}

	// Export / Import
	async function handleExportProfile(profileId: string) {
		try {
			await exportAgent(profileId);
		} catch (e: any) {
			notify('Export failed: ' + (e.detail || 'Unknown error'), { severity: 'error' });
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
			notify(`Successfully imported agent: ${profile.name}`, { severity: 'success' });
		} catch (err: any) {
			notify('Import failed: ' + (err.detail || err.message || 'Invalid file format'), { severity: 'error' });
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

		// Export/generated payloads carry the fields flat (no `.config`); a stored
		// mode wins, older ones fall back to T2's legacy reading of the lists.
		toolSelectionMode = resolveToolMode(agent as any);

		// Legacy exports may carry a preset-shaped prompt — its append text
		// becomes the configured prompt (ruling A12). Remaining SDK-era fields
		// in the export (thinking_mode, …) are ignored; setting_sources is
		// honored again now that the fluid harness maps it.
		const spContent =
			agent.system_prompt_type === 'custom'
				? agent.system_prompt_content || ''
				: agent.system_prompt_append || '';

		profileForm = {
			id: '',
			name: agent.name,
			description: agent.description || '',
			// A legacy export's top-level model becomes the native SUBAGENT
			// binding — the only model an agent still carries.
			provider_models: {
				...(agent.provider_models || {}),
				[NATIVE_PROVIDER_KEY]: agent.provider_models?.[NATIVE_PROVIDER_KEY]
					|| normalizeModel(agent.model)
					|| getDefaultModel()
			},
			max_turns: agent.max_turns || null,
			allowed_tools: allowedTools,
			disallowed_tools: disallowedTools,
			enabled_agents: agent.enabled_agents || [],
			// Export/generated payloads carry the fields flat (no `.config`). Stored
			// mode wins; legacy payloads infer it from the allowlist.
			agent_invoke_mode:
				agent.agent_invoke_mode === 'custom'
					? 'custom'
					: agent.agent_invoke_mode === 'all'
						? 'all'
						: (agent.enabled_agents?.length ?? 0) > 0
							? 'custom'
							: 'all',
			include_partial_messages: agent.include_partial_messages !== false,
			system_prompt_content: spContent,
			cwd: '',
			add_dirs: '',
			env_vars: agent.env ? Object.entries(agent.env).map(([key, value]) => ({ key, value: value as string })) : [],
			setting_sources: Array.isArray((agent as any).setting_sources)
				? ((agent as any).setting_sources as string[])
				: null
		};

		viewMode = 'form';
		// A generated agent arrives with its prompt and tools filled; show them.
		openSections = ['prompt', 'tools'];
		markFormClean();
	}

	async function handleGeneratedProfile(data: AgentExport | unknown) {
		showGenerateDialog = false;
		await flushPendingPersist();
		remoteChange = '';
		saveState = 'idle';
		confirm = null;
		draftId = null;
		openFormFromGenerated(data as AgentExport);
		await revealRow('');
	}

	async function deleteProfile(profileId: string) {
		try {
			if (editingProfile?.id === profileId) {
				if (saveTimer) clearTimeout(saveTimer);
				saveTimer = null;
				persistDirty = false;
				viewMode = 'list';
				editingProfile = null;
				draftId = null;
				resetForm();
			}
			await tabsStore.deleteProfile(profileId);
		} catch (e: any) {
			notify('Failed to delete agent: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
		}
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
	let remoteChange = $state('');
	watchResources(['agents', 'templates', 'settings', 'mcp', 'plugins'], async () => {
		const selected = editingProfile;
		const snapshot = formSnapshot();
		await Promise.all([loadData(true), tabsStore.loadProfiles()]);
		if (!selected || editingProfile?.id !== selected.id || viewMode !== 'form') return;
		const fresh = profiles.find((profile) => profile.id === selected.id);
		if (saveTimer || saveInFlight || persistDirty || formSnapshot() !== lastPersisted || snapshot !== formSnapshot()) {
			if (!fresh || JSON.stringify(fresh) !== JSON.stringify(selected)) {
				remoteChange = fresh ? 'This agent changed elsewhere. Your unsaved changes are kept here.' : 'This agent was deleted elsewhere. Copy your unsaved changes before leaving.';
			}
			return;
		}
		if (fresh) {
			const sections = openSections;
			openEditForm(fresh);
			openSections = sections;
			remoteChange = '';
		} else {
			viewMode = 'list';
			editingProfile = null;
			draftId = null;
			remoteChange = 'This agent was deleted elsewhere.';
		}
	});
</script>

<!-- Shared hidden file input for Import -->
<input type="file" accept=".json" bind:this={profileImportInput} onchange={handleImport} class="hidden" />

<div class="ag">
	{#if remoteChange}<p role="status" class="ag-remote">{remoteChange}</p>{/if}
	<AgentList
		profiles={listProfiles}
		{groups}
		{selectedId}
		draftName={profileForm.name}
		{loading}
		{saveState}
		{confirm}
		importing={profileImporting}
		{getSpec}
		onSelect={selectProfile}
		onClose={closeEditor}
		onNew={openNewForm}
		onGenerate={() => (showGenerateDialog = true)}
		onImport={triggerImport}
		onExport={(p) => handleExportProfile(p.id)}
		onAskDelete={askDelete}
		onAskRevert={askRevert}
		onConfirm={runConfirm}
		onCancelConfirm={() => (confirm = null)}
	>
		{#snippet editor()}
			<ProfileForm
				{editingProfile}
				{profileForm}
				{toolSelectionMode}
				{groups}
				{allSubagents}
				{templates}
				{toolSurface}
				{toolsLoading}
				{toolsRefreshing}
				toolCount={effectiveToolCount}
				{openSections}
				onToggleSection={toggleSection}
				isAdmin={$isAdminStore}
				onFormChange={handleFormChange}
				onToolSelectionModeChange={handleToolSelectionModeChange}
				onToggleTool={toggleTool}
				onToggleGroup={toggleGroup}
				onRefreshTools={refreshToolSurface}
				onToggleAgent={toggleAgent}
				onApplyTemplate={applyTemplate}
				onAssignToGroup={assignToGroup}
				onRemoveFromGroup={removeFromGroup}
				onCreateGroup={createGroup}
				onDeleteGroup={deleteGroup}
			/>
			<footer class="ag-foot">
				{#if editingProfile}
					{@const current = editingProfile}
					<button type="button" class="ag-fbtn" onclick={() => handleExportProfile(current.id)}>
						<Download size={14} strokeWidth={1.8} />Export
					</button>
					{#if current.is_builtin && current.is_modified}
						<button type="button" class="ag-fbtn" onclick={() => askRevert(current)}>
							<RotateCcw size={14} strokeWidth={1.8} />Restore defaults
						</button>
					{/if}
					{#if !current.is_builtin}
						<button type="button" class="ag-fbtn danger" onclick={() => askDelete(current)}>
							<Trash2 size={14} strokeWidth={1.8} />Delete
						</button>
					{/if}
				{/if}
				<span class="ag-fspace"></span>
				<button type="button" class="ag-done" onclick={closeEditor}>{editingProfile ? 'Done' : 'Cancel'}</button>
			</footer>
		{/snippet}
	</AgentList>
</div>

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
	.ag {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		background: var(--background);
	}
	.ag > :global(.al) {
		flex: 1;
	}
	.ag-remote {
		flex: none;
		margin: 0;
		padding: 8px 28px;
		font-size: 0.8rem;
		color: var(--muted-foreground);
		border-bottom: 1px solid var(--border);
	}
	.ag-foot {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 4px;
		margin-top: 10px;
		padding: 10px 0 12px;
		border-top: 1px solid color-mix(in oklch, var(--border) 70%, transparent);
	}
	.ag-fspace {
		flex: 1;
	}
	.ag-fbtn {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		height: 30px;
		padding: 0 10px;
		border: none;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 500;
		cursor: pointer;
	}
	.ag-fbtn:hover {
		color: var(--foreground);
		background: var(--muted);
	}
	.ag-fbtn.danger:hover {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}
	.ag-done {
		height: 30px;
		padding: 0 16px;
		border: 1px solid var(--border);
		border-radius: 7px;
		background: var(--background);
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		font-weight: 600;
		cursor: pointer;
	}
	.ag-done:hover {
		background: var(--muted);
	}
	.ag-fbtn:focus-visible,
	.ag-done:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}
</style>
