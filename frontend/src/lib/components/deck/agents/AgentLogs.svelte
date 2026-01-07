<script lang="ts">
	/**
	 * AgentLogs - Log viewer component
	 *
	 * Features:
	 * - Scrollable log area
	 * - Search/filter input
	 * - Log level filter: All, Info, Warn, Error
	 * - Timestamp toggle
	 * - Auto-scroll toggle
	 * - Copy all button
	 * - Monospace font, dark background
	 */
	import { Search, Copy, ArrowDown, Clock } from 'lucide-svelte';
	import { tick } from 'svelte';

	type LogLevel = 'info' | 'warn' | 'error' | 'debug';

	interface LogEntry {
		id: string;
		timestamp: Date;
		level: LogLevel;
		message: string;
	}

	// State
	let searchQuery = $state('');
	let levelFilter = $state<'all' | LogLevel>('all');
	let showTimestamps = $state(true);
	let autoScroll = $state(true);
	let copied = $state(false);

	let logContainer: HTMLDivElement | undefined = $state();

	// Mock log data
	const mockLogs: LogEntry[] = [
		{ id: '1', timestamp: new Date(Date.now() - 300000), level: 'info', message: 'Agent started' },
		{ id: '2', timestamp: new Date(Date.now() - 295000), level: 'info', message: 'Analyzing task requirements...' },
		{ id: '3', timestamp: new Date(Date.now() - 290000), level: 'debug', message: 'Parsed 15 requirement items' },
		{ id: '4', timestamp: new Date(Date.now() - 280000), level: 'info', message: 'Creating feature branch: feature/auth' },
		{ id: '5', timestamp: new Date(Date.now() - 275000), level: 'info', message: 'Branch created successfully' },
		{ id: '6', timestamp: new Date(Date.now() - 260000), level: 'info', message: 'Starting subtask: Set up authentication middleware' },
		{ id: '7', timestamp: new Date(Date.now() - 250000), level: 'debug', message: 'Reading existing middleware files...' },
		{ id: '8', timestamp: new Date(Date.now() - 240000), level: 'info', message: 'Creating file: src/middleware/auth.ts' },
		{ id: '9', timestamp: new Date(Date.now() - 230000), level: 'debug', message: 'Generating JWT validation logic' },
		{ id: '10', timestamp: new Date(Date.now() - 220000), level: 'info', message: 'File created: src/middleware/auth.ts' },
		{ id: '11', timestamp: new Date(Date.now() - 200000), level: 'info', message: 'Starting subtask: Create user model' },
		{ id: '12', timestamp: new Date(Date.now() - 190000), level: 'warn', message: 'Existing user model found, will extend instead' },
		{ id: '13', timestamp: new Date(Date.now() - 180000), level: 'info', message: 'Updating file: src/models/user.ts' },
		{ id: '14', timestamp: new Date(Date.now() - 170000), level: 'debug', message: 'Adding password hash field' },
		{ id: '15', timestamp: new Date(Date.now() - 160000), level: 'debug', message: 'Adding last login timestamp' },
		{ id: '16', timestamp: new Date(Date.now() - 150000), level: 'info', message: 'File updated: src/models/user.ts' },
		{ id: '17', timestamp: new Date(Date.now() - 120000), level: 'info', message: 'Starting subtask: Create auth routes' },
		{ id: '18', timestamp: new Date(Date.now() - 110000), level: 'error', message: 'TypeScript error in auth.ts:45 - Property "user" does not exist' },
		{ id: '19', timestamp: new Date(Date.now() - 100000), level: 'info', message: 'Fixing TypeScript error...' },
		{ id: '20', timestamp: new Date(Date.now() - 90000), level: 'info', message: 'Error resolved, continuing...' },
		{ id: '21', timestamp: new Date(Date.now() - 60000), level: 'info', message: 'Running tests...' },
		{ id: '22', timestamp: new Date(Date.now() - 30000), level: 'info', message: 'Tests passed: 12/12' },
		{ id: '23', timestamp: new Date(Date.now() - 10000), level: 'info', message: 'Committing changes...' }
	];

	// Filtered logs
	const filteredLogs = $derived(() => {
		let logs = mockLogs;

		// Filter by level
		if (levelFilter !== 'all') {
			logs = logs.filter(log => log.level === levelFilter);
		}

		// Filter by search query
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			logs = logs.filter(log => log.message.toLowerCase().includes(query));
		}

		return logs;
	});

	// Level colors
	const levelColors: Record<LogLevel, string> = {
		info: 'text-blue-400',
		warn: 'text-yellow-400',
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
			<option value="warn">Warn</option>
			<option value="error">Error</option>
			<option value="debug">Debug</option>
		</select>

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
		{#if filteredLogs().length === 0}
			<div class="flex items-center justify-center h-full text-zinc-500">
				{#if searchQuery || levelFilter !== 'all'}
					No logs match your filter
				{:else}
					No logs yet
				{/if}
			</div>
		{:else}
			<div class="space-y-1">
				{#each filteredLogs() as log (log.id)}
					<div class="flex items-start gap-2 hover:bg-zinc-800/50 px-2 py-0.5 rounded">
						{#if showTimestamps}
							<span class="text-zinc-500 flex-shrink-0">
								{formatTimestamp(log.timestamp)}
							</span>
						{/if}
						<span class="flex-shrink-0 w-12 text-right {levelColors[log.level]}">
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
		{#if autoScroll}
			<span class="text-primary">Auto-scrolling</span>
		{/if}
	</div>
</div>
