<script lang="ts">
	/**
	 * AgentLogs - Dedicated log viewer with virtual scrolling
	 *
	 * Features:
	 * - Virtual scroll for performance (handles 1000s of lines)
	 * - Syntax highlighting for code blocks
	 * - Timestamp toggle
	 * - Log level filtering (info, warn, error)
	 * - Search within logs
	 * - Copy all / Copy selection
	 * - Auto-scroll toggle
	 */

	interface Props {
		logs: string[];
		agentId: string;
		maxHeight?: string;
		showControls?: boolean;
	}

	let {
		logs,
		agentId,
		maxHeight = '400px',
		showControls = true
	}: Props = $props();

	// UI State
	let containerRef: HTMLDivElement | undefined = $state();
	let autoScroll = $state(true);
	let showTimestamps = $state(false);
	let searchQuery = $state('');
	let logLevelFilter = $state<'all' | 'info' | 'warn' | 'error'>('all');
	let copySuccess = $state(false);

	// Virtual scroll state
	const LINE_HEIGHT = 20;
	const BUFFER_SIZE = 10;
	let scrollTop = $state(0);
	let containerHeight = $state(400);

	// Parse log entry for level and content
	function parseLogEntry(line: string): { level: 'info' | 'warn' | 'error'; timestamp?: string; content: string } {
		// Check for common log patterns
		const errorPatterns = /\[error\]|\berror\b:|^error:|failed|exception|traceback/i;
		const warnPatterns = /\[warn(ing)?\]|\bwarn(ing)?\b:|^warn(ing)?:/i;
		const timestampPattern = /^\[(\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\]\s*/;

		let content = line;
		let timestamp: string | undefined;

		// Extract timestamp if present
		const tsMatch = line.match(timestampPattern);
		if (tsMatch) {
			timestamp = tsMatch[1];
			content = line.slice(tsMatch[0].length);
		}

		// Determine level
		let level: 'info' | 'warn' | 'error' = 'info';
		if (errorPatterns.test(line)) {
			level = 'error';
		} else if (warnPatterns.test(line)) {
			level = 'warn';
		}

		return { level, timestamp, content };
	}

	// Get color class for log level
	function getLevelColor(level: 'info' | 'warn' | 'error'): string {
		switch (level) {
			case 'error':
				return 'text-red-400';
			case 'warn':
				return 'text-amber-400';
			default:
				return 'text-gray-300';
		}
	}

	// Filter logs based on search and level
	const filteredLogs = $derived.by(() => {
		let result = logs.map((line, idx) => ({
			index: idx,
			...parseLogEntry(line),
			raw: line
		}));

		// Filter by level
		if (logLevelFilter !== 'all') {
			result = result.filter(log => log.level === logLevelFilter);
		}

		// Filter by search
		if (searchQuery.trim()) {
			const query = searchQuery.toLowerCase();
			result = result.filter(log => log.raw.toLowerCase().includes(query));
		}

		return result;
	});

	// Virtual scroll calculations
	const visibleRange = $derived.by(() => {
		const totalHeight = filteredLogs.length * LINE_HEIGHT;
		const startIndex = Math.max(0, Math.floor(scrollTop / LINE_HEIGHT) - BUFFER_SIZE);
		const endIndex = Math.min(
			filteredLogs.length,
			Math.ceil((scrollTop + containerHeight) / LINE_HEIGHT) + BUFFER_SIZE
		);

		return {
			startIndex,
			endIndex,
			totalHeight,
			offsetTop: startIndex * LINE_HEIGHT
		};
	});

	// Get visible log entries
	const visibleLogs = $derived(
		filteredLogs.slice(visibleRange.startIndex, visibleRange.endIndex)
	);

	// Handle scroll
	function handleScroll(event: Event) {
		const target = event.target as HTMLDivElement;
		scrollTop = target.scrollTop;

		// Disable auto-scroll if user scrolled up
		const isAtBottom = target.scrollHeight - target.scrollTop - target.clientHeight < 50;
		if (!isAtBottom && autoScroll) {
			autoScroll = false;
		}
	}

	// Auto-scroll effect
	$effect(() => {
		if (autoScroll && containerRef && logs.length > 0) {
			containerRef.scrollTop = containerRef.scrollHeight;
		}
	});

	// Update container height on resize
	$effect(() => {
		if (containerRef) {
			const observer = new ResizeObserver(entries => {
				for (const entry of entries) {
					containerHeight = entry.contentRect.height;
				}
			});
			observer.observe(containerRef);

			return () => observer.disconnect();
		}
	});

	// Copy logs to clipboard
	async function copyLogs() {
		const text = filteredLogs.map(l => l.raw).join('\n');
		try {
			await navigator.clipboard.writeText(text);
			copySuccess = true;
			setTimeout(() => copySuccess = false, 2000);
		} catch (e) {
			console.error('Failed to copy logs:', e);
		}
	}

	// Clear search
	function clearSearch() {
		searchQuery = '';
	}

	// Highlight search matches
	function highlightMatch(text: string, query: string): string {
		if (!query.trim()) return text;
		const regex = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'gi');
		return text.replace(regex, '<mark class="bg-yellow-500/30 text-yellow-200">$1</mark>');
	}

	// Detect code blocks
	function isCodeLine(content: string): boolean {
		return /^\s{2,}|^\t|^[│├└─]/.test(content) ||
			/^(const|let|var|function|class|import|export|if|else|for|while|return)\b/.test(content) ||
			/^[<{[\]]/.test(content.trim());
	}
</script>

<div class="agent-logs flex flex-col h-full">
	<!-- Controls -->
	{#if showControls}
		<div class="controls flex flex-wrap items-center gap-2 px-3 py-2 bg-[#1a1a2e] border-b border-white/5">
			<!-- Search -->
			<div class="relative flex-1 min-w-[150px] max-w-[250px]">
				<input
					type="text"
					bind:value={searchQuery}
					placeholder="Search logs..."
					class="w-full bg-black/30 border border-white/10 rounded px-3 py-1.5 text-xs text-gray-300 placeholder-gray-500 focus:outline-none focus:border-cyan-500/50"
				/>
				{#if searchQuery}
					<button
						type="button"
						onclick={clearSearch}
						class="absolute right-2 top-1/2 -translate-y-1/2 text-gray-500 hover:text-gray-300"
						aria-label="Clear search"
					>
						<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
							<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
						</svg>
					</button>
				{/if}
			</div>

			<!-- Level filter -->
			<select
				bind:value={logLevelFilter}
				class="bg-black/30 border border-white/10 rounded px-2 py-1.5 text-xs text-gray-300 focus:outline-none focus:border-cyan-500/50"
			>
				<option value="all">All levels</option>
				<option value="info">Info</option>
				<option value="warn">Warnings</option>
				<option value="error">Errors</option>
			</select>

			<!-- Spacer -->
			<div class="flex-1"></div>

			<!-- Timestamp toggle -->
			<button
				type="button"
				onclick={() => showTimestamps = !showTimestamps}
				class="flex items-center gap-1 px-2 py-1.5 text-xs rounded transition-colors {showTimestamps ? 'bg-cyan-500/20 text-cyan-400' : 'text-gray-400 hover:text-gray-200 hover:bg-white/5'}"
				title="Toggle timestamps"
			>
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
				</svg>
			</button>

			<!-- Auto-scroll toggle -->
			<button
				type="button"
				onclick={() => autoScroll = !autoScroll}
				class="flex items-center gap-1 px-2 py-1.5 text-xs rounded transition-colors {autoScroll ? 'bg-cyan-500/20 text-cyan-400' : 'text-gray-400 hover:text-gray-200 hover:bg-white/5'}"
				title="Auto-scroll"
			>
				<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 14l-7 7m0 0l-7-7m7 7V3" />
				</svg>
			</button>

			<!-- Copy button -->
			<button
				type="button"
				onclick={copyLogs}
				class="flex items-center gap-1 px-2 py-1.5 text-xs rounded transition-colors {copySuccess ? 'bg-emerald-500/20 text-emerald-400' : 'text-gray-400 hover:text-gray-200 hover:bg-white/5'}"
				title="Copy all logs"
			>
				{#if copySuccess}
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M5 13l4 4L19 7" />
					</svg>
				{:else}
					<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
						<path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
					</svg>
				{/if}
			</button>

			<!-- Log count -->
			<span class="text-xs text-gray-500">
				{filteredLogs.length} / {logs.length}
			</span>
		</div>
	{/if}

	<!-- Log content with virtual scroll -->
	<div
		bind:this={containerRef}
		onscroll={handleScroll}
		class="log-content flex-1 overflow-y-auto bg-[#0d0d1a] font-mono text-xs"
		style="max-height: {maxHeight};"
	>
		{#if filteredLogs.length === 0}
			<div class="flex items-center justify-center h-full text-gray-500 py-8">
				{#if logs.length === 0}
					Waiting for logs...
				{:else}
					No logs match your filter
				{/if}
			</div>
		{:else}
			<!-- Virtual scroll container -->
			<div style="height: {visibleRange.totalHeight}px; position: relative;">
				<div style="transform: translateY({visibleRange.offsetTop}px);">
					{#each visibleLogs as log (log.index)}
						<div
							class="log-line flex items-start px-3 py-0.5 hover:bg-white/5 {getLevelColor(log.level)}"
							style="height: {LINE_HEIGHT}px;"
						>
							<!-- Line number -->
							<span class="line-number text-gray-600 select-none mr-3 min-w-[3rem] text-right">
								{log.index + 1}
							</span>

							<!-- Timestamp -->
							{#if showTimestamps && log.timestamp}
								<span class="timestamp text-gray-500 mr-2">
									[{log.timestamp}]
								</span>
							{/if}

							<!-- Level indicator -->
							{#if log.level !== 'info'}
								<span class="level-badge mr-2 px-1 rounded text-[10px] font-bold uppercase {log.level === 'error' ? 'bg-red-500/20 text-red-400' : 'bg-amber-500/20 text-amber-400'}">
									{log.level}
								</span>
							{/if}

							<!-- Content -->
							<span
								class="log-text flex-1 whitespace-pre-wrap break-all {isCodeLine(log.content) ? 'text-violet-300' : ''}"
							>
								{#if searchQuery}
									{@html highlightMatch(log.content, searchQuery)}
								{:else}
									{log.content}
								{/if}
							</span>
						</div>
					{/each}
				</div>
			</div>
		{/if}
	</div>

	<!-- Status bar -->
	<div class="status-bar flex items-center justify-between px-3 py-1.5 bg-[#1a1a2e] border-t border-white/5 text-[10px] text-gray-500">
		<span>Agent: {agentId}</span>
		<span>
			{autoScroll ? 'Auto-scrolling' : 'Scroll paused'}
		</span>
	</div>
</div>

<style>
	.agent-logs {
		background: #0d0d1a;
		border-radius: 0.5rem;
		overflow: hidden;
	}

	.log-content {
		scrollbar-width: thin;
		scrollbar-color: rgba(255, 255, 255, 0.1) transparent;
	}

	.log-content::-webkit-scrollbar {
		width: 6px;
	}

	.log-content::-webkit-scrollbar-track {
		background: transparent;
	}

	.log-content::-webkit-scrollbar-thumb {
		background: rgba(255, 255, 255, 0.1);
		border-radius: 3px;
	}

	.log-content::-webkit-scrollbar-thumb:hover {
		background: rgba(255, 255, 255, 0.2);
	}

	.log-line {
		line-height: 20px;
	}

	.line-number {
		font-variant-numeric: tabular-nums;
	}
</style>
