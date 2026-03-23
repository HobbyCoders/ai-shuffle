<script lang="ts">
	/**
	 * DiffViewer - Unified diff viewer for a single file
	 *
	 * Lazily fetches and displays the unified diff for a file when mounted.
	 * Renders line-by-line with two-column gutter (old/new line numbers),
	 * color-coded additions/deletions, and hunk separators.
	 */
	import { ArrowLeft, FileText, Loader2 } from 'lucide-svelte';
	import { git } from '$lib/stores/git';
	import * as gitApi from '$lib/api/git';
	import { parseDiff, getDiffDescription, countChanges } from '$lib/utils/diff-parser';
	import type { ParsedDiff } from '$lib/utils/diff-parser';

	interface Props {
		projectId: string;
		file: gitApi.FileChangeInfo;
		onClose: () => void;
	}

	let { projectId, file, onClose }: Props = $props();

	let diffResponse = $state<gitApi.FileDiffResponse | null>(null);
	let parsedDiff = $state<ParsedDiff | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);

	const statusLabel = $derived(getStatusLabel(file.status));
	const statusColor = $derived(getStatusColor(file.status));

	const totalChanges = $derived(() => {
		if (parsedDiff) {
			return countChanges(parsedDiff);
		}
		return { additions: file.additions, deletions: file.deletions };
	});

	function getStatusLabel(status: string): string {
		switch (status) {
			case 'A': return 'Added';
			case 'M': return 'Modified';
			case 'D': return 'Deleted';
			case 'R': return 'Renamed';
			case '?': return 'New File';
			case 'U': return 'Conflict';
			default: return status;
		}
	}

	function getStatusColor(status: string): string {
		switch (status) {
			case 'A': return 'bg-emerald-500/20 text-emerald-400 ring-emerald-500/30';
			case 'M': return 'bg-amber-500/20 text-amber-400 ring-amber-500/30';
			case 'D': return 'bg-red-500/20 text-red-400 ring-red-500/30';
			case 'R': return 'bg-violet-500/20 text-violet-400 ring-violet-500/30';
			case '?': return 'bg-sky-500/20 text-sky-400 ring-sky-500/30';
			case 'U': return 'bg-orange-500/20 text-orange-400 ring-orange-500/30';
			default: return 'bg-zinc-500/20 text-zinc-400 ring-zinc-500/30';
		}
	}

	$effect(() => {
		const f = file; // track
		loading = true;
		error = null;
		diffResponse = null;
		parsedDiff = null;

		const worktreeId = git.current.worktreeId ?? undefined;

		gitApi.getFileDiff(projectId, f.file, f.category === 'staged', worktreeId)
			.then(resp => {
				diffResponse = resp;
				if (!resp.is_binary && resp.diff) {
					parsedDiff = parseDiff(resp.diff);
				}
				loading = false;
			})
			.catch(e => {
				error = e?.detail || e?.message || 'Failed to load diff';
				loading = false;
			});
	});
</script>

<div class="flex flex-col h-full bg-zinc-900 text-zinc-100">
	<!-- Sticky Header -->
	<header class="sticky top-0 z-10 flex items-center gap-3 px-4 py-3 bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 min-h-[52px]">
		<button
			onclick={onClose}
			class="flex items-center justify-center w-8 h-8 rounded-md text-zinc-400 hover:text-zinc-100 hover:bg-zinc-800 transition-colors shrink-0 focus:outline-none focus:ring-2 focus:ring-zinc-500"
			aria-label="Back to file list"
		>
			<ArrowLeft size={18} />
		</button>

		<FileText size={16} class="text-zinc-500 shrink-0" />

		<span class="font-mono text-sm text-zinc-200 truncate min-w-0" title={file.file}>
			{file.file}
		</span>

		<span class="inline-flex items-center px-2 py-0.5 text-xs font-medium rounded ring-1 ring-inset shrink-0 {statusColor}">
			{statusLabel}
		</span>

		{#if totalChanges().additions > 0 || totalChanges().deletions > 0}
			<span class="text-xs font-mono shrink-0 ml-auto flex items-center gap-1.5">
				{#if totalChanges().additions > 0}
					<span class="text-green-400">+{totalChanges().additions}</span>
				{/if}
				{#if totalChanges().deletions > 0}
					<span class="text-red-400">-{totalChanges().deletions}</span>
				{/if}
			</span>
		{/if}
	</header>

	<!-- Content Area -->
	<div class="flex-1 overflow-y-auto overflow-x-hidden">
		{#if loading}
			<!-- Loading State -->
			<div class="flex flex-col items-center justify-center py-20 gap-3">
				<Loader2 size={24} class="text-zinc-500 animate-spin" />
				<span class="text-sm text-zinc-500">Loading diff...</span>
			</div>
		{:else if error}
			<!-- Error State -->
			<div class="flex flex-col items-center justify-center py-20 gap-2 px-4">
				<div class="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-lg px-4 py-3 max-w-md text-center">
					{error}
				</div>
			</div>
		{:else if diffResponse?.is_binary}
			<!-- Binary File State -->
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-zinc-500">Binary file — cannot display diff</span>
			</div>
		{:else if !parsedDiff || parsedDiff.hunks.length === 0}
			<!-- Empty Diff State -->
			<div class="flex flex-col items-center justify-center py-20 gap-2">
				<span class="text-sm text-zinc-500">No changes to display</span>
			</div>
		{:else}
			<!-- Diff Content -->
			<div class="diff-content">
				{#each parsedDiff.hunks as hunk, hunkIndex}
					{#if hunkIndex > 0}
						<!-- Hunk separator -->
						<div class="flex items-center gap-2 px-4 py-1.5 bg-zinc-950/50 border-y border-zinc-800/50">
							<div class="flex-1 border-t border-zinc-800/60 border-dashed"></div>
							<span class="text-[10px] text-zinc-600 font-mono select-none tracking-wider">···</span>
							<div class="flex-1 border-t border-zinc-800/60 border-dashed"></div>
						</div>
					{/if}

					<div class="hunk">
						{#each hunk.lines as line}
							{#if line.type === 'header'}
								<div class="flex bg-blue-900/15 border-b border-blue-500/10">
									<div class="gutter-col bg-blue-950/30"></div>
									<div class="gutter-col bg-blue-950/30"></div>
									<div class="flex-1 px-3 py-1 font-mono text-xs text-blue-400 whitespace-pre select-none overflow-x-auto">
										{line.content}
									</div>
								</div>
							{:else if line.type === 'addition'}
								<div class="flex bg-green-900/15 hover:bg-green-900/25 transition-colors duration-75">
									<div class="gutter-col text-zinc-700"></div>
									<div class="gutter-col text-green-700">{line.newLineNumber}</div>
									<div class="flex-1 px-3 font-mono text-sm text-green-300 whitespace-pre overflow-x-auto">
										<span class="text-green-600 select-none mr-1">+</span>{line.content}
									</div>
								</div>
							{:else if line.type === 'deletion'}
								<div class="flex bg-red-900/15 hover:bg-red-900/25 transition-colors duration-75">
									<div class="gutter-col text-red-700">{line.oldLineNumber}</div>
									<div class="gutter-col text-zinc-700"></div>
									<div class="flex-1 px-3 font-mono text-sm text-red-300 whitespace-pre overflow-x-auto">
										<span class="text-red-600 select-none mr-1">-</span>{line.content}
									</div>
								</div>
							{:else}
								<!-- Context line -->
								<div class="flex hover:bg-zinc-800/30 transition-colors duration-75">
									<div class="gutter-col text-zinc-700">{line.oldLineNumber}</div>
									<div class="gutter-col text-zinc-700">{line.newLineNumber}</div>
									<div class="flex-1 px-3 font-mono text-sm text-zinc-400 whitespace-pre overflow-x-auto">
										<span class="text-zinc-700 select-none mr-1">&nbsp;</span>{line.content}
									</div>
								</div>
							{/if}
						{/each}
					</div>
				{/each}
			</div>

			<!-- Truncation Notice -->
			{#if diffResponse?.truncated}
				<div class="sticky bottom-0 flex items-center justify-center gap-2 px-4 py-2.5 bg-amber-500/10 border-t border-amber-500/20 text-amber-400 text-xs font-medium">
					Diff output was truncated — only showing partial changes
				</div>
			{/if}
		{/if}
	</div>
</div>

<style>
	.gutter-col {
		width: 3.5rem;
		min-width: 3.5rem;
		padding: 0 0.5rem;
		text-align: right;
		font-family: ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;
		font-size: 0.75rem;
		line-height: 1.5rem;
		user-select: none;
		color: inherit;
		border-right: 1px solid rgb(39, 39, 42, 0.5);
	}

	.diff-content :global(.overflow-x-auto) {
		scrollbar-width: thin;
		scrollbar-color: rgb(63, 63, 70) transparent;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar) {
		height: 4px;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar-track) {
		background: transparent;
	}

	.diff-content :global(.overflow-x-auto::-webkit-scrollbar-thumb) {
		background: rgb(63, 63, 70);
		border-radius: 2px;
	}
</style>
