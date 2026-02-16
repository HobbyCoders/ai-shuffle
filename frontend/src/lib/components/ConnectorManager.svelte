<script lang="ts">
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
	import { slide, fade } from 'svelte/transition';
	import { quintOut } from 'svelte/easing';
	import type {
		Connector,
		ConnectorTypeInfo,
		ConnectorCreate,
		ConnectorEventLog
	} from '$lib/api/connectors';
	import {
		getConnectors,
		createConnector,
		updateConnector,
		deleteConnector,
		testConnector,
		activateConnector,
		deactivateConnector,
		getConnectorTypes,
		getConnectorEvents,
		getConnectorLogs
	} from '$lib/api/connectors';
	import type { ApiUser } from '$lib/api/apiUsers';
	import { getApiUsers } from '$lib/api/apiUsers';
	import { formatDateTime } from '$lib/utils/dateFormatting';

	// State
	let connectors: Connector[] = $state([]);
	let connectorTypes: ConnectorTypeInfo[] = $state([]);
	let availableEvents: string[] = $state([]);
	let apiUsers: ApiUser[] = $state([]);
	let loading = $state(true);
	let error = $state('');

	// UI state
	let view: 'list' | 'add' | 'setup' | 'edit' = $state('list');
	let selectedType: ConnectorTypeInfo | null = $state(null);
	let editingConnector: Connector | null = $state(null);
	let expandedLogs: Set<string> = $state(new Set());
	let connectorLogs: Map<string, ConnectorEventLog[]> = $state(new Map());
	let loadingLogs: Set<string> = $state(new Set());

	// Form state
	let formName = $state('');
	let formApiUserId = $state('');
	let formConfig: Record<string, string> = $state({});
	let formEvents: string[] = $state([]);
	let formSaving = $state(false);
	let formError = $state('');
	let testResult: { success: boolean; message: string } | null = $state(null);
	let testingId: string | null = $state(null);
	let deletingId: string | null = $state(null);

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
				connectors = updated;
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

	async function loadData() {
		loading = true;
		error = '';
		try {
			const [connectorsData, typesData, eventsData, apiUsersData] = await Promise.all([
				getConnectors(),
				getConnectorTypes(),
				getConnectorEvents(),
				getApiUsers()
			]);
			connectors = connectorsData;
			connectorTypes = typesData;
			availableEvents = eventsData;
			apiUsers = apiUsersData.filter(u => u.is_active);
		} catch (e: any) {
			error = e.detail || 'Failed to load connectors';
		} finally {
			loading = false;
		}
	}

	async function handleCreate() {
		if (!selectedType) return;
		if (!formName.trim()) {
			formError = 'Name is required';
			return;
		}
		if (formEvents.length === 0) {
			formError = 'At least one event must be selected';
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
			await loadData();
			cancelForm();
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
			await loadData();
			cancelForm();
		} catch (e: any) {
			formError = e.detail || 'Failed to update connector';
		} finally {
			formSaving = false;
		}
	}

	async function handleDelete(id: string) {
		deletingId = id;
	}

	async function confirmDelete(id: string) {
		try {
			await deleteConnector(id);
			deletingId = null;
			await loadData();
		} catch (e: any) {
			error = e.detail || 'Failed to delete connector';
			deletingId = null;
		}
	}

	function cancelDelete() {
		deletingId = null;
	}

	async function handleTest(id: string) {
		testingId = id;
		testResult = null;

		try {
			const result = await testConnector(id);
			testResult = {
				success: result.success,
				message: result.success
					? `Connection successful${result.response_time_ms ? ` (${result.response_time_ms}ms)` : ''}`
					: result.error || result.message || 'Test failed'
			};
		} catch (e: any) {
			testResult = {
				success: false,
				message: e.detail || 'Failed to test connector'
			};
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
			error = e.detail || 'Failed to toggle connector';
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
		view = 'add';
		testResult = null;
	}

	function selectType(type: ConnectorTypeInfo) {
		selectedType = type;
		formName = '';
		formConfig = {};
		formEvents = [];
		formError = '';
		view = 'setup';
	}

	function startEdit(connector: Connector) {
		editingConnector = connector;
		formName = connector.name;
		formConfig = { ...connector.config };
		formEvents = [...connector.events];
		formError = '';

		// Find the type info for this connector
		selectedType = connectorTypes.find((t) => t.type === connector.type) || null;
		view = 'edit';
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
	}

	function toggleEvent(event: string) {
		if (formEvents.includes(event)) {
			formEvents = formEvents.filter((e) => e !== event);
		} else {
			formEvents = [...formEvents, event];
		}
	}

	function getStatusColor(status: Connector['status']): string {
		switch (status) {
			case 'connected':
				return 'bg-success';
			case 'connecting':
				return 'bg-warning';
			case 'error':
				return 'bg-destructive';
			default:
				return 'bg-muted-foreground';
		}
	}

	function getStatusBadgeClass(status: Connector['status']): string {
		switch (status) {
			case 'connected':
				return 'badge success';
			case 'connecting':
				return 'badge warning';
			case 'error':
				return 'badge danger';
			default:
				return 'badge muted';
		}
	}


	function getTypeBadgeClass(type: string): string {
		switch (type) {
			case 'telegram':
				return 'badge-type badge-telegram';
			case 'discord':
				return 'badge-type badge-discord';
			case 'whatsapp':
				return 'badge-type badge-whatsapp';
			case 'slack':
				return 'badge-type badge-slack';
			case 'line':
				return 'badge-type badge-line';
			case 'signal':
				return 'badge-type badge-signal';
			case 'imessage':
				return 'badge-type badge-imessage';
			case 'msteams':
				return 'badge-type badge-msteams';
			case 'bluebubbles':
				return 'badge-type badge-bluebubbles';
			default:
				return 'badge-type badge-webhook';
		}
	}
</script>

<div class="connector-manager">
	{#if loading}
		<div class="text-center py-8 text-muted-foreground text-sm">Loading connectors...</div>
	{:else if error && view === 'list'}
		<div
			class="bg-status-error border border-status-error rounded-lg p-3 text-destructive text-sm mb-4"
		>
			{error}
			<button
				onclick={() => {
					error = '';
				}}
				class="ml-2 text-xs underline opacity-70 hover:opacity-100"
			>
				Dismiss
			</button>
		</div>
	{/if}

	<!-- ============================================================ -->
	<!-- LIST VIEW -->
	<!-- ============================================================ -->
	{#if view === 'list' && !loading}
		<div class="space-y-4" transition:fade={{ duration: 150 }}>
			<!-- Header -->
			<div class="flex items-center justify-between">
				<div>
					<h3 class="text-sm font-medium text-foreground">Connectors</h3>
					<p class="text-xs text-muted-foreground mt-0.5">
						Connect external messaging platforms and webhooks
					</p>
				</div>
				<button
					onclick={startAdd}
					class="px-3 py-1.5 bg-primary hover:bg-primary/90 text-primary-foreground text-sm rounded-md transition-colors"
				>
					Add Connector
				</button>
			</div>

			<!-- Test Result Banner -->
			{#if testResult}
				<div
					class={`rounded-lg p-3 text-sm ${testResult.success ? 'bg-status-success border border-status-success text-success' : 'bg-status-error border border-status-error text-destructive'}`}
					transition:slide={{ duration: 200, easing: quintOut }}
				>
					{testResult.message}
					<button
						onclick={() => {
							testResult = null;
						}}
						class="ml-2 text-xs underline opacity-70 hover:opacity-100"
					>
						Dismiss
					</button>
				</div>
			{/if}

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
					<p class="text-sm font-medium mb-1">No connectors configured</p>
					<p class="text-xs">Add a connector to integrate with external messaging platforms.</p>
				</div>
			{:else}
				<div class="space-y-2">
					{#each connectors as connector (connector.id)}
						<div
							class="connector-item bg-card/30 rounded-lg border border-border transition-all"
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
										<div class="flex items-center gap-2">
											<button
												onclick={() => confirmDelete(connector.id)}
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
									<!-- Main Row -->
									<div class="flex items-start justify-between gap-4">
										<div class="flex-1 min-w-0">
											<div class="flex items-center gap-2 flex-wrap">
												<!-- Status Dot -->
												<span
													class={`w-2 h-2 rounded-full flex-shrink-0 ${getStatusColor(connector.status)}`}
												></span>
												<!-- Name -->
												<span class="text-sm font-medium text-foreground truncate">
													{connector.name}
												</span>
												<!-- Type Badge -->
												<span class={getTypeBadgeClass(connector.type)}>
													{connector.type}
												</span>
												<!-- Status Badge -->
												<span class={getStatusBadgeClass(connector.status)}>
													{connector.status}
												</span>
											</div>

											<!-- Event tags -->
											<div class="flex flex-wrap items-center gap-1.5 mt-1.5">
												{#each connector.events as event}
													<span
														class="px-1.5 py-0.5 bg-primary/20 text-primary text-xs rounded"
													>
														{event}
													</span>
												{/each}
											</div>

											<!-- Meta info -->
											<div
												class="flex items-center gap-4 mt-2 text-xs text-muted-foreground"
											>
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
										<div class="flex items-center gap-1 flex-shrink-0">
											<!-- Toggle Switch -->
											<button
												class="toggle small"
												class:active={connector.is_active}
												onclick={() => handleToggle(connector)}
												title={connector.is_active ? 'Deactivate' : 'Activate'}
											>
												<span class="toggle-knob"></span>
											</button>

											<!-- Test -->
											<button
												onclick={() => handleTest(connector.id)}
												disabled={testingId === connector.id}
												class="connector-action-btn p-1.5 text-muted-foreground hover:text-primary rounded transition-colors disabled:opacity-50"
												title="Test connection"
											>
												{#if testingId === connector.id}
													<svg
														class="w-4 h-4 animate-spin"
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
														class="w-4 h-4"
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
												class="connector-action-btn p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
												class:text-primary={expandedLogs.has(connector.id)}
												title="View event logs"
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
														d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"
													/>
												</svg>
											</button>

											<!-- Edit -->
											<button
												onclick={() => startEdit(connector)}
												class="connector-action-btn p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors"
												title="Edit connector"
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
														d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z"
													/>
												</svg>
											</button>

											<!-- Delete -->
											<button
												onclick={() => handleDelete(connector.id)}
												class="connector-action-btn p-1.5 text-muted-foreground hover:text-destructive rounded transition-colors"
												title="Delete connector"
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
																	class="px-1.5 py-0.5 rounded text-xs {log.direction === 'outbound' ? 'log-dir-out' : 'log-dir-in'}"
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
					class="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors hover:bg-secondary/50"
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
					class="p-1.5 text-muted-foreground hover:text-foreground rounded transition-colors hover:bg-secondary/50"
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
					<div
						class="bg-status-error border border-status-error rounded-md p-2 text-destructive text-xs mb-3"
						transition:slide={{ duration: 200, easing: quintOut }}
					>
						{formError}
					</div>
				{/if}

				<div class="space-y-4">
					<!-- Name -->
					<div>
						<label class="block text-xs font-medium text-muted-foreground mb-1">
							Connector Name <span class="text-destructive">*</span>
						</label>
						<input
							type="text"
							bind:value={formName}
							placeholder="e.g., My Telegram Bot"
							class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors"
						/>
					</div>

					<!-- API User -->
					{#if view === 'setup'}
						<div>
							<label class="block text-xs font-medium text-muted-foreground mb-1">
								API User
							</label>
							<select
								bind:value={formApiUserId}
								class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground focus:outline-none focus:border-primary transition-colors"
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

					<!-- Dynamic Config Fields -->
					{#if selectedType}
						{#each selectedType.config_fields as field (field.key)}
							<div>
								<label class="block text-xs font-medium text-muted-foreground mb-1">
									{field.label}
									{#if field.required}
										<span class="text-destructive">*</span>
									{/if}
								</label>
								{#if field.type === 'textarea'}
									<textarea
										bind:value={formConfig[field.key]}
										placeholder={field.placeholder || ''}
										rows="3"
										class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors resize-vertical"
									></textarea>
								{:else if field.type === 'password'}
									<input
										type="password"
										bind:value={formConfig[field.key]}
										placeholder={field.placeholder || ''}
										class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors font-mono"
									/>
								{:else}
									<input
										type="text"
										bind:value={formConfig[field.key]}
										placeholder={field.placeholder || ''}
										class="w-full px-3 py-2 bg-input border border-border rounded-md text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:border-primary transition-colors"
									/>
								{/if}
								{#if field.description}
									<p class="text-xs text-muted-foreground mt-1">{field.description}</p>
								{/if}
							</div>
						{/each}
					{/if}

					<!-- Event Selection -->
					<div>
						<label class="block text-xs font-medium text-muted-foreground mb-2">
							Events <span class="text-destructive">*</span>
						</label>
						<p class="text-xs text-muted-foreground mb-2">
							Select which events this connector should listen to
						</p>
						<div class="space-y-2">
							{#each availableEvents as event}
								<label class="flex items-center gap-2 cursor-pointer group">
									<input
										type="checkbox"
										checked={formEvents.includes(event)}
										onchange={() => toggleEvent(event)}
										class="w-4 h-4 rounded border-border bg-input text-primary focus:ring-primary focus:ring-offset-background"
									/>
									<span
										class="text-sm text-foreground group-hover:text-primary transition-colors"
										>{event}</span
									>
								</label>
							{/each}
						</div>
					</div>

					<!-- Actions -->
					<div class="flex items-center gap-2 pt-2">
						<button
							onclick={view === 'edit' ? handleUpdate : handleCreate}
							disabled={formSaving}
							class="px-4 py-2 bg-primary hover:bg-primary/90 disabled:bg-primary/50 text-primary-foreground text-sm rounded-md transition-colors font-medium"
						>
							{formSaving
								? 'Saving...'
								: view === 'edit'
									? 'Update Connector'
									: 'Create Connector'}
						</button>
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
	.connector-manager {
		/* Inherit from parent layout */
	}

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
	.connector-item {
		transition: all 150ms ease;
	}

	.connector-item:hover {
		border-color: color-mix(in oklch, var(--primary) 30%, var(--border));
	}

	.connector-item--deleting {
		background: color-mix(in oklch, var(--destructive) 8%, transparent);
		border-color: color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.connector-action-btn {
		transition: all 100ms;
	}

	.connector-action-btn:hover {
		background: var(--hover-overlay);
	}

	.connector-action-btn:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	/* ========================================
	   Toggle Switch (matching SettingsCard)
	   ======================================== */
	.toggle {
		position: relative;
		width: 44px;
		height: 24px;
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius, 0.75rem);
		cursor: pointer;
		transition: all 200ms ease;
		flex-shrink: 0;
	}

	.toggle:hover {
		border-color: var(--ring);
	}

	.toggle:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: 2px;
	}

	.toggle.active {
		background: var(--primary);
		border-color: var(--primary);
	}

	.toggle.small {
		width: 36px;
		height: 20px;
		border-radius: calc(var(--radius, 0.75rem) * 0.83);
	}

	.toggle-knob {
		position: absolute;
		top: 2px;
		left: 2px;
		width: 18px;
		height: 18px;
		background: var(--primary-foreground);
		border-radius: 50%;
		transition: transform 200ms ease;
	}

	.toggle.active .toggle-knob {
		transform: translateX(20px);
	}

	.toggle.small .toggle-knob {
		width: 14px;
		height: 14px;
	}

	.toggle.small.active .toggle-knob {
		transform: translateX(16px);
	}

	/* ========================================
	   Type Badges
	   ======================================== */
	.badge-type {
		display: inline-flex;
		align-items: center;
		padding: 1px 6px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 4px;
		text-transform: uppercase;
		letter-spacing: 0.02em;
	}

	.badge-telegram {
		background: color-mix(in oklch, oklch(0.6 0.15 230) 15%, transparent);
		color: oklch(0.6 0.15 230);
		border: 1px solid color-mix(in oklch, oklch(0.6 0.15 230) 30%, transparent);
	}

	.badge-discord {
		background: color-mix(in oklch, oklch(0.55 0.18 280) 15%, transparent);
		color: oklch(0.55 0.18 280);
		border: 1px solid color-mix(in oklch, oklch(0.55 0.18 280) 30%, transparent);
	}

	.badge-whatsapp {
		background: color-mix(in oklch, oklch(0.65 0.2 150) 15%, transparent);
		color: oklch(0.65 0.2 150);
		border: 1px solid color-mix(in oklch, oklch(0.65 0.2 150) 30%, transparent);
	}

	.badge-webhook {
		background: color-mix(in oklch, var(--muted-foreground) 15%, transparent);
		color: var(--muted-foreground);
		border: 1px solid color-mix(in oklch, var(--muted-foreground) 30%, transparent);
	}

	.badge-slack {
		background: color-mix(in oklch, oklch(0.6 0.15 330) 15%, transparent);
		color: oklch(0.6 0.15 330);
		border: 1px solid color-mix(in oklch, oklch(0.6 0.15 330) 30%, transparent);
	}

	.badge-line {
		background: color-mix(in oklch, oklch(0.65 0.2 150) 15%, transparent);
		color: oklch(0.65 0.2 150);
		border: 1px solid color-mix(in oklch, oklch(0.65 0.2 150) 30%, transparent);
	}

	.badge-signal {
		background: color-mix(in oklch, oklch(0.6 0.18 250) 15%, transparent);
		color: oklch(0.6 0.18 250);
		border: 1px solid color-mix(in oklch, oklch(0.6 0.18 250) 30%, transparent);
	}

	.badge-imessage {
		background: color-mix(in oklch, oklch(0.6 0.15 260) 15%, transparent);
		color: oklch(0.6 0.15 260);
		border: 1px solid color-mix(in oklch, oklch(0.6 0.15 260) 30%, transparent);
	}

	.badge-msteams {
		background: color-mix(in oklch, oklch(0.55 0.18 280) 15%, transparent);
		color: oklch(0.55 0.18 280);
		border: 1px solid color-mix(in oklch, oklch(0.55 0.18 280) 30%, transparent);
	}

	.badge-bluebubbles {
		background: color-mix(in oklch, oklch(0.6 0.17 240) 15%, transparent);
		color: oklch(0.6 0.17 240);
		border: 1px solid color-mix(in oklch, oklch(0.6 0.17 240) 30%, transparent);
	}

	/* Status badges */
	.badge {
		display: inline-flex;
		align-items: center;
		padding: 1px 6px;
		font-size: 0.6875rem;
		font-weight: 600;
		border-radius: 9999px;
		letter-spacing: 0.02em;
	}

	.badge.success {
		background: color-mix(in oklch, var(--success) 15%, transparent);
		color: var(--success);
		border: 1px solid color-mix(in oklch, var(--success) 30%, transparent);
	}

	.badge.warning {
		background: color-mix(in oklch, var(--warning) 15%, transparent);
		color: var(--warning);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}

	.badge.danger {
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
		color: var(--destructive);
		border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent);
	}

	.badge.muted {
		background: var(--muted);
		color: var(--muted-foreground);
		border: 1px solid var(--border);
	}

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
</style>
