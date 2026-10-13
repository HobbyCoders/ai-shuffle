<script lang="ts">
	/**
	 * GitHubRepoPicker - searchable list of the connected GitHub account's
	 * repositories, used by the clone dialog to fill in the clone URL.
	 *
	 * Uses the server's existing GitHub sign-in (GET /github/repos). Search and
	 * paging run on the server's cached list; "Load more" appends the next page.
	 * When GitHub is not connected it shows where to connect it instead.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { ArrowUpRight, Github, Globe, Lock, RefreshCw, Search } from 'lucide-svelte';
	import { listGitHubRepos, type GitHubRepo } from '$lib/api/github';
	import { relativeTime } from '$lib/api/network-status';

	interface Props {
		/** The clone URL currently in the dialog; the matching repo shows as selected. */
		selectedUrl?: string;
		disabled?: boolean;
		onSelect: (repo: GitHubRepo) => void;
		/** Open the place where GitHub is connected (Settings > Integrations > GitHub). */
		onConnect?: () => void;
	}

	let { selectedUrl = '', disabled = false, onSelect, onConnect }: Props = $props();

	const PER_PAGE = 30;
	const SEARCH_DEBOUNCE_MS = 250;

	type Status = 'loading' | 'ready' | 'disconnected' | 'error';

	let status = $state<Status>('loading');
	let error = $state('');
	let moreError = $state('');
	let query = $state('');
	let repos = $state<GitHubRepo[]>([]);
	let total = $state(0);
	let page = $state(1);
	let hasMore = $state(false);
	let truncated = $state(false);
	let busy = $state(false); // a search or refresh while a list is shown
	let loadingMore = $state(false);

	let seq = 0;
	let controller: AbortController | null = null;
	let debounce: ReturnType<typeof setTimeout> | undefined;

	const selected = $derived(selectedUrl.trim());

	function message(e: unknown): string {
		const err = e as { detail?: unknown; message?: unknown } | null;
		if (typeof err?.detail === 'string' && err.detail) return err.detail;
		if (typeof err?.message === 'string' && err.message) return err.message;
		return 'Could not load your GitHub repositories';
	}

	async function load({ append = false, refresh = false } = {}) {
		const mine = ++seq;
		controller?.abort();
		controller = new AbortController();
		const nextPage = append ? page + 1 : 1;
		moreError = '';
		if (append) loadingMore = true;
		else if (status === 'ready') busy = true;
		else status = 'loading';
		try {
			const res = await listGitHubRepos(
				{ q: query, page: nextPage, perPage: PER_PAGE, refresh },
				{ signal: controller.signal }
			);
			if (mine !== seq) return;
			if (!res.connected) {
				repos = [];
				status = 'disconnected';
				return;
			}
			repos = append ? [...repos, ...res.items] : res.items;
			page = res.page;
			total = res.total;
			hasMore = res.has_more;
			truncated = res.truncated;
			status = 'ready';
		} catch (e) {
			if (mine !== seq || (e as { name?: string } | null)?.name === 'AbortError') return;
			if (append) {
				moreError = message(e);
			} else {
				error = message(e);
				status = 'error';
			}
		} finally {
			if (mine === seq) {
				busy = false;
				loadingMore = false;
			}
		}
	}

	function onSearchInput() {
		clearTimeout(debounce);
		debounce = setTimeout(() => load(), SEARCH_DEBOUNCE_MS);
	}

	function updatedLabel(iso: string | null): string {
		if (!iso) return '';
		const ms = new Date(iso).getTime();
		if (!Number.isFinite(ms)) return '';
		if (Date.now() - ms < 30 * 86_400_000) return `Updated ${relativeTime(iso)}`;
		return `Updated ${new Date(ms).toLocaleDateString(undefined, { month: 'short', year: 'numeric' })}`;
	}

	onMount(() => {
		load();
	});

	onDestroy(() => {
		clearTimeout(debounce);
		seq++;
		controller?.abort();
	});
</script>

<div class="picker" data-repo-picker>
	{#if status === 'disconnected'}
		<div class="connect-hint">
			<Github size={16} strokeWidth={1.5} />
			<span>Connect GitHub to choose from your own and your organizations' repositories.</span>
			{#if onConnect}
				<button type="button" class="link-btn" onclick={onConnect} {disabled}>
					Connect GitHub
					<ArrowUpRight size={13} strokeWidth={1.5} />
				</button>
			{/if}
		</div>
	{:else}
		<div class="picker-head">
			<span class="picker-label" id="gh-repo-picker-label">
				<Github size={14} strokeWidth={1.5} />
				Your GitHub repositories
			</span>
			<button
				type="button"
				class="icon-btn"
				aria-label="Refresh repositories"
				title="Refresh"
				onclick={() => load({ refresh: true })}
				disabled={disabled || status === 'loading' || busy}
			>
				<span class="refresh-icon" class:spinning={busy}>
					<RefreshCw size={13} strokeWidth={1.5} />
				</span>
			</button>
		</div>

		<div class="search">
			<Search size={14} strokeWidth={1.5} />
			<input
				type="search"
				class="search-input"
				placeholder="Search repositories"
				aria-label="Search repositories"
				autocomplete="off"
				spellcheck="false"
				bind:value={query}
				oninput={onSearchInput}
				disabled={disabled || status === 'loading'}
			/>
		</div>

		{#if status === 'loading'}
			<div class="state" role="status">Loading your repositories...</div>
		{:else if status === 'error'}
			<div class="state state-error">
				<span>{error}</span>
				<button type="button" class="link-btn" onclick={() => load()} {disabled}>Retry</button>
			</div>
		{:else if repos.length === 0}
			<div class="state" role="status">
				{query.trim() ? `No repositories match "${query.trim()}"` : 'No repositories on this GitHub account yet'}
			</div>
		{:else}
			<ul class="repo-list" aria-labelledby="gh-repo-picker-label" aria-busy={busy}>
				{#each repos as repo (repo.full_name)}
					{@const isSelected = selected === repo.clone_url}
					<li>
						<button
							type="button"
							class="repo"
							class:selected={isSelected}
							aria-pressed={isSelected}
							aria-label={`${repo.full_name}, ${repo.private ? 'private' : 'public'}${repo.updated_at ? `, ${updatedLabel(repo.updated_at).toLowerCase()}` : ''}`}
							title={repo.description || repo.full_name}
							onclick={() => !disabled && onSelect(repo)}
							{disabled}
						>
							<span class="repo-name">
								<span class="repo-owner">{repo.owner}/</span>{repo.name}
							</span>
							<span class="repo-meta">
								<span class="badge" class:badge-private={repo.private}>
									{#if repo.private}
										<Lock size={10} strokeWidth={2} />
										Private
									{:else}
										<Globe size={10} strokeWidth={2} />
										Public
									{/if}
								</span>
								{#if repo.fork}<span class="badge">Fork</span>{/if}
								{#if repo.archived}<span class="badge">Archived</span>{/if}
								{#if repo.updated_at}<span class="updated">{updatedLabel(repo.updated_at)}</span>{/if}
							</span>
						</button>
					</li>
				{/each}
			</ul>
			<div class="list-foot">
				<span>{repos.length} of {total}</span>
				{#if hasMore}
					<button type="button" class="link-btn" onclick={() => load({ append: true })} disabled={disabled || loadingMore || busy}>
						{loadingMore ? 'Loading...' : 'Load more'}
					</button>
				{/if}
			</div>
			{#if moreError}
				<div class="state state-error">{moreError}</div>
			{/if}
			{#if truncated}
				<p class="note">Showing your 1,000 most recently pushed repositories. Paste a URL above for older ones.</p>
			{/if}
		{/if}
	{/if}
</div>

<style>
	.picker {
		display: flex;
		flex-direction: column;
		gap: 8px;
		min-width: 0;
	}

	.picker-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
	}

	.picker-label {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		font-size: 0.8125rem;
		font-weight: 500;
		color: var(--foreground);
	}

	.icon-btn {
		width: 28px;
		height: 28px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border: none;
		border-radius: 6px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
	}

	.icon-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}

	.icon-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.refresh-icon {
		display: inline-flex;
	}

	.refresh-icon.spinning {
		animation: spin 900ms linear infinite;
	}

	@keyframes spin {
		to { transform: rotate(360deg); }
	}

	.search {
		display: flex;
		align-items: center;
		gap: 8px;
		padding: 0 12px;
		background: var(--card);
		border: 1px solid var(--border);
		border-radius: 10px;
		color: var(--muted-foreground);
		transition: border-color 150ms ease, box-shadow 150ms ease;
	}

	.search:focus-within {
		border-color: var(--ring);
		box-shadow: 0 0 0 3px color-mix(in oklch, var(--ring) 15%, transparent);
	}

	.search-input {
		flex: 1;
		min-width: 0;
		padding: 9px 0;
		background: transparent;
		border: none;
		outline: none;
		color: var(--foreground);
		font-size: 0.875rem;
	}

	.search-input:disabled {
		opacity: 0.5;
	}

	.repo-list {
		list-style: none;
		margin: 0;
		padding: 4px;
		max-height: 232px;
		overflow-y: auto;
		overscroll-behavior: contain;
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		display: flex;
		flex-direction: column;
		gap: 2px;
	}

	.repo-list[aria-busy='true'] {
		opacity: 0.6;
	}

	.repo {
		width: 100%;
		display: flex;
		flex-direction: column;
		align-items: flex-start;
		gap: 3px;
		padding: 7px 10px;
		text-align: left;
		background: transparent;
		border: 1px solid transparent;
		border-radius: 8px;
		color: var(--foreground);
		cursor: pointer;
		transition: background 120ms ease, border-color 120ms ease;
	}

	.repo:hover:not(:disabled) {
		background: var(--hover-overlay);
	}

	.repo.selected {
		background: color-mix(in oklch, var(--primary) 10%, transparent);
		border-color: color-mix(in oklch, var(--primary) 45%, transparent);
	}

	.repo:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.repo-name {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
		font-size: 0.8125rem;
		font-weight: 500;
	}

	.repo-owner {
		color: var(--muted-foreground);
		font-weight: 400;
	}

	.repo-meta {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.badge {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		padding: 1px 6px;
		border: 1px solid var(--border);
		border-radius: 999px;
		line-height: 1.4;
	}

	.badge-private {
		color: var(--foreground);
	}

	.state {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		padding: 12px;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		background: var(--background);
		border: 1px dashed var(--border);
		border-radius: 10px;
		line-height: 1.4;
	}

	.state-error {
		color: var(--destructive);
		border-style: solid;
		border-color: color-mix(in oklch, var(--destructive) 25%, transparent);
		background: color-mix(in oklch, var(--destructive) 6%, transparent);
	}

	.list-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		font-size: 0.75rem;
		color: var(--muted-foreground);
	}

	.link-btn {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		flex-shrink: 0;
		padding: 4px 8px;
		font-size: 0.75rem;
		font-weight: 500;
		background: transparent;
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--foreground);
		cursor: pointer;
	}

	.link-btn:hover:not(:disabled) {
		border-color: var(--foreground);
	}

	.link-btn:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}

	.note {
		margin: 0;
		font-size: 0.75rem;
		color: var(--muted-foreground);
		line-height: 1.4;
	}

	.connect-hint {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 8px;
		padding: 10px 12px;
		font-size: 0.8125rem;
		color: var(--muted-foreground);
		background: var(--background);
		border: 1px solid var(--border);
		border-radius: 10px;
		line-height: 1.4;
	}

	.connect-hint span {
		flex: 1;
		min-width: 160px;
	}
</style>
