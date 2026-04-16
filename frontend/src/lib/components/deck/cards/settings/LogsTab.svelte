<script lang="ts">
	import { fade } from 'svelte/transition';
	import { api, fetchAndDownload } from '$lib/api/client';
	import './settings-shared.css';

	interface LogLine {
		timestamp: string;
		level: string;
		logger_name: string;
		message: string;
	}

	interface LogsResponse {
		lines: LogLine[];
		total: number;
		returned: number;
		levels: string[];
	}

	let logs = $state<LogLine[]>([]);
	let totalLogs = $state(0);
	let returnedLogs = $state(0);
	let availableLevels = $state<string[]>([]);
	let loading = $state(true);
	let error = $state('');
	let selectedLevel = $state('');
	let searchQuery = $state('');
	let debouncedSearch = $state('');
	let exporting = $state(false);
	let logViewerEl = $state<HTMLDivElement | null>(null);
	let searchTimeout: ReturnType<typeof setTimeout> | null = null;
	let refreshInterval: ReturnType<typeof setInterval> | null = null;
	let initialLoadDone = $state(false);

	const LEVEL_ORDER = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

	function levelClass(level: string): string {
		switch (level.toUpperCase()) {
			case 'DEBUG': return 'debug';
			case 'INFO': return 'info';
			case 'WARNING': return 'warning';
			case 'ERROR':
			case 'CRITICAL': return 'error';
			default: return 'debug';
		}
	}

	function formatTimestamp(ts: string): string {
		try {
			const d = new Date(ts);
			const h = String(d.getHours()).padStart(2, '0');
			const m = String(d.getMinutes()).padStart(2, '0');
			const s = String(d.getSeconds()).padStart(2, '0');
			const ms = String(d.getMilliseconds()).padStart(3, '0');
			return `${h}:${m}:${s}.${ms}`;
		} catch {
			return ts;
		}
	}

	async function fetchLogs() {
		try {
			error = '';
			const params = new URLSearchParams({ limit: '500' });
			if (selectedLevel) params.set('level', selectedLevel);
			if (debouncedSearch) params.set('search', debouncedSearch);

			const data = await api.get<LogsResponse>(`/logs?${params.toString()}`);
			logs = data.lines;
			totalLogs = data.total;
			returnedLogs = data.returned;
			if (data.levels.length > 0) {
				availableLevels = data.levels;
			}
		} catch (err: unknown) {
			const e = err as { detail?: string };
			error = e.detail || 'Failed to load logs';
		} finally {
			loading = false;
		}
	}

	async function handleExport() {
		exporting = true;
		try {
			await fetchAndDownload('/logs/export', 'ai-shuffle-logs.log');
		} catch (err: unknown) {
			const e = err as { detail?: string };
			error = e.detail || 'Export failed';
		} finally {
			exporting = false;
		}
	}

	function handleRefresh() {
		loading = true;
		fetchLogs();
	}

	// Debounce search input
	$effect(() => {
		// Access searchQuery to track it
		const q = searchQuery;
		if (searchTimeout) clearTimeout(searchTimeout);
		searchTimeout = setTimeout(() => {
			debouncedSearch = q;
		}, 300);
		return () => {
			if (searchTimeout) clearTimeout(searchTimeout);
		};
	});

	// Fetch when filters change
	$effect(() => {
		// Track reactive dependencies
		selectedLevel;
		debouncedSearch;
		fetchLogs();
	});

	// Auto-scroll to bottom on initial load
	$effect(() => {
		if (!loading && logs.length > 0 && !initialLoadDone && logViewerEl) {
			// Use tick-like delay to ensure DOM is rendered
			requestAnimationFrame(() => {
				if (logViewerEl) {
					logViewerEl.scrollTop = logViewerEl.scrollHeight;
				}
			});
			initialLoadDone = true;
		}
	});

	// Auto-refresh every 10 seconds
	$effect(() => {
		refreshInterval = setInterval(() => {
			fetchLogs();
		}, 10_000);
		return () => {
			if (refreshInterval) clearInterval(refreshInterval);
		};
	});

	let displayedLogs = $derived(
		[...logs].reverse()
	);

	let sortedLevels = $derived(
		Array.from(new Set([...LEVEL_ORDER, ...availableLevels]))
			.sort((a, b) => {
				const ai = LEVEL_ORDER.indexOf(a);
				const bi = LEVEL_ORDER.indexOf(b);
				if (ai === -1 && bi === -1) return a.localeCompare(b);
				if (ai === -1) return 1;
				if (bi === -1) return -1;
				return ai - bi;
			})
	);
</script>

<div class="logs-tab" transition:fade={{ duration: 150 }}>
	<!-- Toolbar -->
	<div class="logs-toolbar">
		<div class="toolbar-filters">
			<select
				class="input compact level-select"
				bind:value={selectedLevel}
			>
				<option value="">All Levels</option>
				{#each sortedLevels as level}
					<option value={level}>{level}</option>
				{/each}
			</select>

			<input
				type="text"
				class="input compact search-input"
				placeholder="Search logs..."
				bind:value={searchQuery}
			/>
		</div>

		<div class="toolbar-actions">
			<button
				class="btn secondary compact"
				onclick={handleRefresh}
				disabled={loading}
				title="Refresh logs"
			>
				<svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<polyline points="23 4 23 10 17 10"/>
					<polyline points="1 20 1 14 7 14"/>
					<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
				</svg>
				<span class="btn-label">Refresh</span>
			</button>

			<button
				class="btn secondary compact"
				onclick={handleExport}
				disabled={exporting}
				title="Export logs"
			>
				<svg class="btn-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
					<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
					<polyline points="7 10 12 15 17 10"/>
					<line x1="12" y1="15" x2="12" y2="3"/>
				</svg>
				<span class="btn-label">{exporting ? 'Exporting...' : 'Export'}</span>
			</button>
		</div>
	</div>

	<!-- Status line -->
	<div class="logs-status">
		{#if !loading && !error}
			<span class="status-text">Showing {returnedLogs} of {totalLogs} logs</span>
		{/if}
		{#if error}
			<span class="status-text status-error">{error}</span>
		{/if}
	</div>

	<!-- Log viewer -->
	<div class="log-viewer" bind:this={logViewerEl}>
		{#if loading && logs.length === 0}
			<div class="logs-empty">
				<div class="loading-spinner"></div>
				<span>Loading logs...</span>
			</div>
		{:else if !loading && displayedLogs.length === 0}
			<div class="logs-empty">
				<svg class="empty-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
					<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
					<polyline points="14 2 14 8 20 8"/>
					<line x1="16" y1="13" x2="8" y2="13"/>
					<line x1="16" y1="17" x2="8" y2="17"/>
				</svg>
				<span>No logs found</span>
				{#if selectedLevel || debouncedSearch}
					<span class="empty-hint">Try adjusting your filters</span>
				{/if}
			</div>
		{:else}
			{#each displayedLogs as line}
				<div class="log-line">
					<span class="log-time">{formatTimestamp(line.timestamp)}</span>
					<span class="log-level log-level-{levelClass(line.level)}">{line.level}</span>
					<span class="log-msg">{line.message}</span>
				</div>
			{/each}
		{/if}
	</div>
</div>

<style>
	.logs-tab {
		display: flex;
		flex-direction: column;
		gap: 0;
		height: 100%;
		min-height: 0;
	}

	/* Toolbar */
	.logs-toolbar {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 10px 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: var(--radius) var(--radius) 0 0;
		position: sticky;
		top: 0;
		z-index: 2;
		flex-wrap: wrap;
	}

	.toolbar-filters {
		display: flex;
		gap: 8px;
		align-items: center;
		flex: 1;
		min-width: 0;
	}

	.level-select {
		width: 130px;
		flex-shrink: 0;
	}

	.search-input {
		flex: 1;
		min-width: 120px;
		max-width: 280px;
	}

	.toolbar-actions {
		display: flex;
		gap: 6px;
		align-items: center;
		flex-shrink: 0;
	}

	.btn-icon {
		width: 14px;
		height: 14px;
		flex-shrink: 0;
	}

	.btn-label {
		white-space: nowrap;
	}

	/* Status line */
	.logs-status {
		padding: 4px 12px;
		background: var(--muted);
		border-left: 1px solid var(--border);
		border-right: 1px solid var(--border);
		min-height: 24px;
		display: flex;
		align-items: center;
	}

	.status-text {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
		font-family: var(--font-mono);
	}

	.status-error {
		color: var(--destructive);
	}

	/* Log viewer */
	.log-viewer {
		flex: 1;
		overflow-y: auto;
		background: var(--muted);
		border: 1px solid var(--border);
		border-top: none;
		border-radius: 0 0 var(--radius) var(--radius);
		padding: 6px 0;
		min-height: 200px;
		max-height: 520px;
		font-family: var(--font-mono);
		font-size: 0.75rem;
		line-height: 1.6;
	}

	/* Log line */
	.log-line {
		display: flex;
		gap: 8px;
		padding: 1px 12px;
		align-items: baseline;
		transition: background 80ms ease;
	}

	.log-line:hover {
		background: var(--hover-overlay);
	}

	.log-time {
		color: var(--muted-foreground);
		flex-shrink: 0;
		font-variant-numeric: tabular-nums;
		opacity: 0.7;
	}

	.log-level {
		flex-shrink: 0;
		font-weight: 600;
		font-size: 0.625rem;
		text-transform: uppercase;
		letter-spacing: 0.03em;
		padding: 0 5px;
		border-radius: 3px;
		text-align: center;
		min-width: 58px;
	}

	.log-level-debug {
		color: var(--muted-foreground);
		background: color-mix(in oklch, var(--muted-foreground) 12%, transparent);
	}

	.log-level-info {
		color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent);
	}

	.log-level-warning {
		color: var(--warning);
		background: color-mix(in oklch, var(--warning) 15%, transparent);
	}

	.log-level-error {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 15%, transparent);
	}

	.log-msg {
		color: var(--foreground);
		word-break: break-word;
		white-space: pre-wrap;
	}

	/* Empty / loading states */
	.logs-empty {
		display: flex;
		flex-direction: column;
		align-items: center;
		justify-content: center;
		gap: 8px;
		padding: 48px 16px;
		color: var(--muted-foreground);
		font-family: var(--font-sans, sans-serif);
		font-size: 0.875rem;
	}

	.empty-icon {
		width: 32px;
		height: 32px;
		opacity: 0.4;
	}

	.empty-hint {
		font-size: 0.75rem;
		opacity: 0.6;
	}

	.loading-spinner {
		width: 20px;
		height: 20px;
		border: 2px solid var(--border);
		border-top-color: var(--primary);
		border-radius: 50%;
		animation: spin 0.7s linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	/* Mobile adjustments */
	@media (max-width: 639px) {
		.logs-toolbar {
			flex-direction: column;
			align-items: stretch;
		}

		.toolbar-filters {
			flex-direction: column;
		}

		.level-select {
			width: 100%;
		}

		.search-input {
			max-width: none;
		}

		.toolbar-actions {
			justify-content: flex-end;
		}

		.log-line {
			flex-wrap: wrap;
			gap: 4px;
			padding: 4px 10px;
		}

		.log-time {
			font-size: 0.6875rem;
		}

		.log-viewer {
			max-height: 400px;
		}
	}
</style>
