<script lang="ts">
	/**
	 * AgentLogs - Log viewer component
	 *
	 * Features:
	 * - Scrollable log area
	 * - Search/filter input
	 * - Log level filter: All, Info, Warn, Error, Debug
	 * - Timestamp toggle
	 * - Auto-scroll toggle
	 * - Copy all button
	 * - Monospace font, dark background
	 */
	import { onMount } from 'svelte';
	import { Search, Copy, ArrowDown, Clock, Loader2, RefreshCw } from 'lucide-svelte';
	import { tick } from 'svelte';
	import { agents, type AgentLogEntry } from '$lib/stores/agents';

	type LogLevel = 'info' | 'warning' | 'error' | 'debug';

	interface Props {
		agentId: string;
		initialLogs?: AgentLogEntry[];
	}

	let { agentId, initialLogs = [] }: Props = $props();

	// State
	let searchQuery = $state('');
	let levelFilter = $state<'all' | LogLevel>('all');
	let showTimestamps = $state(true);
	let autoScroll = $state(true);
	let copied = $state(false);
	let loading = $state(false);
	let logs = $state<AgentLogEntry[]>(initialLogs);

	let logContainer: HTMLDivElement | undefined = $state();

	// Fetch logs on mount
	onMount(async () => {
		if (logs.length === 0) {
			await fetchLogs();
		}
	});

	async function fetchLogs() {
		loading = true;
		try {
			const fetchedLogs = await agents.fetchLogs(agentId, { limit: 500 });
			logs = fetchedLogs;
		} catch (err) {
			console.error('Failed to fetch logs:', err);
		} finally {
			loading = false;
		}
	}

	async function refreshLogs() {
		await fetchLogs();
		if (autoScroll) {
			await scrollToBottom();
		}
	}

	// Filtered logs
	const filteredLogs = $derived(() => {
		let filtered = logs;

		// Filter by level
		if (levelFilter !== 'all') {
			filtered = filtered.filter(log => log.level === levelFilter);
		}

		// Filter by search query
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			filtered = filtered.filter(log => log.message.toLowerCase().includes(query));
		}

		return filtered;
	});

	// Level colors
	const levelColors: Record<LogLevel, string> = {
		info: 'text-blue-400',
		warning: 'text-yellow-400',
		error: 'text-red-400',
		debug: 'text-gray-400'
	};

	// Format timestamp
	function formatTimestamp(date: Date): string {
		return date.toLocaleTimeString([], {
			hour: '2-digit',
			minute: '2-digit',
			second: '2-digit'
		});
	}

	// Copy all logs
	async function copyAllLogs() {
		const text = filteredLogs()
			.map(log => {
				const time = showTimestamps ? `[${formatTimestamp(log.timestamp)}] ` : '';
				return `${time}[${log.level.toUpperCase()}] ${log.message}`;
			})
			.join('\n');

		await navigator.clipboard.writeText(text);
		copied = true;
		setTimeout(() => copied = false, 2000);
	}

	// Scroll to bottom
	async function scrollToBottom() {
		if (logContainer) {
			await tick();
			logContainer.scrollTop = logContainer.scrollHeight;
		}
	}

	// Auto-scroll effect
	$effect(() => {
		if (autoScroll && filteredLogs()) {
			scrollToBottom();
		}
	});

	// Watch for new logs from initialLogs prop (real-time updates via WebSocket)
	$effect(() => {
		if (initialLogs.length > logs.length) {
			logs = initialLogs;
		}
	});
</script>

<div class="h-full flex flex-col bg-zinc-900">
	<!-- Toolbar -->
	<div class="flex items-center gap-2 px-4 py-2 border-b border-zinc-700 flex-wrap">
		<!-- Search -->
		<div class="relative flex-1 min-w-[150px]">
			<Search class="absolute left-2.5 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
			<input
				type="text"
				bind:value={searchQuery}
				placeholder="Filter logs..."
				class="w-full pl-8 pr-3 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 placeholder:text-zinc-500 focus:outline-none focus:ring-1 focus:ring-primary/50"
			/>
		</div>

		<!-- Level filter -->
		<select
			bind:value={levelFilter}
			class="px-2 py-1.5 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:ring-1 focus:ring-primary/50"
		>
			<option value="all">All Levels</option>
			<option value="info">Info</option>
			<option value="warning">Warning</option>
			<option value="error">Error</option>
			<option value="debug">Debug</option>
		</select>

		<!-- Refresh button -->
		<button
			onclick={refreshLogs}
			disabled={loading}
			class="p-1.5 rounded-lg transition-colors text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
			title="Refresh logs"
		>
			<RefreshCw class="w-4 h-4 {loading ? 'animate-spin' : ''}" />
		</button>

		<!-- Timestamp toggle -->
		<button
			onclick={() => showTimestamps = !showTimestamps}
			class="p-1.5 rounded-lg transition-colors {showTimestamps ? 'bg-primary/20 text-primary' : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800'}"
			title="Toggle timestamps"
		>
			<Clock class="w-4 h-4" />
		</button>

		<!-- Auto-scroll toggle -->
		<button
			onclick={() => autoScroll = !autoScroll}
			class="p-1.5 rounded-lg transition-colors {autoScroll ? 'bg-primary/20 text-primary' : 'text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800'}"
			title="Auto-scroll"
		>
			<ArrowDown class="w-4 h-4" />
		</button>

		<!-- Copy button -->
		<button
			onclick={copyAllLogs}
			class="p-1.5 text-zinc-500 hover:text-zinc-300 rounded-lg hover:bg-zinc-800 transition-colors"
			title="Copy all"
		>
			{#if copied}
				<span class="text-xs text-green-400">Copied!</span>
			{:else}
				<Copy class="w-4 h-4" />
			{/if}
		</button>
	</div>

	<!-- Log content -->
	<div
		bind:this={logContainer}
		class="flex-1 overflow-y-auto p-4 font-mono text-sm"
	>
		{#if loading && logs.length === 0}
			<div class="flex items-center justify-center h-full">
				<Loader2 class="w-6 h-6 text-zinc-500 animate-spin" />
			</div>
		{:else if filteredLogs().length === 0}
			<div class="flex items-center justify-center h-full text-zinc-500">
				{#if searchQuery || levelFilter !== 'all'}
					No logs match your filter
				{:else}
					No logs yet
				{/if}
			</div>
		{:else}
			<div class="space-y-1">
				{#each filteredLogs() as log, index (log.timestamp.getTime() + '-' + index)}
					<div class="flex items-start gap-2 hover:bg-zinc-800/50 px-2 py-0.5 rounded">
						{#if showTimestamps}
							<span class="text-zinc-500 flex-shrink-0">
								{formatTimestamp(log.timestamp)}
							</span>
						{/if}
						<span class="flex-shrink-0 w-14 text-right {levelColors[log.level]}">
							[{log.level.toUpperCase().slice(0, 4)}]
						</span>
						<span class="text-zinc-200 break-all">
							{log.message}
						</span>
					</div>
				{/each}
			</div>
		{/if}
	</div>

	<!-- Status bar -->
	<div class="px-4 py-1.5 border-t border-zinc-700 text-xs text-zinc-500 flex items-center justify-between">
		<span>{filteredLogs().length} {filteredLogs().length === 1 ? 'entry' : 'entries'}</span>
		<div class="flex items-center gap-3">
			{#if loading}
				<span class="text-primary">Loading...</span>
			{/if}
			{#if autoScroll}
				<span class="text-primary">Auto-scrolling</span>
			{/if}
		</div>
	</div>
</div>
