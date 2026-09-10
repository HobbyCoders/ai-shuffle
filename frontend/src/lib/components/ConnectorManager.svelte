<script lang="ts">
	import { watchResources } from '$lib/services/watch-resources';
	/**
	 * ConnectorManager - Manage external connectors (Telegram, Discord, WhatsApp, Webhook, Slack, LINE, Signal, iMessage)
	 *
	 * Views:
	 * - list: Show existing connectors with status, toggle, and actions
	 * - add: Grid of connector type cards to choose from
	 * - setup: Type-specific configuration form for new connector
	 * - edit: Edit an existing connector's configuration
	 */

	import { onMount, onDestroy } from 'svelte';
	import GoogleConnection from '$lib/components/plugins/GoogleConnection.svelte';
	import Banner from '$lib/components/Banner.svelte';
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import type {
		Connector,
		ConnectorTypeInfo,
		ConnectorCreate,
		ConnectorEventLog,
		DiscordDiscoverResult
	} from '$lib/api/connectors';
	import {
		getConnectors,
		getConnectionDependencies,
		type ConnectionDependency,
		createConnector,
		updateConnector,
		deleteConnector,
		testConnector,
		activateConnector,
		deactivateConnector,
		getConnectorTypes,
		getConnectorEvents,
		getConnectorLogs,
		discoverDiscord
	} from '$lib/api/connectors';
	import type { ApiUser } from '$lib/api/apiUsers';
	import { getApiUsers } from '$lib/api/apiUsers';
	import { listInstalls } from '$lib/api/installs';
	import { formatDateTime } from '$lib/utils/dateFormatting';
	import { notify } from '$lib/stores/notifications';

	let { typeFilter = '', onChanged }: { typeFilter?: string; onChanged?: () => void } = $props();
	let showAdvanced = $state(false);

	// State
	let connectors: Connector[] = $state([]);
	let connectorTypes: ConnectorTypeInfo[] = $state([]);
	let availableEvents: string[] = $state([]);
	let apiUsers: ApiUser[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// Multi-install affinity: which install runs each connector. `selfInstallId`
	// is this install's id (from the cluster registry); `installNames` maps an
	// install id to its friendly device name. Best-effort — a connector card
	// only shows a "runs on <install>" badge when its runner_id is known AND
	// differs from this install.
	let selfInstallId = $state<string | null>(null);
	let installNames: Record<string, string> = $state({});

	function runnerBadgeLabel(connector: Connector): string | null {
		const rid = connector.runner_id;
		if (!rid) return null;
		// Only surface the badge for connectors owned by a DIFFERENT install.
		// If we can't resolve our own id, stay silent rather than mislabel a
		// local connector as remote.
		if (!selfInstallId || rid === selfInstallId) return null;
		const name = installNames[rid];
		const short = rid.length > 8 ? rid.slice(0, 8) : rid;
		return `runs on ${name || short}`;
	}

	// UI state
	let view: 'list' | 'add' | 'setup' | 'edit' = $state('list');
	let selectedType = $state<ConnectorTypeInfo | null>(null);
	let editingConnector: Connector | null = $state(null);
	let expandedLogs: Set<string> = $state(new Set());
	let connectorLogs: Map<string, ConnectorEventLog[]> = $state(new Map());
	let loadingLogs: Set<string> = $state(new Set());
	let showVariantsSection = $state(false);

	// Form state
	let formName = $state('');
	let formApiUserId = $state('');
	let formConfig: Record<string, string> = $state({});
	let formEvents: string[] = $state([]);
	let formSaving = $state(false);
	let formError = $state('');
	let testingId: string | null = $state(null);
	let deletingId: string | null = $state(null);
	let dependencies: ConnectionDependency[] = $state([]);
	let checkingDependencies = $state(false);
	let dependencyError = $state('');

	// -----------------------------------------------------------------
	// Discord guided setup state
	// -----------------------------------------------------------------
	let discovering = $state(false);
	let discoverError = $state('');
	let discoverResult = $state<DiscordDiscoverResult | null>(null);
	let manualMode = $state(false); // "Enter IDs manually" fallback (create view)
	let selectedGuildIds: Set<string> = $state(new Set());
	let selectedChannelIds: Set<string> = $state(new Set());
	let prefixMode: 'none' | 'preset' | 'custom' = $state('none');
	let customPrefix = $state('');

	let isDiscord = $derived(selectedType?.type === 'discord');
	// Pickers are active only in guided mode once a discovery has succeeded.
	let showPickers = $derived(isDiscord && !manualMode && discoverResult !== null);
	let prefixPresets = $derived(
		(selectedType?.config_fields.find((f) => f.presets)?.presets as string[] | undefined) ?? []
	);
	let inviteUrl = $derived(
		discoverResult
			? `https://discord.com/oauth2/authorize?client_id=${discoverResult.application_id}&scope=bot&permissions=277025508416`
			: ''
	);

	// Guilds to render: fetched guilds, plus any pre-selected id absent from the
	// fetched list (edit view) surfaced as an "Unknown" row so a save can't drop it.
	let guildRows = $derived.by(() => {
		if (!discoverResult) return [] as { id: string; name: string; error: string | null }[];
		const fetchedIds = new Set(discoverResult.guilds.map((g) => g.id));
		const rows = discoverResult.guilds.map((g) => ({ id: g.id, name: g.name, error: g.error }));
		for (const id of selectedGuildIds) {
			if (!fetchedIds.has(id)) rows.push({ id, name: `Unknown (${id})`, error: null });
		}
		return rows;
	});

	// Channel groups: text channels grouped under guild headings. When one or more
	// guilds are checked, only checked guilds' channels show; otherwise all show.
	// Selected channel ids not present in any shown group are surfaced separately.
	let channelGroups = $derived.by(() => {
		if (!discoverResult)
			return [] as {
				guildId: string;
				guildName: string;
				error: string | null;
				channels: { id: string; name: string }[];
			}[];
		const anyGuildChecked = selectedGuildIds.size > 0;
		const groups: {
			guildId: string;
			guildName: string;
			error: string | null;
			channels: { id: string; name: string }[];
		}[] = [];
		const shown = new Set<string>();
		for (const g of discoverResult.guilds) {
			if (anyGuildChecked && !selectedGuildIds.has(g.id)) continue;
			if (g.channels.length === 0 && !g.error) continue;
			groups.push({ guildId: g.id, guildName: g.name, error: g.error, channels: g.channels });
			for (const c of g.channels) shown.add(c.id);
		}
		const unknown = [...selectedChannelIds].filter((id) => !shown.has(id));
		if (unknown.length) {
			groups.push({
				guildId: '__unknown__',
				guildName: 'Other (stored)',
				error: null,
				channels: unknown.map((id) => ({ id, name: `Unknown (${id})` }))
			});
		}
		return groups;
	});

	// Keep the flat formConfig in sync with the picker selections while pickers are
	// active, so handleCreate/handleUpdate serialize them (comma-joined → lists).
	$effect(() => {
		if (!showPickers) return;
		formConfig.guild_ids = [...selectedGuildIds].join(',');
		formConfig.allowed_channel_ids = [...selectedChannelIds].join(',');
	});

	function parseIds(v: any): string[] {
		if (!v) return [];
		if (Array.isArray(v)) return v.map((x) => String(x)).filter(Boolean);
		return String(v)
			.split(/[\s,]+/)
			.filter(Boolean);
	}

	function resetDiscordState() {
		discovering = false;
		discoverError = '';
		discoverResult = null;
		manualMode = false;
		selectedGuildIds = new Set();
		selectedChannelIds = new Set();
		prefixMode = 'none';
		customPrefix = '';
	}

	function initPrefix(value: string) {
		if (!value) {
			prefixMode = 'none';
			customPrefix = '';
			formConfig.command_prefix = '';
		} else if (prefixPresets.includes(value)) {
			prefixMode = 'preset';
			customPrefix = '';
			formConfig.command_prefix = value;
		} else {
			prefixMode = 'custom';
			customPrefix = value;
			formConfig.command_prefix = value;
		}
	}

	function toggleGuild(id: string) {
		const next = new Set(selectedGuildIds);
		if (next.has(id)) {
			next.delete(id);
			// Uncheck channels of this guild (per spec) — visibility is guild-filtered.
			const guild = discoverResult?.guilds.find((g) => g.id === id);
			if (guild) {
				const chans = new Set(guild.channels.map((c) => c.id));
				selectedChannelIds = new Set([...selectedChannelIds].filter((c) => !chans.has(c)));
			}
		} else {
			next.add(id);
		}
		selectedGuildIds = next;
	}

	function toggleChannel(id: string) {
		const next = new Set(selectedChannelIds);
		if (next.has(id)) next.delete(id);
		else next.add(id);
		selectedChannelIds = next;
	}

	function selectPrefixNone() {
		prefixMode = 'none';
		formConfig.command_prefix = '';
	}
	function selectPrefixPreset(p: string) {
		prefixMode = 'preset';
		formConfig.command_prefix = p;
	}
	function selectPrefixCustom() {
		prefixMode = 'custom';
		formConfig.command_prefix = customPrefix;
	}
	function syncCustomPrefix() {
		if (prefixMode === 'custom') formConfig.command_prefix = customPrefix;
	}

	async function runDiscover() {
		const token = (formConfig.bot_token || '').trim();
		discovering = true;
		discoverError = '';
		try {
			let result: DiscordDiscoverResult;
			if (view === 'edit' && token.startsWith('****') && editingConnector) {
				// Masked token still in the field — reuse the stored one via connector_id.
				result = await discoverDiscord({ connector_id: editingConnector.id });
			} else {
				result = await discoverDiscord({ bot_token: token });
			}
			discoverResult = result;
		} catch (e: any) {
			discoverError = e.detail || 'Invalid bot token';
		} finally {
			discovering = false;
		}
	}

	// Connector type icons (SVG path data)
	const typeIcons: Record<string, string> = {
		telegram: 'M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z',
		discord: 'M20.317 4.37a19.791 19.791 0 00-4.885-1.515.074.074 0 00-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 00-5.487 0 12.64 12.64 0 00-.617-1.25.077.077 0 00-.079-.037A19.736 19.736 0 003.677 4.37a.07.07 0 00-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 00.031.057 19.9 19.9 0 005.993 3.03.078.078 0 00.084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 00-.041-.106 13.107 13.107 0 01-1.872-.892.077.077 0 01-.008-.128 10.2 10.2 0 00.372-.292.074.074 0 01.077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 01.078.01c.12.098.246.198.373.292a.077.077 0 01-.006.127 12.299 12.299 0 01-1.873.892.077.077 0 00-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 00.084.028 19.839 19.839 0 006.002-3.03.077.077 0 00.032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 00-.031-.03z',
		whatsapp: 'M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347z',
		webhook: 'M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4',
		slack: 'M14.5 2A1.5 1.5 0 0013 3.5V10h1.5A1.5 1.5 0 0016 8.5v-5A1.5 1.5 0 0014.5 2zM20 8.5A1.5 1.5 0 0018.5 10H18v1.5a1.5 1.5 0 003 0v-1.5A1.5 1.5 0 0020 8.5zM9.5 22A1.5 1.5 0 0011 20.5V14H9.5A1.5 1.5 0 008 15.5v5A1.5 1.5 0 009.5 22zM4 15.5A1.5 1.5 0 005.5 14H6v-1.5a1.5 1.5 0 00-3 0V14a1.5 1.5 0 001 1.5zM22 14.5a1.5 1.5 0 00-1.5-1.5H14v1.5a1.5 1.5 0 001.5 1.5h5a1.5 1.5 0 001.5-1.5zM15.5 20a1.5 1.5 0 00-1.5-1.5h-1.5a1.5 1.5 0 000 3H14a1.5 1.5 0 001.5-1.5zM2 9.5A1.5 1.5 0 003.5 11H10V9.5A1.5 1.5 0 008.5 8h-5A1.5 1.5 0 002 9.5zM8.5 4A1.5 1.5 0 0010 5.5V6h1.5a1.5 1.5 0 000-3H10A1.5 1.5 0 008.5 4z',
		line: 'M12 2C6.48 2 2 5.81 2 10.4c0 3.73 3.01 6.94 7.31 7.93.28.06.67.19.77.43.09.22.06.56.03.78l-.12.74c-.04.22-.16.86.76.47.92-.39 4.96-2.92 6.77-5 1.25-1.42 1.85-2.86 1.85-4.35C19.37 5.81 16.52 2 12 2z',
		signal: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-1 15v-4H8l5-7v4h3l-5 7z',
		imessage: 'M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z',
		msteams: 'M19.5 5.5h-5v5h5v-5zM12 2v4h-2V2H4v8h6v2H2v8h8v-6h2v6h8V10h-6V8h6V2h-8z',
		bluebubbles: 'M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm-7 9h-2V5h2v6zm0 4h-2v-2h2v2z',
		spotify: 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm4.64 14.36c-.2.31-.62.4-.93.2-2.55-1.56-5.76-1.91-9.54-1.05-.36.08-.73-.14-.81-.51-.08-.36.14-.73.51-.81 4.13-.94 7.68-.54 10.55 1.22.31.2.41.62.22.95zm1.23-2.71c-.25.39-.78.51-1.16.25-2.92-1.79-7.37-2.31-10.82-1.27-.44.13-.9-.11-1.04-.55-.13-.44.11-.9.55-1.04 3.94-1.19 8.84-.61 12.22 1.45.38.25.5.78.25 1.16zm.11-2.82c-3.5-2.08-9.28-2.27-12.62-1.26-.53.16-1.09-.14-1.25-.67-.16-.53.14-1.09.67-1.25 3.84-1.16 10.22-.94 14.26 1.45.48.28.64.9.35 1.38-.28.48-.9.63-1.41.35z',
		hue: 'M9 2a7 7 0 00-4 12.7V17a1 1 0 001 1h6a1 1 0 001-1v-2.3A7 7 0 009 2zm-2 19a1 1 0 001 1h4a1 1 0 001-1v-1H7v1z',
		homeassistant: 'M12 3L2 12h3v8h6v-6h2v6h6v-8h3L12 3z',
		gmail: 'M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z'
	};

	// Load data on mount, then poll for status updates
	let pollInterval: ReturnType<typeof setInterval> | null = null;

	onMount(async () => {
		await loadData();
		pollInterval = setInterval(async () => {
			try {
				const updated = await getConnectors();
				connectors = updated.filter(c => !typeFilter || c.type === typeFilter);
				// Refresh logs for any expanded connector
				for (const cid of expandedLogs) {
					const logs = await getConnectorLogs(cid);
					connectorLogs.set(cid, logs);
					connectorLogs = new Map(connectorLogs);
				}
			} catch {
				// Silently ignore poll failures
			}
		}, 15_000);
	});

	onDestroy(() => {
		if (pollInterval) clearInterval(pollInterval);
	});

	async function loadData(quiet = false) {
		if (!quiet) loading = true;
		error = '';
		try {
			const [connectorsData, typesData, eventsData, apiUsersData] = await Promise.all([
				getConnectors(),
				getConnectorTypes(),
				getConnectorEvents(),
				getApiUsers()
			]);
			connectors = connectorsData.filter(c => !typeFilter || c.type === typeFilter);
			connectorTypes = typesData.filter(t => !typeFilter || t.type === typeFilter);
			availableEvents = eventsData;
			apiUsers = apiUsersData.filter(u => u.is_active);
		} catch (e: any) {
			error = e.detail || 'Failed to load connectors';
		} finally {
			loading = false;
		}

		// Best-effort: resolve this install's id + peer device names so the card
		// can label connectors that run on another install. A failure here must
		// not affect the connectors list.
		try {
			const res = await listInstalls();
			selfInstallId = res.self_instance_id;
			const names: Record<string, string> = {};
			for (const inst of res.installs) names[inst.id] = inst.device_name || inst.id;
			installNames = names;
		} catch {
			// Leave selfInstallId null — badges stay hidden.
		}
	}

	async function handleCreate() {
		if (!selectedType) return;
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}

		// Validate required config fields
		for (const field of selectedType.config_fields) {
			if (field.required && !formConfig[field.key]?.trim()) {
				formError = `${field.label} is required`;
				return;
			}
		}

		formSaving = true;
		formError = '';

		try {
			const data: ConnectorCreate = {
				name: formName,
				type: selectedType.type as ConnectorCreate['type'],
				config: { ...formConfig },
				events: formEvents,
				...(formApiUserId ? { api_user_id: formApiUserId } : {})
			};
			await createConnector(data);
			// Close the form on the create response alone — don't gate the view
			// transition on the follow-up loadData() reload. loadData() bundles
			// four fetches behind one Promise.all (connectors, types, events, api
			// users); if any single one stalls — e.g. the backend is momentarily
			// busy standing up the new connector's live bot connection — the
			// whole bundle never settles, which left the setup view mounted
			// frozen on "Saving..." forever even though the connector was
			// created (a background poll elsewhere would eventually show it in
			// the list, making it look like "the list updates" while this view
			// stayed stuck). Refresh in the background instead.
			cancelForm();
			void loadData();
			onChanged?.();
		} catch (e: any) {
			formError = e.detail || 'Failed to create connector';
		} finally {
			formSaving = false;
		}
	}

	async function handleUpdate() {
		if (!editingConnector) return;
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}

		formSaving = true;
		formError = '';

		try {
			await updateConnector(editingConnector.id, {
				name: formName,
				config: { ...formConfig },
				events: formEvents
			});
			// Same reasoning as handleCreate: close the form on the update
			// response alone so a slow/stalled background reload can't leave
			// this view stuck.
			cancelForm();
			void loadData();
			onChanged?.();
		} catch (e: any) {
			formError = e.detail || 'Failed to update connector';
		} finally {
			formSaving = false;
		}
	}

	async function handleDelete(id: string) {
		deletingId = id;
		dependencies = [];
		dependencyError = '';
		checkingDependencies = true;
		try {
			const result = await getConnectionDependencies(id);
			if (deletingId === id) dependencies = result;
		} catch {
			if (deletingId === id) dependencyError = 'Could not check automation dependencies. Cancel and try again.';
		} finally {
			if (deletingId === id) checkingDependencies = false;
		}
	}

	async function confirmDelete(id: string) {
		if (checkingDependencies || dependencyError || dependencies.length) return;
		try {
			await deleteConnector(id);
			deletingId = null;
			await loadData();
		} catch (e: any) {
			notify(e.detail || 'Failed to delete connector', { severity: 'error' });
			deletingId = null;
		}
	}

	function cancelDelete() {
		deletingId = null;
	}

	async function handleTest(id: string) {
		testingId = id;

		try {
			const result = await testConnector(id);
			if (result.success) {
				notify(
					`Connection successful${result.response_time_ms ? ` (${result.response_time_ms}ms)` : ''}`,
					{ severity: 'success' }
				);
			} else {
				notify(result.error || result.message || 'Test failed', { severity: 'error' });
			}
		} catch (e: any) {
			notify(e.detail || 'Failed to test connector', { severity: 'error' });
		} finally {
			testingId = null;
		}
	}

	async function handleToggle(connector: Connector) {
		try {
			if (connector.is_active) {
				await deactivateConnector(connector.id);
			} else {
				await activateConnector(connector.id);
			}
			await loadData();
		} catch (e: any) {
			notify(e.detail || 'Failed to update connector', { severity: 'error' });
		}
	}

	async function toggleLogs(connectorId: string) {
		if (expandedLogs.has(connectorId)) {
			expandedLogs.delete(connectorId);
			expandedLogs = new Set(expandedLogs);
		} else {
			expandedLogs.add(connectorId);
			expandedLogs = new Set(expandedLogs);

			// Fetch logs if not already loaded
			if (!connectorLogs.has(connectorId)) {
				loadingLogs.add(connectorId);
				loadingLogs = new Set(loadingLogs);
				try {
					const logs = await getConnectorLogs(connectorId);
					connectorLogs.set(connectorId, logs);
					connectorLogs = new Map(connectorLogs);
				} catch (e: any) {
					// silently fail, show empty
					connectorLogs.set(connectorId, []);
					connectorLogs = new Map(connectorLogs);
				} finally {
					loadingLogs.delete(connectorId);
					loadingLogs = new Set(loadingLogs);
				}
			}
		}
	}

	function startAdd() {
		const type = connectorTypes.find(t => t.type === typeFilter);
		if (type) selectType(type);
		else view = 'add';
	}

	function selectType(type: ConnectorTypeInfo) {
		selectedType = type;
		formName = type.name;
		formConfig = {};
		formEvents = [];
		showAdvanced = false;
		formError = '';
		resetDiscordState();
		view = 'setup';
	}

	function startEdit(connector: Connector) {
		editingConnector = connector;
		formName = connector.name;
		// Normalize list-typed config values (e.g. discord guild_ids) to comma-joined
		// strings so plain inputs bind cleanly; picker selections are re-hydrated below.
		const cfg: Record<string, string> = {};
		for (const [k, v] of Object.entries(connector.config)) {
			cfg[k] = Array.isArray(v) ? v.map((x) => String(x)).join(',') : (v as any);
		}
		formConfig = cfg;
		formEvents = [...connector.events];
		formError = '';

		// Find the type info for this connector
		selectedType = connectorTypes.find((t) => t.type === connector.type) || null;

		resetDiscordState();
		if (connector.type === 'discord') {
			selectedGuildIds = new Set(parseIds(connector.config.guild_ids));
			selectedChannelIds = new Set(parseIds(connector.config.allowed_channel_ids));
			initPrefix(String(connector.config.command_prefix ?? ''));
		}
		view = 'edit';
		remoteBaseline = remoteSnapshot();
		remoteChange = '';
	}
	function cancelForm() {
		view = 'list';
		selectedType = null;
		editingConnector = null;
		formName = '';
		formApiUserId = '';
		formConfig = {};
		formEvents = [];
		formError = '';
		resetDiscordState();
	}

	function toggleEvent(event: string) {
		if (formEvents.includes(event)) {
			formEvents = formEvents.filter((e) => e !== event);
		} else {
			formEvents = [...formEvents, event];
		}
	}

	function statusTone(status: Connector['status']): 'ok' | 'warn' | 'danger' | 'off' {
		switch (status) {
			case 'connected': return 'ok';
			case 'connecting': return 'warn';
			case 'error': return 'danger';
			default: return 'off';
		}
	}


	let remoteChange = $state('');
	let remoteBaseline = '';
	function remoteSnapshot() { return JSON.stringify([formName, formApiUserId, formConfig, formEvents, [...selectedGuildIds], [...selectedChannelIds], prefixMode, customPrefix]); }
	watchResources(['connectors', 'api_users'], async () => {
		const selected = editingConnector;
		const snapshot = remoteSnapshot();
		await loadData(true);
		if (!selected || editingConnector !== selected) return;
		const fresh = connectors.find((item) => item.id === selected.id);
		if (formSaving || snapshot !== remoteSnapshot() || remoteSnapshot() !== remoteBaseline) {
			if (!fresh || JSON.stringify(fresh) !== JSON.stringify(selected)) remoteChange = fresh
				? 'This item changed elsewhere. Your unsaved changes are kept here.'
				: 'This item was deleted elsewhere. Copy your unsaved changes before leaving.';
			return;
		}
		if (fresh) startEdit(fresh);
		else { cancelForm(); remoteChange = 'This item was deleted elsewhere.'; }
	});
</script>

{#if remoteChange}<p role="status" style="flex: none; height: auto;" class="px-4 py-2 text-sm text-muted-foreground">{remoteChange}</p>{/if}

<div class="connector-manager">
	{#if loading}
		<div class="text-center py-8 text-muted-foreground text-sm">Loading connectors...</div>
	{:else if error && view === 'list'}
		<Banner severity="error" message={error} dismissible onDismiss={() => (error = '')} />
	{/if}

	<!-- ============================================================ -->
	<!-- LIST VIEW -->
	<!-- ============================================================ -->
	{#if view === 'list' && !loading}
		<div class="space-y-4" transition:fade={{ duration: 150 }}>
			<!-- Header -->
			<div class="flex items-center justify-between">
				<div>
					<h3 class="text-sm font-medium text-foreground">Connections</h3>
				</div>
				<button
					onclick={startAdd}
					class="px-3 py-1.5 bg-primary hover:bg-primary/90 text-primary-foreground text-sm rounded-md transition-colors"
				>
					Connect account
				</button>
			</div>

			<!-- Connector List -->
			{#if connectors.length === 0}
				<div class="text-center py-12 text-muted-foreground">
					<svg
						class="w-12 h-12 mx-auto mb-3 opacity-40"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="1.5"
							d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"
						/>
					</svg>
					<p class="text-sm font-medium mb-1">No accounts connected</p>
					<p class="text-xs">Connect an account or device to use its tools and automation triggers.</p>
				</div>
			{:else}
				<div class="space-y-2">
					{#each connectors as connector (connector.id)}
						<div
							class="connector-item"
							class:connector-item--active={connector.is_active && deletingId !== connector.id}
							class:connector-item--deleting={deletingId === connector.id}
						>
							<div class="p-3">
								<!-- Delete Confirmation -->
								{#if deletingId === connector.id}
									<div
										class="space-y-3"
										transition:slide={{ duration: 200, easing: quintOut }}
									>
										<p class="text-sm text-foreground">
											Delete <strong class="text-destructive">{connector.name}</strong>?
											This cannot be undone.
										</p>
										{#if checkingDependencies}<p class="text-sm" role="status">Checking automations…</p>{/if}
										{#if dependencyError}<p class="text-sm text-destructive" role="alert">{dependencyError}</p>{/if}
										{#if dependencies.length}
											<p class="text-sm">Update these automations before removing this connection:</p>
											<ul class="text-sm list-disc pl-5">{#each dependencies as dep (dep.id)}<li>{dep.name} — {dep.uses.join(', ')}{dep.is_enabled ? '' : ' (paused)'}</li>{/each}</ul>
										{/if}
										<div class="flex items-center gap-2">
											<button
												onclick={() => confirmDelete(connector.id)}
												disabled={checkingDependencies || !!dependencyError || dependencies.length > 0}
												class="px-3 py-1.5 bg-destructive hover:bg-destructive/90 text-destructive-foreground text-sm rounded-md transition-colors"
											>
												Delete
											</button>
											<button
												onclick={cancelDelete}
												class="px-3 py-1.5 bg-secondary hover:bg-secondary/80 text-foreground text-sm rounded-md transition-colors"
											>
												Cancel
											</button>
										</div>
									</div>
								{:else}
									{@const typeInfo = connectorTypes.find(t => t.type === connector.type)}
									<!-- Main Row -->
									<div class="flex items-start justify-between gap-4">
										<div class="connector-ic" aria-hidden="true">
											<svg fill="none" stroke="currentColor" viewBox="0 0 24 24" stroke-width="1.5">
												<path stroke-linecap="round" stroke-linejoin="round" d={typeIcons[connector.type] || typeIcons.webhook} />
											</svg>
										</div>
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<!-- Name -->
												<span class="text-sm font-medium text-foreground truncate">
													{connector.name}
												</span>
												<!-- Type Badge -->
												<span class="sb off">{connector.type}</span>
												<!-- Status Badge -->
												<span class="sb {statusTone(connector.status)}">{connector.status}</span>
												<!-- Runner affinity: only shown when this connector runs on a DIFFERENT install -->
												{#if runnerBadgeLabel(connector)}
													<span
														class="connector-runner-badge"
														title="This connector's live client runs on another install. Changes here apply on that install's next restart."
													>
														{runnerBadgeLabel(connector)}
													</span>
												{/if}
											</div>

											<!-- Event tags -->
											<div class="connector-event-chips">
												{#each connector.events as event}
													<span class="event-chip">{event}</span>
												{/each}
											</div>

											<!-- Meta info -->
											<div
												class="flex items-center gap-2 flex-wrap mt-2 text-xs text-muted-foreground"
											>
												{#if typeInfo?.triggers?.length}
													<span title="Triggers: {typeInfo.triggers.map(t => t.name).join(', ')}">{typeInfo.triggers.length} trigger{typeInfo.triggers.length !== 1 ? 's' : ''}</span>
													<span class="meta-sep">·</span>
												{/if}
												{#if typeInfo?.actions?.length}
													<span title="Actions: {typeInfo.actions.map(a => a.name).join(', ')}">{typeInfo.actions.length} action{typeInfo.actions.length !== 1 ? 's' : ''}</span>
													<span class="meta-sep">·</span>
												{/if}
												{#if typeInfo?.tools?.length}
													<span title="AI Tools: {typeInfo.tools.map(t => t.name).join(', ')}">{typeInfo.tools.length} tool{typeInfo.tools.length !== 1 ? 's' : ''}</span>
													<span class="meta-sep">·</span>
												{/if}
												<span>Last event: {formatDateTime(connector.last_event_at)}</span>
												{#if connector.failure_count > 0}
													<span class="text-warning"
														>Failures: {connector.failure_count}</span
													>
												{/if}
												{#if connector.status_message}
													<span class="text-muted-foreground truncate max-w-[200px]"
														>{connector.status_message}</span
													>
												{/if}
											</div>
										</div>

										<!-- Actions -->
										<div class="flex items-center gap-1.5 shrink-0">
											<!-- Toggle Switch (canonical .switch pattern) -->
											<button
												type="button"
												class="switch"
												class:on={connector.is_active}
												role="switch"
												aria-checked={connector.is_active}
												aria-label={connector.is_active ? 'Deactivate connector' : 'Activate connector'}
												onclick={() => handleToggle(connector)}
												title={connector.is_active ? 'Deactivate' : 'Activate'}
											>
												<i></i>
											</button>

											<!-- Test -->
											<button
												onclick={() => handleTest(connector.id)}
												disabled={testingId === connector.id}
												class="connector-action-btn"
												title="Test connection"
											>
												{#if testingId === connector.id}
													<svg
														class="animate-spin"
														fill="none"
														viewBox="0 0 24 24"
													>
														<circle
															class="opacity-25"
															cx="12"
															cy="12"
															r="10"
															stroke="currentColor"
															stroke-width="4"
														></circle>
														<path
															class="opacity-75"
															fill="currentColor"
															d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"
														></path>
													</svg>
												{:else}
													<svg
														fill="none"
														stroke="currentColor"
														viewBox="0 0 24 24"
													>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															stroke-width="2"
															d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"
														/>
														<path
															stroke-linecap="round"
															stroke-linejoin="round"
															stroke-width="2"
															d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
														/>
													</svg>
												{/if}
											</button>

											<!-- Logs -->
											<button
												onclick={() => toggleLogs(connector.id)}
												class="connector-action-btn"
												class:active={expandedLogs.has(connector.id)}
												title="View event logs"
											>
												<svg
													fill="none"
													stroke="currentColor"
													viewBox="0 0 24 24"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
													/>
												</svg>
											</button>

											<!-- Edit -->
											<button
												onclick={() => startEdit(connector)}
												class="connector-action-btn"
												title="Edit connector"
											>
												<svg
													fill="none"
													stroke="currentColor"
													viewBox="0 0 24 24"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
													/>
												</svg>
											</button>

											<!-- Delete -->
											<button
												onclick={() => handleDelete(connector.id)}
												class="connector-action-btn act-danger"
												title="Delete connector"
											>
												<svg
													fill="none"
													stroke="currentColor"
													viewBox="0 0 24 24"
												>
													<path
														stroke-linecap="round"
														stroke-linejoin="round"
														stroke-width="2"
														d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"
													/>
												</svg>
											</button>
										</div>
									</div>

									<!-- Expanded Event Logs -->
									{#if expandedLogs.has(connector.id)}
										<div
											class="mt-3 pt-3 border-t border-border"
											transition:slide={{ duration: 200, easing: quintOut }}
										>
											<h4 class="text-xs font-medium text-muted-foreground mb-2">
												Recent Events
											</h4>
											{#if loadingLogs.has(connector.id)}
												<div class="text-xs text-muted-foreground py-2">
													Loading logs...
												</div>
											{:else if (connectorLogs.get(connector.id) || []).length === 0}
												<div class="text-xs text-muted-foreground py-2">
													No events recorded yet.
												</div>
											{:else}
												<div class="log-table">
													<div class="log-header">
														<span class="log-col-time">Time</span>
														<span class="log-col-event">Event</span>
														<span class="log-col-dir">Direction</span>
														<span class="log-col-status">Status</span>
													</div>
													{#each connectorLogs.get(connector.id) || [] as log (log.id)}
														<div class="log-row">
															<span class="log-col-time"
																>{formatDateTime(log.created_at)}</span
															>
															<span class="log-col-event">{log.event_type}</span>
															<span class="log-col-dir">
																<span
																	class="px-1.5 py-0.5 rounded-sm text-xs {log.direction === 'outbound' ? 'log-dir-out' : 'log-dir-in'}"
																>
																	{log.direction}
																</span>
															</span>
															<span class="log-col-status">
																{#if log.status === 'success'}
																	<span class="text-success text-xs"
																		>Success</span
																	>
																{:else if log.error}
																	<span
																		class="text-destructive text-xs"
																		title={log.error}>{log.status}</span
																	>
																{:else}
																	<span class="text-muted-foreground text-xs"
																		>{log.status}</span
																	>
																{/if}
															</span>
														</div>
													{/each}
												</div>
											{/if}
										</div>
									{/if}
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</div>
	{/if}

	<!-- ============================================================ -->
	<!-- ADD VIEW - Connector Type Selection Grid -->
	<!-- ============================================================ -->
	{#if view === 'add'}
		<div class="space-y-4" transition:fade={{ duration: 150 }}>
			<!-- Header with Back -->
			<div class="flex items-center gap-3">
				<button
					onclick={cancelForm}
					class="p-1.5 text-muted-foreground hover:text-foreground rounded-sm transition-colors hover:bg-secondary/50"
					title="Back to list"
				>
					<svg
						class="w-4 h-4"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M15 19l-7-7 7-7"
						/>
					</svg>
				</button>
				<div>
					<h3 class="text-sm font-medium text-foreground">Add Connector</h3>
					<p class="text-xs text-muted-foreground mt-0.5">
						Choose a connector type to set up
					</p>
				</div>
			</div>

			<!-- Type Cards Grid -->
			<div class="connector-type-grid">
				{#each connectorTypes as type (type.type)}
					<button
						class="connector-type-card"
						onclick={() => selectType(type)}
					>
						<div class="connector-type-icon">
							<svg
								class="w-6 h-6"
								fill="none"
								stroke="currentColor"
								viewBox="0 0 24 24"
								stroke-width="1.5"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									d={typeIcons[type.type] || typeIcons.webhook}
								/>
							</svg>
						</div>
						<div class="connector-type-info">
							<span class="connector-type-name">{type.name}</span>
							<span class="connector-type-desc">{type.description}</span>
							{#if type.triggers?.length || type.actions?.length || type.tools?.length}
								<div class="connector-type-variants">
									{#if type.triggers?.length}<span class="mini-badge">&#9889;{type.triggers.length}</span>{/if}
									{#if type.actions?.length}<span class="mini-badge">&#9654;{type.actions.length}</span>{/if}
									{#if type.tools?.length}<span class="mini-badge">&#128295;{type.tools.length}</span>{/if}
								</div>
							{/if}
						</div>
						<div class="connector-type-action">
							<span class="text-xs text-primary font-medium">Set Up</span>
							<svg
								class="w-3.5 h-3.5 text-primary"
								fill="none"
								stroke="currentColor"
								viewBox="0 0 24 24"
							>
								<path
									stroke-linecap="round"
									stroke-linejoin="round"
									stroke-width="2"
									d="M9 5l7 7-7 7"
								/>
							</svg>
						</div>
					</button>
				{/each}
			</div>
		</div>
	{/if}

	<!-- ============================================================ -->
	<!-- SETUP / EDIT VIEW - Configuration Form -->
	<!-- ============================================================ -->
	{#if view === 'setup' || view === 'edit'}
		<div class="space-y-4" transition:fade={{ duration: 150 }}>
			<!-- Header with Back -->
			<div class="flex items-center gap-3">
				<button
					onclick={cancelForm}
					class="p-1.5 text-muted-foreground hover:text-foreground rounded-sm transition-colors hover:bg-secondary/50"
					title="Back"
				>
					<svg
						class="w-4 h-4"
						fill="none"
						stroke="currentColor"
						viewBox="0 0 24 24"
					>
						<path
							stroke-linecap="round"
							stroke-linejoin="round"
							stroke-width="2"
							d="M15 19l-7-7 7-7"
						/>
					</svg>
				</button>
				<div>
					<h3 class="text-sm font-medium text-foreground">
						{view === 'edit' ? 'Edit Connector' : `Set Up ${selectedType?.name || 'Connector'}`}
					</h3>
					<p class="text-xs text-muted-foreground mt-0.5">
						{view === 'edit'
							? `Editing ${editingConnector?.name}`
							: selectedType?.description || 'Configure the connector'}
					</p>
				</div>
			</div>

			<!-- Form -->
			<div class="bg-muted/50 rounded-lg p-4 border border-border">
				{#if formError}
					<div transition:slide={{ duration: 200, easing: quintOut }}>
						<Banner severity="error" message={formError} />
					</div>
				{/if}

				<div class="space-y-4">
					<!-- Name -->
					<div>
						<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-name">
							Connector Name <span class="text-destructive">*</span>
						</label>
						<input
							id="connector-name"
							type="text"
							bind:value={formName}
							placeholder="e.g., Work account"
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
						/>
					</div>

					<button class="text-sm text-primary" aria-expanded={showAdvanced} onclick={() => showAdvanced = !showAdvanced}>
						{showAdvanced ? 'Hide advanced settings' : 'Advanced settings'}
					</button>
					<!-- API User -->
					{#if view === 'setup' && showAdvanced}
						<div>
							<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-api-user">
								API User
							</label>
							<select
								id="connector-api-user"
								bind:value={formApiUserId}
								class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground focus:outline-hidden focus:border-primary transition-colors"
							>
								<option value="">Auto-create dedicated user</option>
								{#each apiUsers as user}
									<option value={user.id}>
										{user.name}{user.description ? ` — ${user.description}` : ''}
									</option>
								{/each}
							</select>
							<p class="text-xs text-muted-foreground mt-1">
								Select an existing API user or leave empty to auto-create one.
								Manage API users in Settings &rarr; System.
							</p>
						</div>
					{/if}

					{#if selectedType?.type === 'gmail'}
						<GoogleConnection name={formName} config={formConfig} connectorId={editingConnector?.id} onConnected={() => { cancelForm(); void loadData(); onChanged?.(); }} />
					{/if}

					<!-- Dynamic Config Fields -->
					{#if selectedType}
						{#each selectedType.config_fields.filter(f => showAdvanced || (selectedType?.type !== 'gmail' && !['poll_interval_seconds', 'max_results'].includes(f.key))) as field (field.key)}
							{#if isDiscord && field.key === 'bot_token'}
								<!-- Discord: bot token + Validate (guided discovery) -->
								<div>
									<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-field-{field.key}">
										{field.label}
										{#if field.required}<span class="text-destructive">*</span>{/if}
									</label>
									<div class="flex gap-2">
										<input
											id="connector-field-{field.key}"
											type="password"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || ''}
											class="flex-1 min-w-0 px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors font-mono"
										/>
										<button
											type="button"
											onclick={runDiscover}
											disabled={discovering || !(formConfig[field.key] || '').trim()}
											class="px-3 py-2 bg-primary hover:bg-primary/90 disabled:bg-primary/50 text-primary-foreground text-sm rounded-md transition-colors font-medium whitespace-nowrap"
										>
											{discovering ? 'Validating…' : discoverResult ? 'Re-validate' : 'Validate'}
										</button>
									</div>
									{#if field.description}
										<p class="text-xs text-muted-foreground mt-1">{field.description}</p>
									{/if}
									{#if discoverResult}
										<p class="discord-auth-ok" transition:slide={{ duration: 150 }}>
											&#10003; Authenticated as <strong>{discoverResult.bot.name}</strong>
											· {discoverResult.guilds.length} server{discoverResult.guilds.length === 1 ? '' : 's'} visible
										</p>
									{/if}
									{#if discoverError}
										<p class="discord-auth-err" transition:slide={{ duration: 150 }}>{discoverError}</p>
									{/if}
									{#if view === 'setup'}
										<button
											type="button"
											class="manual-toggle"
											onclick={() => (manualMode = !manualMode)}
										>
											{manualMode ? 'Use guided setup' : 'Enter IDs manually'}
										</button>
									{/if}
								</div>
							{:else if isDiscord && field.picker === 'discord_guilds'}
								{#if showPickers}
									<div>
										<span class="block text-xs font-medium text-muted-foreground mb-1">{field.label}</span>
										{#if guildRows.length === 0}
											<div class="invite-box">
												<p class="text-xs text-muted-foreground mb-2">
													This bot isn't in any servers yet. Invite it, then re-validate.
												</p>
												<a class="invite-link" href={inviteUrl} target="_blank" rel="noopener noreferrer">
													Open Discord invite &rarr;
												</a>
											</div>
										{:else}
											<div class="picker-list">
												{#each guildRows as g (g.id)}
													<label class="picker-item">
														<input
															type="checkbox"
															checked={selectedGuildIds.has(g.id)}
															onchange={() => toggleGuild(g.id)}
															class="w-4 h-4 rounded-sm border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
														/>
														<span class="picker-item-name">{g.name}</span>
														{#if g.error}
															<span class="picker-item-warn" title={g.error}>limited access</span>
														{/if}
													</label>
												{/each}
											</div>
											<p class="text-xs text-muted-foreground mt-1">
												Leave all unchecked to allow every visible server.
											</p>
										{/if}
									</div>
								{:else if manualMode || view === 'edit'}
									<div>
										<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-field-{field.key}">{field.label}</label>
										<input
											id="connector-field-{field.key}"
											type="text"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || 'Comma-separated guild IDs'}
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
										/>
										{#if field.description}<p class="text-xs text-muted-foreground mt-1">{field.description}</p>{/if}
									</div>
								{/if}
							{:else if isDiscord && field.picker === 'discord_channels'}
								{#if showPickers}
									<div>
										<span class="block text-xs font-medium text-muted-foreground mb-1">{field.label}</span>
										{#if channelGroups.length === 0}
											<p class="text-xs text-muted-foreground">
												No text channels found for the selected server(s).
											</p>
										{:else}
											<div class="picker-list">
												{#each channelGroups as grp (grp.guildId)}
													<div class="picker-group-label">{grp.guildName}</div>
													{#if grp.error}
														<p class="picker-group-err">{grp.error}</p>
													{/if}
													{#each grp.channels as c (c.id)}
														<label class="picker-item picker-item-indent">
															<input
																type="checkbox"
																checked={selectedChannelIds.has(c.id)}
																onchange={() => toggleChannel(c.id)}
																class="w-4 h-4 rounded-sm border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
															/>
															<span class="picker-item-name">#{c.name}</span>
														</label>
													{/each}
												{/each}
											</div>
											<p class="text-xs text-muted-foreground mt-1">
												Leave all unchecked to allow every channel.
											</p>
										{/if}
									</div>
								{:else if manualMode || view === 'edit'}
									<div>
										<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-field-{field.key}">{field.label}</label>
										<input
											id="connector-field-{field.key}"
											type="text"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || 'Comma-separated channel IDs'}
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
										/>
										{#if field.description}<p class="text-xs text-muted-foreground mt-1">{field.description}</p>{/if}
									</div>
								{/if}
							{:else if isDiscord && field.presets}
								{#if manualMode}
									<div>
										<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-field-{field.key}">{field.label}</label>
										<input
											id="connector-field-{field.key}"
											type="text"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || ''}
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
										/>
										{#if field.description}<p class="text-xs text-muted-foreground mt-1">{field.description}</p>{/if}
									</div>
								{:else if showPickers || view === 'edit'}
									<div>
										<span class="block text-xs font-medium text-muted-foreground mb-1">{field.label}</span>
										<div class="prefix-chips">
											<button type="button" class="prefix-chip" class:active={prefixMode === 'none'} onclick={selectPrefixNone}>None</button>
											{#each prefixPresets as p}
												<button type="button" class="prefix-chip" class:active={prefixMode === 'preset' && formConfig.command_prefix === p} onclick={() => selectPrefixPreset(p)}>{p}</button>
											{/each}
											<button type="button" class="prefix-chip" class:active={prefixMode === 'custom'} onclick={selectPrefixCustom}>Custom</button>
										</div>
										{#if prefixMode === 'custom'}
											<input
												type="text"
												bind:value={customPrefix}
												oninput={syncCustomPrefix}
												placeholder="e.g., !!"
												class="mt-2 w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors font-mono"
											/>
										{/if}
										{#if field.description}<p class="text-xs text-muted-foreground mt-1">{field.description}</p>{/if}
									</div>
								{/if}
							{:else}
								<div>
									<label class="block text-xs font-medium text-muted-foreground mb-1" for="connector-field-{field.key}">
										{field.label}
										{#if field.required}
											<span class="text-destructive">*</span>
										{/if}
									</label>
									{#if field.type === 'textarea'}
										<textarea
											id="connector-field-{field.key}"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || ''}
											rows="3"
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors resize-vertical"
										></textarea>
									{:else if field.type === 'password'}
										<input
											id="connector-field-{field.key}"
											type="password"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || ''}
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors font-mono"
										/>
									{:else}
										<input
											id="connector-field-{field.key}"
											type="text"
											bind:value={formConfig[field.key]}
											placeholder={field.placeholder || ''}
											class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-hidden focus:border-primary transition-colors"
										/>
									{/if}
									{#if field.description}
										<p class="text-xs text-muted-foreground mt-1">{field.description}</p>
									{/if}
								</div>
							{/if}
						{/each}
					{/if}

					<!-- Session lifecycle notifications are separate from automation triggers. -->
					{#if showAdvanced}
					<div>
						<span class="block text-xs font-medium text-muted-foreground mb-2">
							Session notifications
						</span>
						<p class="text-xs text-muted-foreground mb-2">
							Optional session notifications. Automation triggers work independently of these selections.
						</p>
						<div class="space-y-2">
							{#each availableEvents as event}
								<label class="flex items-center gap-2 cursor-pointer group">
									<input
										type="checkbox"
										checked={formEvents.includes(event)}
										onchange={() => toggleEvent(event)}
										class="w-4 h-4 rounded-sm border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
									/>
									<span
										class="text-sm text-foreground group-hover:text-primary transition-colors"
										>{event}</span
									>
								</label>
							{/each}
						</div>
					</div>

					{/if}
					<!-- Connector Capabilities (Triggers, Actions, Tools) -->
					{#if selectedType && (selectedType.triggers?.length || selectedType.actions?.length || selectedType.tools?.length)}
						<div class="capabilities-section">
							<button class="capabilities-toggle" onclick={() => showVariantsSection = !showVariantsSection}>
								{#if showVariantsSection}
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" /></svg>
								{:else}
									<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" /></svg>
								{/if}
								Capabilities ({(selectedType.triggers?.length || 0) + (selectedType.actions?.length || 0) + (selectedType.tools?.length || 0)})
							</button>

							{#if showVariantsSection}
								<div transition:slide={{ duration: 150 }} style="margin-top: 8px;">
									{#if selectedType.triggers?.length}
										<div class="variant-section">
											<span class="variant-section-title">&#9889; Triggers</span>
											{#each selectedType.triggers as trigger}
												<div class="variant-item">
													<span class="variant-item-name">{trigger.name}</span>
													<span class="variant-item-desc">{trigger.description}</span>
												</div>
											{/each}
										</div>
									{/if}
									{#if selectedType.actions?.length}
										<div class="variant-section">
											<span class="variant-section-title">&#9654; Actions</span>
											{#each selectedType.actions as action}
												<div class="variant-item">
													<span class="variant-item-name">{action.name}</span>
													<span class="variant-item-desc">{action.description}</span>
												</div>
											{/each}
										</div>
									{/if}
									{#if selectedType.tools?.length}
										<div class="variant-section">
											<span class="variant-section-title">&#128295; AI Tools</span>
											{#each selectedType.tools as tool}
												<div class="variant-item">
													<span class="variant-item-name">{tool.name}</span>
													<span class="variant-item-desc">{tool.description}</span>
												</div>
											{/each}
										</div>
									{/if}
								</div>
							{/if}
						</div>
					{/if}

					<!-- Actions -->
					<div class="flex items-center gap-2 pt-2">
						{#if selectedType?.type !== 'gmail' || view === 'edit' || showAdvanced}
						<button
							onclick={view === 'edit' ? handleUpdate : handleCreate}
							disabled={formSaving || (view === 'setup' && isDiscord && !manualMode && !discoverResult)}
							class="px-4 py-2 bg-primary hover:bg-primary/90 disabled:bg-primary/50 text-primary-foreground text-sm rounded-md transition-colors font-medium"
						>
							{formSaving
								? 'Saving...'
								: view === 'edit'
									? 'Update Connector'
									: 'Create Connector'}
						</button>
						{/if}
						<button
							onclick={cancelForm}
							class="px-4 py-2 bg-secondary hover:bg-secondary/80 text-foreground text-sm rounded-md transition-colors"
						>
							Cancel
						</button>
					</div>
				</div>
			</div>
		</div>
	{/if}
</div>

<style>
	.switch { width: 32px; height: 18px; border-radius: 999px; background: var(--muted-foreground); border: 1px solid var(--border); padding: 2px; flex-shrink: 0; cursor: pointer; }
	.switch i { display: block; width: 12px; height: 12px; border-radius: 50%; background: var(--background); transition: transform .15s; }
	.switch[aria-checked='true'] { background: var(--primary); }
	.switch[aria-checked='true'] i { transform: translateX(12px); }
	.switch:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	.switch:disabled { opacity: .5; cursor: not-allowed; }
	/* .connector-manager inherits from parent layout */

	/* ========================================
	   Connector Type Selection Grid
	   ======================================== */
	.connector-type-grid {
		display: grid;
		grid-template-columns: repeat(2, 1fr);
		gap: 12px;
	}

	@media (max-width: 639px) {
		.connector-type-grid {
			grid-template-columns: 1fr;
		}
	}

	.connector-type-card {
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 12px;
		padding: 16px;
		background: color-mix(in oklch, var(--muted) 60%, transparent);
		border: 1px solid var(--border);
		border-radius: var(--radius);
		cursor: pointer;
		transition: all 200ms cubic-bezier(0.16, 1, 0.3, 1);
		text-align: left;
		position: relative;
		overflow: hidden;
	}

	.connector-type-card::before {
		content: '';
		position: absolute;
		inset: 0;
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--primary) 4%, transparent) 0%,
			transparent 50%
		);
		opacity: 0;
		transition: opacity 200ms;
		pointer-events: none;
	}

	.connector-type-card:hover {
		border-color: var(--primary);
		box-shadow:
			0 4px 6px -1px rgba(0, 0, 0, 0.15),
			0 0 12px color-mix(in oklch, var(--primary) 20%, transparent);
		transform: translateY(-2px);
	}

	.connector-type-card:hover::before {
		opacity: 1;
	}

	.connector-type-card:active {
		transform: translateY(0);
	}

	.connector-type-card:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.connector-type-icon {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 44px;
		height: 44px;
		border-radius: 9999px;
		background: linear-gradient(
			135deg,
			color-mix(in oklch, var(--primary) 18%, transparent) 0%,
			color-mix(in oklch, var(--primary) 8%, transparent) 100%
		);
		border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent);
		color: var(--primary);
		transition: all 200ms;
	}

	.connector-type-card:hover .connector-type-icon {
		box-shadow: 0 0 12px color-mix(in oklch, var(--primary) 30%, transparent);
		transform: scale(1.05);
	}

	.connector-type-info {
		display: flex;
		flex-direction: column;
		gap: 4px;
		position: relative;
		z-index: 1;
	}

	.connector-type-name {
		font-size: 0.875rem;
		font-weight: 600;
		color: var(--foreground);
	}

	.connector-type-desc {
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
		display: -webkit-box;
		-webkit-line-clamp: 2;
		line-clamp: 2;
		-webkit-box-orient: vertical;
		overflow: hidden;
	}

	.connector-type-action {
		display: flex;
		align-items: center;
		gap: 4px;
		margin-top: auto;
		position: relative;
		z-index: 1;
	}

	/* ========================================
	   Connector List Items
	   ======================================== */
	/* Card matches the providers row system (.acct-row): muted surface,
	   tokenized border/radius, green highlight when enabled. */
	.connector-item {
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: 10px;
		transition: border-color 150ms ease, background 150ms ease;
	}

	.connector-item:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, var(--border));
	}

	.connector-item--active {
		border-color: color-mix(in oklch, var(--success) 45%, var(--border));
		background: color-mix(in oklch, var(--success) 7%, var(--muted));
	}

	.connector-item--deleting {
		background: color-mix(in oklch, var(--destructive) 8%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	/* Muted "runs on <install>" badge — surfaced only for connectors whose live
	   client is owned by another install (multi-install affinity). */
	.connector-runner-badge {
		display: inline-flex;
		align-items: center;
		padding: 1px 6px;
		font-size: 0.6875rem;
		line-height: 1.4;
		color: var(--muted-foreground);
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 9999px;
		white-space: nowrap;
	}

	/* Leading type-icon tile — matches the providers row (.acct-ic). */
	.connector-ic {
		flex-shrink: 0;
		width: 32px;
		height: 32px;
		border-radius: 8px;
		display: grid;
		place-items: center;
		background: var(--background);
		color: var(--foreground);
	}
	.connector-ic svg {
		width: 17px;
		height: 17px;
	}

	/* Canonical status/type pill — copied from the providers row (.sb). */
	.sb {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
		line-height: 1.5;
	}
	.sb.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.sb.warn {
		color: var(--warning, #d97706);
		background: color-mix(in oklch, var(--warning, #d97706) 16%, transparent);
	}
	.sb.danger {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 14%, transparent);
	}
	.sb.off {
		color: var(--muted-foreground);
		background: var(--background);
	}

	.meta-sep {
		opacity: 0.45;
		user-select: none;
	}

	.connector-action-btn {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s, background 0.15s;
	}
	.connector-action-btn svg {
		width: 15px;
		height: 15px;
	}
	.connector-action-btn:hover:not(:disabled) {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.connector-action-btn.act-danger:hover:not(:disabled) {
		color: var(--destructive);
		border-color: var(--destructive);
	}
	.connector-action-btn.active {
		color: var(--primary);
		border-color: color-mix(in oklch, var(--primary) 45%, var(--border));
	}
	.connector-action-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.connector-action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 1px;
	}

	/* The enable/disable toggle reuses the app's canonical `.switch`
	   pattern (button.switch > <i>), themed globally by StudioEditorShell's
	   `.ed-content :global(.switch)` rules — no bespoke toggle CSS here. */

	/* ========================================
	   Event Log Table
	   ======================================== */
	.log-table {
		border: 1px solid var(--border);
		border-radius: calc(var(--radius) - 2px);
		overflow: hidden;
		font-size: 0.75rem;
	}

	.log-header {
		display: grid;
		grid-template-columns: 140px 1fr 80px 80px;
		gap: 8px;
		padding: 6px 10px;
		background: var(--muted);
		border-bottom: 1px solid var(--border);
		font-weight: 600;
		color: var(--muted-foreground);
		text-transform: uppercase;
		letter-spacing: 0.04em;
		font-size: 0.625rem;
	}

	.log-row {
		display: grid;
		grid-template-columns: 140px 1fr 80px 80px;
		gap: 8px;
		padding: 6px 10px;
		border-bottom: 1px solid color-mix(in oklch, var(--border) 50%, transparent);
		transition: background 100ms;
	}

	.log-row:last-child {
		border-bottom: none;
	}

	.log-row:hover {
		background: var(--hover-overlay);
	}

	.log-col-time {
		color: var(--muted-foreground);
		font-size: 0.6875rem;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.log-col-event {
		color: var(--foreground);
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}

	.log-col-dir {
		display: flex;
		align-items: center;
	}

	.log-col-status {
		display: flex;
		align-items: center;
	}

	/* Mobile: stack log columns */
	@media (max-width: 639px) {
		.log-header {
			display: none;
		}

		.log-row {
			display: flex;
			flex-wrap: wrap;
			gap: 4px 12px;
			padding: 8px 10px;
		}

		.log-col-time {
			flex: 0 0 100%;
			font-size: 0.625rem;
		}

		.log-col-event {
			flex: 1 1 auto;
		}
	}

	.log-dir-out {
		background: color-mix(in oklch, var(--primary) 20%, transparent);
		color: var(--primary);
	}

	.log-dir-in {
		background: var(--muted);
		color: var(--muted-foreground);
	}

	/* ========================================
	   Variant Badges (Triggers, Actions, Tools)
	   ======================================== */
	/* Event names as small themed chips (was a bare Tailwind text row). */
	.connector-event-chips {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-top: 6px;
	}

	.event-chip {
		display: inline-flex;
		align-items: center;
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 500;
		letter-spacing: 0.02em;
		line-height: 1.5;
		padding: 1px 7px;
		border-radius: 999px;
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--foreground) 5%, transparent);
	}

	/* ========================================
	   Capabilities Section (Edit/Setup View)
	   ======================================== */
	.capabilities-section {
		border-top: 1px solid var(--border);
		padding-top: 12px;
	}

	.capabilities-toggle {
		display: flex;
		align-items: center;
		gap: 6px;
		padding: 6px 0;
		background: none;
		border: none;
		color: var(--muted-foreground);
		font-size: 0.8125rem;
		font-weight: 600;
		cursor: pointer;
		transition: color 150ms ease;
	}

	.capabilities-toggle:hover {
		color: var(--foreground);
	}

	.variant-section {
		margin-bottom: 12px;
	}

	.variant-section:last-child {
		margin-bottom: 0;
	}

	.variant-section-title {
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.02em;
		opacity: 0.8;
		display: block;
		margin-bottom: 6px;
	}

	.variant-item {
		padding: 6px 10px;
		background: color-mix(in oklch, var(--foreground) 3%, transparent);
		border-radius: 6px;
		margin-bottom: 4px;
	}

	.variant-item:last-child {
		margin-bottom: 0;
	}

	.variant-item-name {
		font-size: 12px;
		font-weight: 500;
		display: block;
		color: var(--foreground);
	}

	.variant-item-desc {
		font-size: 11px;
		opacity: 0.6;
		display: block;
		margin-top: 2px;
	}

	/* ========================================
	   Mini Badges (Type Selection Grid)
	   ======================================== */
	.connector-type-variants {
		display: flex;
		gap: 4px;
		margin-top: 4px;
	}

	.mini-badge {
		font-size: 10px;
		padding: 1px 4px;
		background: color-mix(in oklch, var(--foreground) 6%, transparent);
		border-radius: 4px;
		opacity: 0.7;
	}

	/* ========================================
	   Discord guided setup
	   ======================================== */
	.discord-auth-ok {
		font-size: 12px;
		margin-top: 6px;
		color: var(--success, oklch(0.7 0.15 150));
	}

	.discord-auth-err {
		font-size: 12px;
		margin-top: 6px;
		color: var(--destructive);
	}

	.manual-toggle {
		margin-top: 8px;
		font-size: 12px;
		color: var(--primary);
		background: none;
		border: none;
		padding: 0;
		cursor: pointer;
		text-decoration: underline;
	}

	.picker-list {
		max-height: 220px;
		overflow-y: auto;
		border: 1px solid var(--border);
		border-radius: var(--radius);
		padding: 8px;
		background: var(--input);
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.picker-item {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 4px 6px;
		border-radius: 6px;
		cursor: pointer;
	}

	.picker-item:hover {
		background: color-mix(in oklch, var(--foreground) 5%, transparent);
	}

	.picker-item-indent {
		padding-left: 18px;
	}

	.picker-item-name {
		font-size: 13px;
		color: var(--foreground);
	}

	.picker-item-warn {
		font-size: 10px;
		padding: 1px 5px;
		border-radius: 4px;
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
	}

	.picker-group-label {
		font-size: 11px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
		margin-top: 6px;
		padding: 0 6px;
	}

	.picker-group-err {
		font-size: 11px;
		color: var(--destructive);
		padding: 0 6px;
	}

	.invite-box {
		border: 1px dashed var(--border);
		border-radius: var(--radius);
		padding: 12px;
		background: color-mix(in oklch, var(--muted) 40%, transparent);
	}

	.invite-link {
		font-size: 13px;
		color: var(--primary);
		font-weight: 500;
		text-decoration: none;
	}

	.invite-link:hover {
		text-decoration: underline;
	}

	.prefix-chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}

	.prefix-chip {
		min-width: 32px;
		padding: 4px 10px;
		font-size: 13px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--input);
		color: var(--foreground);
		cursor: pointer;
		transition: all 150ms;
	}

	.prefix-chip:hover {
		border-color: var(--primary);
	}

	.prefix-chip.active {
		background: var(--primary);
		border-color: var(--primary);
		color: var(--primary-foreground);
	}
</style>
