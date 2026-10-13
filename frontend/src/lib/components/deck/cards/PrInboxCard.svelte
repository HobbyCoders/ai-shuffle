<script lang="ts">
	/**
	 * PrInboxCard — pull requests across every project, in one list.
	 *
	 * The server resolves each project's GitHub origin, folds projects that
	 * share a repository, and lists each repository's pull requests with their
	 * review and check state. State is a server filter (gh's --state); Review,
	 * Checks and Project filter the loaded list here. Projects the server could
	 * not read are listed under the results, with why.
	 */
	import { onMount } from 'svelte';
	import { GitPullRequest, GitPullRequestDraft, GitMerge, GitPullRequestClosed, RefreshCw, Loader2, Check, X, Clock, ExternalLink } from 'lucide-svelte';
	import StudioPageHeader from '$lib/components/layout/StudioPageHeader.svelte';
	import { getPullRequestInbox, type InboxPullRequest, type PullRequestInbox } from '$lib/api/github';
	import {
		activityAge,
		CHECKS_LABELS,
		DEFAULT_FILTERS,
		filterInbox,
		inboxProjects,
		REVIEW_LABELS,
		STATE_OPTIONS,
		type InboxFilters,
		type ServerState
	} from './pr-inbox';

	let serverState = $state<ServerState>('open');
	let filters = $state<InboxFilters>({ ...DEFAULT_FILTERS });
	let inbox = $state<PullRequestInbox | null>(null);
	let loading = $state(true);
	let error = $state<string | null>(null);
	let now = $state(Date.now());
	let request = 0;

	const projects = $derived(inbox ? inboxProjects(inbox.items) : []);
	const shown = $derived(inbox ? filterInbox(inbox.items, filters) : []);
	const filtered = $derived(
		filters.review !== 'any' || filters.checks !== 'any' || filters.project !== 'any' || filters.hideDrafts
	);

	async function load() {
		const mine = ++request;
		loading = true;
		error = null;
		try {
			const next = await getPullRequestInbox(serverState);
			if (mine !== request) return;
			inbox = next;
			now = Date.now();
			if (filters.project !== 'any' && !next.items.some((pr) => pr.projects.some((p) => p.id === filters.project))) {
				filters.project = 'any';
			}
		} catch (e) {
			if (mine !== request) return;
			const err = e as { status?: number; detail?: string; message?: string };
			error = err.status === 401
				? 'GitHub CLI is not signed in on this server. Run gh auth login there, then refresh.'
				: err.detail || err.message || 'Could not load pull requests';
		} finally {
			if (mine === request) loading = false;
		}
	}

	function setServerState(next: ServerState) {
		if (next === serverState) return;
		serverState = next;
		void load();
	}

	function clearFilters() {
		filters = { ...DEFAULT_FILTERS };
	}

	function stateLabel(pr: InboxPullRequest): string {
		return pr.state === 'draft' ? 'Draft' : pr.state === 'merged' ? 'Merged' : pr.state === 'closed' ? 'Closed' : 'Open';
	}

	onMount(() => {
		void load();
	});
</script>

<div class="pri">
	<div class="pri-wrap">
		<div class="pri-head">
			<StudioPageHeader eyebrow="Across every project" title="Pull" accent="requests">
				{#snippet actions()}
					<div class="pri-seg" role="group" aria-label="Pull request state">
						{#each STATE_OPTIONS as option (option.value)}
							<button
								type="button"
								class="pri-seg-btn"
								class:on={serverState === option.value}
								aria-pressed={serverState === option.value}
								onclick={() => setServerState(option.value)}
							>{option.label}</button>
						{/each}
					</div>
					<button type="button" class="pri-btn" onclick={() => load()} disabled={loading} title="Refresh" aria-label="Refresh pull requests">
						<RefreshCw size={15} strokeWidth={1.9} class={loading ? 'pri-spin' : ''} />
					</button>
				{/snippet}
			</StudioPageHeader>

			<div class="pri-filters" role="group" aria-label="Filter pull requests">
				<label>
					<span>Review</span>
					<select bind:value={filters.review} aria-label="Review">
						<option value="any">Any</option>
						{#each Object.entries(REVIEW_LABELS) as [value, label] (value)}
							<option {value}>{label}</option>
						{/each}
					</select>
				</label>
				<label>
					<span>Checks</span>
					<select bind:value={filters.checks} aria-label="Checks">
						<option value="any">Any</option>
						{#each Object.entries(CHECKS_LABELS) as [value, label] (value)}
							<option {value}>{label}</option>
						{/each}
					</select>
				</label>
				<label>
					<span>Project</span>
					<select bind:value={filters.project} aria-label="Project">
						<option value="any">All projects</option>
						{#each projects as project (project.id)}
							<option value={project.id}>{project.name}</option>
						{/each}
					</select>
				</label>
				{#if serverState === 'open' || serverState === 'all'}
					<label class="pri-check">
						<input type="checkbox" bind:checked={filters.hideDrafts} />
						<span>Hide drafts</span>
					</label>
				{/if}
				{#if filtered}
					<button type="button" class="pri-clear" onclick={clearFilters}>Clear filters</button>
				{/if}
			</div>
		</div>

		<div class="pri-scroll">
			{#if error}
				<div class="pri-error" role="alert">{error}</div>
			{:else if loading && !inbox}
				<div class="pri-empty"><Loader2 size={22} class="pri-spin" /> Loading pull requests…</div>
			{:else if inbox}
				{#if inbox.items.length === 0}
					<div class="pri-empty">
						{inbox.repos.length === 0 ? 'No project has a GitHub remote yet.' : `No ${serverState === 'all' ? '' : `${serverState} `}pull requests.`}
					</div>
				{:else if shown.length === 0}
					<div class="pri-empty">
						No pull requests match these filters.
						<button type="button" class="pri-clear" onclick={clearFilters}>Clear filters</button>
					</div>
				{:else}
					<ul class="pri-list" aria-label="Pull requests">
						{#each shown as pr (`${pr.repo}#${pr.number}`)}
							<li class="pri-row" data-state={pr.state}>
								<span class="pri-state" title={stateLabel(pr)} aria-label={stateLabel(pr)}>
									{#if pr.state === 'draft'}<GitPullRequestDraft size={16} />
									{:else if pr.state === 'merged'}<GitMerge size={16} />
									{:else if pr.state === 'closed'}<GitPullRequestClosed size={16} />
									{:else}<GitPullRequest size={16} />{/if}
								</span>
								<div class="pri-main">
									<a class="pri-title" href={pr.url} target="_blank" rel="noopener noreferrer">
										{pr.title}<ExternalLink size={11} aria-hidden="true" />
									</a>
									<div class="pri-meta">
										<span class="pri-repo">{pr.repo}#{pr.number}</span>
										<span class="pri-branch" title="{pr.head_branch} → {pr.base_branch}">{pr.head_branch} → {pr.base_branch}</span>
										<span>{pr.author}</span>
										<span class="pri-projects" title={pr.projects.map((p) => p.name).join(', ')}>{pr.projects.map((p) => p.name).join(', ')}</span>
									</div>
								</div>
								<div class="pri-badges">
									{#if pr.review !== 'none'}
										<span class="pri-chip" data-review={pr.review}>{REVIEW_LABELS[pr.review]}</span>
									{/if}
									{#if pr.checks !== 'none'}
										<span class="pri-checks" data-checks={pr.checks} title={CHECKS_LABELS[pr.checks]} aria-label={CHECKS_LABELS[pr.checks]}>
											{#if pr.checks === 'passing'}<Check size={14} strokeWidth={2.4} />
											{:else if pr.checks === 'failing'}<X size={14} strokeWidth={2.4} />
											{:else}<Clock size={14} strokeWidth={2.2} />{/if}
										</span>
									{/if}
									<span class="pri-age" title={new Date(pr.updated_at).toLocaleString()}>{activityAge(pr.updated_at, now)}</span>
								</div>
							</li>
						{/each}
					</ul>
				{/if}

				{#if inbox.skipped.length > 0}
					<details class="pri-skipped">
						<summary>{inbox.skipped.length} {inbox.skipped.length === 1 ? 'project or repository' : 'projects or repositories'} not included</summary>
						<ul>
							{#each inbox.skipped as skip, i (i)}
								<li><strong>{skip.project_name ?? skip.repo ?? 'Unknown'}</strong> — {skip.reason}</li>
							{/each}
						</ul>
					</details>
				{/if}
			{/if}
		</div>
	</div>
</div>

<style>
	.pri {
		display: flex;
		flex-direction: column;
		height: 100%;
		min-height: 0;
		overflow: hidden;
		background: var(--background);
	}
	.pri-wrap {
		width: 100%;
		max-width: 1080px;
		margin: 0 auto;
		flex: 1;
		min-height: 0;
		display: flex;
		flex-direction: column;
	}
	.pri-head { flex-shrink: 0; padding: 22px 28px 0; }
	.pri-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 28px 28px; }

	.pri-seg { display: inline-flex; padding: 3px; gap: 2px; background: var(--muted); border-radius: calc(var(--radius, 0.75rem) - 2px); }
	.pri-seg-btn {
		height: 28px; padding: 0 11px; border: 0; border-radius: calc(var(--radius, 0.75rem) - 4px);
		background: transparent; color: var(--muted-foreground); font-size: 0.8125rem; cursor: pointer;
	}
	.pri-seg-btn:hover { color: var(--foreground); }
	.pri-seg-btn.on { background: var(--card); color: var(--foreground); box-shadow: 0 1px 2px rgba(0, 0, 0, 0.18); }
	.pri-btn {
		display: inline-grid; place-items: center; width: 34px; height: 34px; border: 1px solid var(--border);
		border-radius: calc(var(--radius, 0.75rem) - 2px); background: var(--card); color: var(--muted-foreground); cursor: pointer;
	}
	.pri-btn:hover:not(:disabled) { color: var(--foreground); background: var(--hover-overlay); }
	.pri-btn:disabled { opacity: 0.55; cursor: not-allowed; }

	.pri-filters { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; margin-top: 14px; font-size: 0.8125rem; }
	.pri-filters label { display: inline-flex; align-items: center; gap: 6px; color: var(--muted-foreground); }
	.pri-filters select {
		height: 30px; padding: 0 8px; border: 1px solid var(--border); border-radius: calc(var(--radius, 0.75rem) - 4px);
		background: var(--card); color: var(--foreground); font: inherit; max-width: 200px;
	}
	.pri-check { cursor: pointer; }
	.pri-clear {
		border: 0; background: transparent; color: var(--primary); font: inherit; font-size: 0.8125rem; cursor: pointer; padding: 0 2px;
	}
	.pri-clear:hover { text-decoration: underline; }

	.pri-error {
		padding: 12px 14px; border: 1px solid color-mix(in oklch, var(--destructive) 40%, var(--border));
		border-radius: var(--radius, 0.75rem); color: var(--destructive); font-size: 0.875rem;
	}
	.pri-empty {
		display: flex; align-items: center; justify-content: center; gap: 10px; padding: 48px 12px;
		color: var(--muted-foreground); font-size: 0.875rem;
	}

	.pri-list { list-style: none; margin: 0; padding: 0; border: 1px solid var(--border); border-radius: var(--radius, 0.75rem); background: var(--card); overflow: hidden; }
	.pri-row { display: flex; align-items: flex-start; gap: 12px; padding: 11px 14px; border-top: 1px solid var(--border); }
	.pri-row:first-child { border-top: 0; }
	.pri-row:hover { background: var(--hover-overlay); }
	.pri-state { flex-shrink: 0; margin-top: 2px; color: var(--success, #22a06b); display: inline-flex; }
	.pri-row[data-state='draft'] .pri-state { color: var(--muted-foreground); }
	.pri-row[data-state='merged'] .pri-state { color: var(--primary); }
	.pri-row[data-state='closed'] .pri-state { color: var(--destructive); }
	.pri-main { flex: 1; min-width: 0; }
	.pri-title {
		display: inline-flex; align-items: center; gap: 5px; max-width: 100%; color: var(--foreground);
		font-size: 0.9rem; font-weight: 500; text-decoration: none; overflow-wrap: anywhere;
	}
	.pri-title :global(svg) { flex-shrink: 0; opacity: 0; color: var(--muted-foreground); }
	.pri-title:hover { text-decoration: underline; }
	.pri-title:hover :global(svg), .pri-title:focus-visible :global(svg) { opacity: 1; }
	.pri-meta {
		display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 3px; color: var(--muted-foreground);
		font-size: 0.75rem; min-width: 0;
	}
	.pri-meta > span { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 280px; }
	.pri-repo, .pri-branch { font-family: var(--font-mono, monospace); }
	.pri-badges { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
	.pri-chip {
		font-size: 0.6875rem; padding: 2px 8px; border-radius: 999px; border: 1px solid var(--border); color: var(--muted-foreground); white-space: nowrap;
	}
	.pri-chip[data-review='approved'] { color: var(--success, #22a06b); border-color: color-mix(in oklch, var(--success, #22a06b) 45%, var(--border)); }
	.pri-chip[data-review='changes_requested'] { color: var(--destructive); border-color: color-mix(in oklch, var(--destructive) 45%, var(--border)); }
	.pri-checks { display: inline-flex; }
	.pri-checks[data-checks='passing'] { color: var(--success, #22a06b); }
	.pri-checks[data-checks='failing'] { color: var(--destructive); }
	.pri-checks[data-checks='pending'] { color: var(--warning, #c58a00); }
	.pri-age { min-width: 30px; text-align: right; color: var(--muted-foreground); font-size: 0.75rem; font-variant-numeric: tabular-nums; }

	.pri-skipped { margin-top: 14px; font-size: 0.8125rem; color: var(--muted-foreground); }
	.pri-skipped summary { cursor: pointer; }
	.pri-skipped ul { margin: 8px 0 0; padding-left: 18px; }
	.pri-skipped li { margin: 3px 0; }

	:global(.pri-spin) { animation: pri-spin 0.9s linear infinite; }
	@keyframes pri-spin { to { transform: rotate(360deg); } }

	@media (max-width: 640px) {
		.pri-head { padding: 16px 14px 0; }
		.pri-scroll { padding: 12px 14px 20px; }
		.pri-row { flex-wrap: wrap; }
		.pri-badges { width: 100%; padding-left: 28px; }
		.pri-age { margin-left: auto; }
	}
</style>
