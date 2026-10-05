<script lang="ts">
	/**
	 * ProfileCard: the Agents page (#1825). A Settings-style collection page:
	 * the agent list on the left (assistant, Yours, Built-in; search, an
	 * All / Yours / Built-in filter and a folder filter) and one agent's detail
	 * on the right (AgentDetail). On a phone, or at 600px wide or less, they
	 * are two steps with a back button (and Android Back).
	 *
	 * This component owns the data and the autosave: edits save as you type,
	 * a new agent saves once it has a name. Delete and restore confirm inline
	 * in the detail's danger zone.
	 */
	import { onMount, tick, untrack } from 'svelte';
	import { page } from '$app/state';
	import { pushState } from '$app/navigation';
	import { Bot, Plus } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import AIGenerateDialog from './AIGenerateDialog.svelte';
	import { notify } from '$lib/stores/notifications';
	import { api, type Profile, exportAgent, importAgent, parseAgentExportFile, type AgentExport } from '$lib/api/client';
	import { updateAgentIdentity, assistantError, type AvatarId } from '$lib/api/assistant';
	import { getSubagentDefault, putSubagentDefault, type SubagentDefault } from '$lib/api/subagentDefault';
	import { refreshAssistant, assistantState, agentIdentities } from '$lib/stores/assistant';
	import { profiles as profilesStore, appData as tabsStore } from '$lib/stores/app-data.svelte';
	import { groups as groupsStore } from '$lib/stores/groups.svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { isAdmin as isAdminStore } from '$lib/stores/auth.svelte';
	import { EMPTY_TOOL_SURFACE, fetchToolSurface, type ToolSurface, type ToolSurfaceGroup } from '$lib/api/toolSurface';
	import {
		getDefaultModel,
		getProviderDisplay,
		getProviders,
		normalizeModel,
		NATIVE_PROVIDER_KEY
	} from '$lib/constants/models';
	import { computerUse } from '$lib/stores/computerUse.svelte';
	import CollectionPage from '$lib/components/settings/shell/CollectionPage.svelte';
	import EmptyDetail from '$lib/components/settings/shell/EmptyDetail.svelte';
	import Segmented from '$lib/components/settings/rows/Segmented.svelte';
	import SelectField from '$lib/components/settings/rows/SelectField.svelte';
	import '$lib/components/settings/shell/settings.css';

	// Sub-components
	import AgentList from '$lib/components/layout/build/AgentList.svelte';
	import AgentDetail from './profiles/AgentDetail.svelte';
	import NewAgentDialog from './profiles/NewAgentDialog.svelte';
	import { modelLabel } from './profiles/AgentModelsTab.svelte';
	import {
		type AgentForm,
		type AgentTab,
		type AgentTemplate,
		type GroupState,
		type RosterAgent
	} from './profiles/agentForm';
	import {
		FOLDER_NONE,
		folderOf,
		isPinned,
		pinnedModel,
		pinnedProviders,
		profileModels,
		sectionAgents,
		uniqueAgentId,
		type AgentFilter
	} from './profiles/agentList';
	import {
		type SelectabilityContext,
		type ToolSelectionMode,
		countAllowedTools,
		resolveToolMode,
		selectableSurfaceTools,
		toggleGroupSelection,
		toggleToolSelection
	} from './profiles/toolSurface';

	interface Props {
		/** One-shot deep-link: open this profile's editor once, after data loads. */
		initialSelectedId?: string | null;
	}

	let { initialSelectedId }: Props = $props();

	/** Panes stack at or below this page width (and always in the phone app). */
	const STACK_WIDTH = 600;
	const SELECTED_KEY = 'agents.selected';

	// The list marks the personal assistant and shows each agent's avatar.
	let identityError = $state('');
	async function loadIdentities() {
		identityError = '';
		try {
			await refreshAssistant();
		} catch (e) {
			identityError = assistantError(e);
		}
	}
	onMount(() => {
		if ($isAdminStore) void loadIdentities();
	});

	// Internal data state - loaded from stores/API
	let allSubagents = $state<RosterAgent[]>([]);
	// The live tool surface (T1). One inventory for the picker and the facts.
	let toolSurface = $state<ToolSurface>(EMPTY_TOOL_SURFACE);
	let toolsLoading = $state(true);
	let toolsRefreshing = $state(false);
	// Built-in system-prompt templates (GET /agents/templates), for new agents.
	let templates = $state<AgentTemplate[]>([]);
	let loading = $state(true);
	/** undefined while loading; null when this worker has no subagent default (404). */
	let subagentDefault = $state<SubagentDefault | null | undefined>(undefined);
	let defaultError = $state('');

	// Get profiles and groups from stores
	const profiles = $derived($profilesStore);
	const groups = $derived<GroupState>($groupsStore);
	const assignments = $derived(groups?.profiles?.assignments ?? {});
	const folders = $derived((groups?.profiles?.groups ?? []).map((g) => g.name));
	/** The agent API returns `avatar_color` (feat/subagent-default); without it the colour choice hides. */
	const colorSupported = $derived(profiles.some((p) => Object.hasOwn(p, 'avatar_color')));
	const assistantId = $derived($assistantState?.agent_id ?? null);

	// Load data on mount
	$effect(() => {
		loadData();
	});

	async function loadData(quiet = false) {
		if (!quiet) loading = true;
		try {
			const [subagentsResult, surfaceResult, templatesResult] = await Promise.all([
				api.get<RosterAgent[]>('/agents').catch((err) => {
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
				}),
				loadSubagentDefault()
			]);
			allSubagents = subagentsResult;
			toolSurface = surfaceResult;
			templates = templatesResult;
		} catch (e) {
			console.error('Failed to load the Agents page:', e);
		} finally {
			loading = false;
			toolsLoading = false;
		}
	}

	async function loadSubagentDefault() {
		try {
			subagentDefault = await getSubagentDefault();
			defaultError = '';
		} catch (e: any) {
			// Unknown failure: keep the per-provider pickers and say why.
			if (subagentDefault === undefined) subagentDefault = null;
			defaultError = "Couldn't load the subagent default.";
			console.warn('Failed to load subagent default:', e);
		}
	}

	async function saveSubagentDefault(next: { model: string | null }) {
		if (!subagentDefault) return;
		const before = subagentDefault;
		subagentDefault = { ...before, ...next };
		try {
			subagentDefault = await putSubagentDefault(next);
			defaultError = '';
		} catch (e: any) {
			subagentDefault = before;
			notify('Failed to save the subagent default: ' + (e?.detail || e?.message || 'Unknown error'), {
				severity: 'error'
			});
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

	// ── Layout: side by side, or two steps ────────────────────────────────
	let root: HTMLDivElement | undefined = $state();
	let width = $state(0);
	const stacked = $derived(platform.isMobile || (width > 0 && width <= STACK_WIDTH));
	let pane = $state<'list' | 'detail'>('list');
	/** History entries pushed for the detail step (stacked mode). */
	let pushed = 0;

	$effect(() => {
		if (!root) return;
		width = root.getBoundingClientRect().width;
		if (typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver((entries) => {
			width = entries[0]?.contentRect.width ?? width;
		});
		ro.observe(root);
		return () => ro.disconnect();
	});

	function showDetail() {
		if (stacked && pane !== 'detail') {
			try {
				pushState('', { ...(page.state ?? {}), agentsView: 'detail' } as App.PageState);
				pushed += 1;
			} catch {
				// Router not ready (tests): the in-page back still works.
			}
		}
		pane = 'detail';
	}

	function backToList() {
		if (stacked && pushed > 0 && typeof history !== 'undefined') {
			pushed -= 1;
			history.back();
			return;
		}
		pane = 'list';
	}

	// Android Back / browser Back while stacked pops the detail step.
	let firstHistory = true;
	$effect(() => {
		const view = (page.state as { agentsView?: string } | undefined)?.agentsView;
		if (firstHistory) {
			firstHistory = false;
			return;
		}
		untrack(() => {
			if (view !== 'detail' && pane === 'detail') {
				pushed = Math.max(0, pushed - 1);
				pane = 'list';
			}
		});
	});

	// ── List: search and filters ──────────────────────────────────────────
	let query = $state('');
	let filter = $state<AgentFilter>('all');
	let folderFilter = $state('');
	const narrowed = $derived(!!query.trim() || filter !== 'all' || !!folderFilter);

	// View state
	let viewMode = $state<'list' | 'form'>('list');
	let editingProfile = $state<Profile | null>(null);
	let tab = $state<AgentTab>('profile');
	/** Bumps when a different agent (or a new draft) opens; a draft's first save keeps it. */
	let openKey = $state(0);
	/** Id of an agent created from a draft in this visit (Instructions keeps its template row). */
	let draftId = $state<string | null>(null);

	/** The open agent's id; '' while drafting a new one; null when none is open. */
	const selectedId = $derived(viewMode !== 'form' ? null : editingProfile ? editingProfile.id : '');
	const isNew = $derived(!editingProfile || editingProfile.id === draftId);
	let saveState = $state<'idle' | 'saving' | 'saved' | 'error'>('idle');

	const sections = $derived(
		sectionAgents(profiles, {
			query,
			filter,
			folder: folderFilter,
			assignments,
			folders,
			selectedId,
			assistantId
		})
	);

	// Profile form state — fluid harness options only (ruling R3). SDK-era
	// fields (thinking mode, checkpointing, …) are gone: the fluid harness
	// ignores them. max_turns and setting_sources are harness-honored
	// (setting_sources maps onto fluid's user/project/local scope filter).
	function emptyForm(): AgentForm {
		return {
			id: '',
			name: '',
			description: '',
			// With the subagent default a new agent pins nothing; without it, it
			// starts pinned to the default model as before.
			provider_models: subagentDefault ? {} : { [NATIVE_PROVIDER_KEY]: getDefaultModel() },
			max_turns: null,
			allowed_tools: [],
			disallowed_tools: [],
			enabled_agents: [],
			// 'all' = unrestricted; 'custom' = enabled_agents is the exact allowlist.
			agent_invoke_mode: 'all',
			include_partial_messages: true,
			system_prompt_content: '',
			cwd: '',
			add_dirs: '',
			env_vars: [],
			// null = all scopes (default, key omitted); [] = filesystem-isolated.
			setting_sources: null,
			avatar_color: null
		};
	}
	let profileForm = $state<AgentForm>(emptyForm());

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
	let profileImporting = $state(false);
	let showGenerateDialog = $state(false);
	let generateText = $state('');
	let showNewDialog = $state(false);

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
	const totalTools = $derived(selectableSurfaceTools(toolSurface.groups, selectability).length);

	/** Every DB agent but the open one: the subagent roster. */
	const others = $derived(allSubagents.filter((a) => a.id !== editingProfile?.id));

	// ── Avatars ────────────────────────────────────────────────────────────
	function avatarOf(id: string): AvatarId {
		return ($agentIdentities.find((a) => a.id === id)?.avatar_id ?? 'robot') as AvatarId;
	}
	function colorOfProfile(p: Profile): string | null {
		if (p.is_builtin) return null;
		if (viewMode === 'form' && editingProfile?.id === p.id) return profileForm.avatar_color;
		return p.avatar_color ?? null;
	}
	function colorOfId(id: string): string | null {
		const p = profiles.find((x) => x.id === id);
		return p ? colorOfProfile(p) : null;
	}

	// ── Model summaries ────────────────────────────────────────────────────
	function providerNames(models: Record<string, string>): string {
		const names = pinnedProviders(models).map((key) => {
			if (key === NATIVE_PROVIDER_KEY) return 'Claude';
			const provider = getProviders().find((p) => p.id === key);
			return provider ? getProviderDisplay(provider).name : key;
		});
		return names.join(', ');
	}

	function modelFact(models: Record<string, string>): string {
		if (isPinned(models)) return subagentDefault ? `Pinned · ${providerNames(models)}` : providerNames(models);
		if (subagentDefault) {
			return subagentDefault.model ? `Default · ${modelLabel(subagentDefault.model)}` : 'Default · not set';
		}
		return 'Chat only';
	}

	/** Right-hand meta on a list row: "Home" for the assistant, else the model. */
	function metaOf(p: Profile): string {
		if (p.id === assistantId) return 'Home';
		const models =
			viewMode === 'form' && editingProfile?.id === p.id ? profileForm.provider_models : profileModels(p);
		const pinned = pinnedModel(models);
		if (pinned) return modelLabel(pinned);
		return subagentDefault ? 'Default' : 'Chat only';
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

	const facts = $derived.by(() => {
		const n = others.length;
		const reach = invokableCount(editingProfile?.id ?? '', profileForm.agent_invoke_mode, profileForm.enabled_agents);
		return {
			tools:
				toolSelectionMode === 'all'
					? `All ${totalTools}`
					: toolSelectionMode === 'none'
						? 'None'
						: `${effectiveToolCount} of ${totalTools}`,
			subagents: n === 0 ? 'None' : profileForm.agent_invoke_mode !== 'custom' ? `All ${n}` : `${reach} of ${n}`,
			model: modelFact(profileForm.provider_models),
			chats: editingProfile ? (editingProfile.session_count ?? null) : null
		};
	});

	// ── Opening agents ─────────────────────────────────────────────────────
	function resetForm() {
		profileForm = emptyForm();
		toolSelectionMode = 'all';
	}

	function rememberSelection(id: string | null) {
		try {
			if (id) sessionStorage.setItem(SELECTED_KEY, id);
			else sessionStorage.removeItem(SELECTED_KEY);
		} catch {
			// Storage blocked: the page opens on the first agent next time.
		}
	}

	async function openNewForm(nextTab: AgentTab = 'profile') {
		await flushPendingPersist();
		remoteChange = '';
		saveState = 'idle';
		draftId = null;
		resetForm();
		editingProfile = null;
		viewMode = 'form';
		tab = nextTab;
		openKey += 1;
		markFormClean();
		showDetail();
		await tick();
		root?.querySelector<HTMLInputElement>('.st-dpane input.st-txt')?.focus();
	}

	/** Open an agent, saving the open one's pending edits first. */
	async function selectProfile(profile: Profile, nextTab?: AgentTab) {
		const alreadyOpen = viewMode === 'form' && editingProfile?.id === profile.id;
		if (!alreadyOpen) {
			await flushPendingPersist();
			remoteChange = '';
			saveState = 'idle';
			if (draftId !== profile.id) draftId = null;
			openEditForm(profile);
			tab = nextTab ?? 'profile';
			openKey += 1;
			rememberSelection(profile.id);
		} else if (nextTab) {
			tab = nextTab;
		}
		showDetail();
		await tick();
		root?.querySelector('.st-dpane')?.scrollTo?.({ top: 0 });
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
			enabled_agents: ((config as any).enabled_agents || []).filter((id: string) => id !== profile.id),
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
				: null,
			avatar_color: profile.avatar_color ?? null
		};

		viewMode = 'form';
		markFormClean();
	}

	/** Close the open agent (after a delete), saving pending edits first. */
	function clearSelection() {
		viewMode = 'list';
		draftId = null;
		resetForm();
		editingProfile = null;
		rememberSelection(null);
	}

	// Desktop opens on an agent: the deep link, the last one, the assistant, or the first.
	let autoOpened = false;
	$effect(() => {
		if (autoOpened || loading || stacked || viewMode === 'form' || profiles.length === 0) return;
		if (initialSelectedId) return; // the deep link below wins
		autoOpened = true;
		untrack(() => {
			let stored: string | null = null;
			try {
				stored = sessionStorage.getItem(SELECTED_KEY);
			} catch {
				stored = null;
			}
			const first =
				profiles.find((p) => p.id === stored) ??
				profiles.find((p) => p.id === assistantId) ??
				sections.yours[0] ??
				sections.builtin[0] ??
				profiles[0];
			if (first) void selectProfile(first);
		});
	});

	// Consume the deep-link exactly once, after the target profile has loaded.
	let deepLinkConsumed = false;
	$effect(() => {
		if (!initialSelectedId || deepLinkConsumed) return;
		const target = profiles.find((p) => p.id === initialSelectedId);
		if (!target) return; // profiles not loaded yet — wait for the next run
		deepLinkConsumed = true;
		autoOpened = true;
		untrack(() => void selectProfile(target));
		layout.clearFeatureItemId();
	});

	// ── Editing helpers ────────────────────────────────────────────────────
	// Tool helpers. Both write into whichever list the current mode owns; in
	// 'all'/'none' the picker is read-only and never calls them.
	function setActiveToolList(next: string[]) {
		if (toolSelectionMode === 'allow') profileForm.allowed_tools = next;
		else if (toolSelectionMode === 'disallow') profileForm.disallowed_tools = next;
	}

	function toggleTool(group: ToolSurfaceGroup, toolName: string) {
		setActiveToolList(toggleToolSelection(activeToolList, group, toolName, selectability));
	}

	function toggleGroup(group: ToolSurfaceGroup) {
		setActiveToolList(toggleGroupSelection(activeToolList, group, selectability));
	}

	function toggleAgent(agentId: string) {
		if (profileForm.enabled_agents.includes(agentId)) {
			profileForm.enabled_agents = profileForm.enabled_agents.filter((id) => id !== agentId);
		} else {
			profileForm.enabled_agents = [...profileForm.enabled_agents, agentId];
		}
	}

	function handleFormChange(field: keyof AgentForm, value: unknown) {
		(profileForm as any)[field] = value;
	}

	function handleToolSelectionModeChange(mode: ToolSelectionMode) {
		toolSelectionMode = mode;
		if (mode === 'all' || mode === 'none') {
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

	// ── Saving ─────────────────────────────────────────────────────────────
	function buildProfilePayload() {
		// `PUT /agents/{id}` replaces the typed config, so a schema key left out
		// here is genuinely cleared — that is how 'all'/'none' drop a stale list and
		// how hidden legacy bindings (model, provider, permission mode, effort) are
		// retired. The backend keeps keys outside the schema, including the avatar
		// (owned by the identity PATCH), so neither is sent from this snapshot.
		const config: any = {
			include_partial_messages: profileForm.include_partial_messages
		};
		// Subagent runtime map — the ONE model binding an agent still owns, used
		// when another agent dispatches it as a Task subagent. Blank values mean
		// unavailable and are pruned; no entries means the subagent default.
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
		// list at all — the full-replace PUT clears whatever was there.
		config.tool_selection_mode = toolSelectionMode;
		if (toolSelectionMode === 'allow') {
			config.allowed_tools = profileForm.allowed_tools;
		} else if (toolSelectionMode === 'disallow') {
			config.disallowed_tools = profileForm.disallowed_tools;
		}
		// Invoke allowlist. Prune ids of deleted agents, but only when the agent
		// list actually loaded — pruning against an empty/unloaded list would wipe
		// the allowlist.
		config.enabled_agents =
			allSubagents.length > 0
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

		const validEnvVars = profileForm.env_vars.filter((v) => v.key.trim());
		if (validEnvVars.length > 0) {
			config.env = validEnvVars.reduce(
				(acc, { key, value }) => {
					acc[key.trim()] = value;
					return acc;
				},
				{} as Record<string, string>
			);
		}

		// The configured prompt IS the base (ruling A12) — always the custom shape.
		config.system_prompt = {
			type: 'custom',
			content: profileForm.system_prompt_content
		};

		const derivedId = uniqueAgentId(
			profileForm.name,
			profiles.map((p) => p.id)
		);
		const data: { id: string; name: string; description?: string; config: any; avatar_color?: string | null } = {
			id: editingProfile ? editingProfile.id : derivedId,
			name: profileForm.name,
			description: profileForm.description || undefined,
			config
		};
		// The colour rides the agent save only where the API knows the field.
		if (colorSupported) data.avatar_color = profileForm.avatar_color;
		return data;
	}

	async function writeProfile() {
		const writtenSnapshot = formSnapshot();
		const data = buildProfilePayload();
		if (editingProfile) {
			const { id, ...rest } = data;
			await tabsStore.updateProfile(id, rest);
			const fresh = profiles.find((p) => p.id === id);
			if (fresh && editingProfile?.id === id) editingProfile = fresh;
		} else {
			await tabsStore.createProfile(data);
			draftId = data.id;
			editingProfile = profiles.find((p) => p.id === data.id) ?? {
				id: data.id,
				name: data.name,
				description: data.description ?? null,
				is_builtin: false,
				is_modified: false,
				config: data.config,
				created_at: '',
				updated_at: ''
			};
			rememberSelection(data.id);
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
					if (!profileForm.name.trim()) continue;
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

	/** Drop unsaved edits so autosave can't re-apply them (restore, delete, load theirs). */
	async function dropPending() {
		if (saveTimer) clearTimeout(saveTimer);
		saveTimer = null;
		persistDirty = false;
		if (inFlightPromise) await inFlightPromise;
	}

	$effect(() => {
		const snap = formSnapshot();
		if (viewMode !== 'form') return;
		if (snap === lastPersisted) return;
		schedulePersist();
	});

	// "Saved" fades back to quiet after a moment.
	$effect(() => {
		if (saveState !== 'saved') return;
		const t = setTimeout(() => {
			if (saveState === 'saved') saveState = 'idle';
		}, 1600);
		return () => clearTimeout(t);
	});

	// ── Actions ────────────────────────────────────────────────────────────
	async function revertProfile() {
		const profileId = editingProfile?.id;
		if (!profileId) return;
		try {
			await dropPending();
			await tabsStore.restoreProfile(profileId);
			const fresh = profiles.find((p) => p.id === profileId);
			if (fresh && viewMode === 'form' && editingProfile?.id === profileId) openEditForm(fresh);
			notify('Restored defaults.', { severity: 'success' });
		} catch (e: any) {
			notify('Failed to restore defaults: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
		}
	}

	async function deleteOpenAgent() {
		const target = editingProfile;
		// A draft was never saved: discarding it is enough.
		if (!target) {
			await dropPending();
			clearSelection();
			if (stacked) backToList();
			return;
		}
		const order = [...sections.assistant, ...sections.yours, ...sections.builtin];
		const at = order.findIndex((p) => p.id === target.id);
		try {
			await dropPending();
			await tabsStore.deleteProfile(target.id);
			notify(`Deleted ${target.name}.`, { severity: 'success' });
			clearSelection();
			const rest = order.filter((p) => p.id !== target.id);
			const next = rest[Math.min(Math.max(at, 0), rest.length - 1)];
			if (stacked) backToList();
			else if (next) void selectProfile(next);
		} catch (e: any) {
			notify('Failed to delete agent: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
		}
	}

	/** A copy of the open agent under a new name, then open it. */
	async function duplicateAgent() {
		const source = editingProfile;
		if (!source) return;
		await flushPendingPersist();
		const latest = profiles.find((p) => p.id === source.id) ?? source;
		const name = `${latest.name} copy`;
		const id = uniqueAgentId(name, profiles.map((p) => p.id));
		const config = JSON.parse(JSON.stringify(latest.config ?? {}));
		// The avatar belongs to the identity PATCH, not the agent's config.
		delete config.avatar_id;
		const data: { id: string; name: string; description?: string; config: Record<string, unknown>; avatar_color?: string | null } = {
			id,
			name,
			description: latest.description ?? undefined,
			config
		};
		if (colorSupported) data.avatar_color = latest.is_builtin ? null : (latest.avatar_color ?? null);
		try {
			await tabsStore.createProfile(data);
			const shape = avatarOf(latest.id);
			if ($isAdminStore && shape) {
				try {
					const identity = await updateAgentIdentity(id, { avatar_id: shape });
					agentIdentities.update((rows) => [...rows.filter((r) => r.id !== id), identity]);
				} catch {
					// The copy keeps the default avatar; nothing else depends on it.
				}
			}
			void loadData(true);
			const fresh = profiles.find((p) => p.id === id);
			if (fresh) await selectProfile(fresh, 'profile');
			notify(`Duplicated as ${name}.`, { severity: 'success' });
		} catch (e: any) {
			notify('Duplicate failed: ' + (e.detail || e.message || 'Unknown error'), { severity: 'error' });
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

	async function handleImportFile(file: File) {
		profileImporting = true;
		try {
			const parsed = parseAgentExportFile(await file.text());
			const profile = await importAgent(parsed);
			await tabsStore.loadProfiles();
			void loadData(true);
			showNewDialog = false;
			notify(`Imported ${profile.name}.`, { severity: 'success' });
			const fresh = profiles.find((p) => p.id === profile.id);
			if (fresh) await selectProfile(fresh);
		} catch (err: any) {
			notify('Import failed: ' + (err.detail || err.message || 'Invalid file format'), { severity: 'error' });
		} finally {
			profileImporting = false;
		}
	}

	/** New agent dialog: start from a built-in's prompt and tools. */
	async function newFromBuiltin(id: string) {
		const source = profiles.find((p) => p.id === id);
		showNewDialog = false;
		await openNewForm();
		if (!source) return;
		const config = source.config ?? {};
		const sp = (config.system_prompt || {}) as { content?: string; append?: string };
		profileForm.system_prompt_content = sp.content || sp.append || '';
		toolSelectionMode = resolveToolMode(config);
		profileForm.allowed_tools = [...(config.allowed_tools ?? [])];
		profileForm.disallowed_tools = [...(config.disallowed_tools ?? [])];
		profileForm.description = source.description ?? '';
	}

	/** Opens the editor populated with AI-generated agent data, as a draft. */
	function openFormFromGenerated(data: AgentExport) {
		editingProfile = null;
		const agent = data.agent;
		// Export/generated payloads carry the fields flat (no `.config`); a stored
		// mode wins, older ones fall back to T2's legacy reading of the lists.
		toolSelectionMode = resolveToolMode(agent as any);
		// Legacy exports may carry a preset-shaped prompt — its append text
		// becomes the configured prompt (ruling A12).
		const spContent =
			agent.system_prompt_type === 'custom' ? agent.system_prompt_content || '' : agent.system_prompt_append || '';
		const exported = agent.provider_models?.[NATIVE_PROVIDER_KEY] || normalizeModel(agent.model);

		profileForm = {
			...emptyForm(),
			name: agent.name,
			description: agent.description || '',
			// A legacy export's top-level model becomes the native SUBAGENT binding.
			// With the subagent default, an export without one pins nothing.
			provider_models: {
				...(agent.provider_models || {}),
				...(exported
					? { [NATIVE_PROVIDER_KEY]: exported }
					: subagentDefault
						? {}
						: { [NATIVE_PROVIDER_KEY]: getDefaultModel() })
			},
			max_turns: agent.max_turns || null,
			allowed_tools: agent.allowed_tools || [],
			disallowed_tools: agent.disallowed_tools || [],
			enabled_agents: agent.enabled_agents || [],
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
			env_vars: agent.env ? Object.entries(agent.env).map(([key, value]) => ({ key, value: value as string })) : [],
			setting_sources: Array.isArray((agent as any).setting_sources)
				? ((agent as any).setting_sources as string[])
				: null
		};

		viewMode = 'form';
		openKey += 1;
		// A generated agent arrives with its prompt filled; show it.
		tab = 'prompt';
		// Leave it dirty: it has a name, so autosave creates it.
		lastPersisted = '';
	}

	async function handleGeneratedProfile(data: AgentExport | unknown) {
		showGenerateDialog = false;
		await flushPendingPersist();
		remoteChange = '';
		saveState = 'idle';
		draftId = null;
		openFormFromGenerated(data as AgentExport);
		showDetail();
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
		if (folderFilter === name) folderFilter = '';
	}

	// ── Changes from elsewhere ─────────────────────────────────────────────
	let remoteChange = $state('');
	watchResources(['agents', 'templates', 'settings', 'mcp', 'plugins'], async () => {
		const selected = editingProfile;
		const snapshot = formSnapshot();
		await Promise.all([loadData(true), tabsStore.loadProfiles()]);
		if (!selected || editingProfile?.id !== selected.id || viewMode !== 'form') return;
		const fresh = profiles.find((profile) => profile.id === selected.id);
		if (saveTimer || saveInFlight || persistDirty || formSnapshot() !== lastPersisted || snapshot !== formSnapshot()) {
			if (!fresh || JSON.stringify(fresh) !== JSON.stringify(selected)) {
				remoteChange = fresh ? 'Changed elsewhere. Your edits are kept.' : 'Deleted elsewhere. Copy your edits before leaving.';
			}
			return;
		}
		if (fresh) {
			openEditForm(fresh);
			remoteChange = '';
		} else {
			clearSelection();
			remoteChange = '';
			notify('That agent was deleted elsewhere.', { severity: 'info' });
		}
	});

	async function loadTheirs() {
		const fresh = editingProfile ? profiles.find((p) => p.id === editingProfile!.id) : null;
		await dropPending();
		remoteChange = '';
		if (fresh) openEditForm(fresh);
	}

	const builtinChoices = $derived(profiles.filter((p) => p.is_builtin).map((p) => ({ id: p.id, name: p.name })));
	const noneOfYours = $derived(!profiles.some((p) => !p.is_builtin && p.id !== assistantId));
	const folderOptions = $derived([
		{ value: '', label: 'All folders' },
		...folders.map((f) => ({ value: f, label: f })),
		{ value: FOLDER_NONE, label: 'No folder' }
	]);
	const openFolder = $derived(editingProfile ? folderOf(editingProfile.id, assignments, folders) : '');
</script>

<div
	class="settings-shell agents-shell"
	bind:this={root}
	data-stacked={stacked || undefined}
	data-view={stacked ? (pane === 'detail' ? 'detail' : 'page') : undefined}
>
	<CollectionPage
		title="Agents"
		count={loading && profiles.length === 0 ? undefined : profiles.length}
		addLabel="New agent"
		onAdd={() => (showNewDialog = true)}
		filterPlaceholder="Search agents"
		filterIcon="search"
		bind:filter={query}
		onBack={null}
		listLabel="Agent list"
	>
		{#snippet filters()}
			<Segmented
				size="sm"
				label="Show"
				bind:value={filter}
				options={[
					{ value: 'all', label: 'All' },
					{ value: 'yours', label: 'Yours' },
					{ value: 'builtin', label: 'Built-in' }
				]}
			/>
			{#if folders.length > 0}
				<span class="folder-filter">
					<SelectField label="Folder" bind:value={folderFilter} width={170} options={folderOptions} />
				</span>
			{/if}
		{/snippet}
		{#snippet list()}
			<AgentList
				{sections}
				draft={viewMode === 'form' && !editingProfile
					? { name: profileForm.name, color: profileForm.avatar_color }
					: null}
				{selectedId}
				loading={loading}
				{noneOfYours}
				{narrowed}
				{query}
				showYours={filter !== 'builtin'}
				{avatarOf}
				colorOf={colorOfProfile}
				{metaOf}
				onSelect={(p) => selectProfile(p)}
				onSelectDraft={showDetail}
				onNew={() => (showNewDialog = true)}
				onClear={() => {
					query = '';
					filter = 'all';
					folderFilter = '';
				}}
			/>
		{/snippet}
		{#snippet detail()}
			{#if viewMode === 'form'}
				{#key openKey}
					<AgentDetail
						form={profileForm}
						{toolSelectionMode}
						{editingProfile}
						{isNew}
						avatar={editingProfile ? avatarOf(editingProfile.id) : 'robot'}
						isAssistant={!!editingProfile && editingProfile.id === assistantId}
						isAdmin={$isAdminStore}
						{colorSupported}
						folder={openFolder}
						{saveState}
						{remoteChange}
						{tab}
						{facts}
						{groups}
						{templates}
						{toolSurface}
						{toolsLoading}
						{toolsRefreshing}
						{others}
						{subagentDefault}
						{defaultError}
						{identityError}
						{avatarOf}
						colorOf={colorOfId}
						onTab={(t) => (tab = t)}
						onBack={backToList}
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
						onOpenAssistant={(id) => layout.openFeaturePage('assistant', id)}
						onRetryIdentity={loadIdentities}
						onDefaultModel={(model) => saveSubagentDefault({ model })}
						onDuplicate={duplicateAgent}
						onExport={() => editingProfile && handleExportProfile(editingProfile.id)}
						onDelete={deleteOpenAgent}
						onRestore={revertProfile}
						onReloadRemote={loadTheirs}
						onDismissRemote={() => (remoteChange = '')}
					/>
				{/key}
			{:else if loading && profiles.length === 0}
				<div class="sk-detail" aria-hidden="true">
					<div class="sk-dh"><span class="sk sq"></span><span><span class="sk" style:width="180px" style:height="14px"></span><span class="sk" style:width="260px" style:height="10px"></span></span></div>
					<span class="sk" style:height="56px"></span>
					<span class="sk" style:height="180px"></span>
				</div>
			{:else if noneOfYours && !loading}
				<EmptyDetail icon={Bot} title="Make your first agent" message="Built-ins are ready. Yours go here.">
					<button type="button" class="st-btn primary" onclick={() => (showNewDialog = true)}><Plus />New agent</button>
				</EmptyDetail>
			{:else}
				<EmptyDetail icon={Bot} title="No agent selected" message="Pick one from the list." />
			{/if}
		{/snippet}
	</CollectionPage>

	<NewAgentDialog
		open={showNewDialog}
		builtins={builtinChoices}
		busy={profileImporting}
		onClose={() => (showNewDialog = false)}
		onBlank={() => {
			showNewDialog = false;
			void openNewForm();
		}}
		onFromBuiltin={newFromBuiltin}
		onDescribe={(text) => {
			showNewDialog = false;
			generateText = text;
			showGenerateDialog = true;
		}}
		onImport={handleImportFile}
	/>
</div>

<!-- AI Generate Dialog ("Describe it") -->
<AIGenerateDialog
	type="profile"
	isOpen={showGenerateDialog}
	initialDescription={generateText}
	onClose={() => (showGenerateDialog = false)}
	onGenerated={handleGeneratedProfile}
/>

<style>
	/* The Settings tokens and row grammar, without the Settings index column. */
	.settings-shell.agents-shell {
		display: flex;
		flex-direction: column;
		grid-template-columns: none;
		height: 100%;
	}
	.agents-shell :global(.st-coll) {
		flex: 1;
	}
	.folder-filter {
		display: inline-flex;
		flex: 1;
		min-width: 0;
		max-width: 170px;
	}
	.folder-filter :global(select) {
		height: 26px;
		font-size: 12.5px;
	}
	/* Phone: header actions stay on the name line (they fit as icons). */
	.settings-shell.agents-shell[data-stacked] :global(.st-dh) {
		grid-template-columns: auto minmax(0, 1fr) auto;
	}
	.settings-shell.agents-shell[data-stacked] :global(.st-dha) {
		grid-column: auto;
		justify-content: flex-end;
	}
	.settings-shell.agents-shell[data-stacked] :global(.st-lh-top h1) {
		font-size: 22px;
	}

	.sk-detail {
		max-width: 740px;
		padding: 40px 32px;
		display: grid;
		gap: 20px;
	}
	.sk-dh {
		display: flex;
		gap: 14px;
		align-items: center;
	}
	.sk-dh > span:last-child {
		display: grid;
		gap: 8px;
	}
	.sk {
		display: block;
		border-radius: 6px;
		background: linear-gradient(
			90deg,
			var(--st-muted) 0%,
			color-mix(in oklch, var(--st-muted) 55%, var(--st-card)) 50%,
			var(--st-muted) 100%
		);
		background-size: 200% 100%;
		animation: pc-sk 1.3s ease-in-out infinite;
	}
	.sk.sq {
		width: 46px;
		height: 46px;
		border-radius: 13px;
	}
	@keyframes pc-sk {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.sk {
			animation: none;
		}
	}
</style>
