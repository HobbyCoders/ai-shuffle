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
 import { platform } from '$lib/stores/platform.svelte';
	import { GitPullRequest, GitPullRequestDraft, GitMerge, GitPullRequestClosed, RefreshCw, ListFilter, Check, X, MessageSquare, CircleCheck, CircleX, LoaderCircle, ExternalLink } from 'lucide-svelte';
	import { ViewShell, ViewPanel, PanelIconButton, ListItem, Segmented, SettingRow, SelectField, Switch, DetailPane, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import PanelField from '$lib/components/layout/view/PanelField.svelte';
	import SidebarSheet from '$lib/components/layout/sidebar/SidebarSheet.svelte';
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
	const filterCount = $derived(Number(filters.review !== 'any') + Number(filters.checks !== 'any') + Number(filters.project !== 'any') + Number(filters.hideDrafts));
 const filtered = $derived(filterCount > 0);
 let filtersOpen = $state(false);
 let stacked = $state(false);

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

	let detailOpen = $state(false);
</script>

<div class="pri-surface settings-shell" data-stacked={stacked ? '' : undefined}>
<ViewShell inheritScope label="Pull requests" contentFirst bind:stacked {detailOpen} onBack={() => detailOpen = false}>
 {#snippet panel()}
  <ViewPanel title="Pull requests" {loading}>
   {#snippet actions()}<PanelIconButton label="Refresh pull requests" icon={RefreshCw} onclick={load} disabled={loading} />{/snippet}
   <div role="group" aria-label="Pull request state">
    {#each STATE_OPTIONS as option (option.value)}
     <ListItem variant="plain" id={option.value} name={option.label} selected={serverState === option.value} onselect={() => { setServerState(option.value); detailOpen = true; }} />
    {/each}
   </div>

  </ViewPanel>
 {/snippet}
 {#snippet main()}
  <DetailPane width="data" contentFirst name={`${STATE_OPTIONS.find(o => o.value === serverState)?.label} pull requests`} subtitle={`${shown.length} ${shown.length === 1 ? 'pull request' : 'pull requests'}`}>
   {#snippet heading()}
    <div class="pri-heading"><h2>{STATE_OPTIONS.find(o => o.value === serverState)?.label} pull requests</h2>
    <button type="button" class="st-btn pri-filter" aria-label={`Filter pull requests${filterCount ? `, ${filterCount} active` : ''}`} aria-expanded={filtersOpen} onclick={() => filtersOpen = !filtersOpen}><ListFilter size={16} /><span>Filters</span>{#if filterCount}<span class="pri-count">{filterCount}</span>{/if}</button>
    </div>
   {/snippet}
   {#if stacked}<div class="pri-toolbar"><Segmented label="Pull request state" options={STATE_OPTIONS} value={serverState} onchange={setServerState} /></div>{/if}
   {#if error}<div role="alert"><InlineBanner tone="warn">{error}</InlineBanner></div>
   {:else if loading && !inbox}<EmptyDetail title="Loading pull requests" message="Fetching repository status…" />
   {:else if inbox}
				{#if inbox.items.length === 0}
					<div class="pri-empty">
						{inbox.repos.length === 0 ? 'No project has a GitHub remote yet.' : `No ${serverState === 'all' ? '' : `${serverState} `}pull requests.`}
					</div>
				{:else if shown.length === 0}
					<EmptyDetail title="No matching pull requests" message="No pull requests match these filters." actionLabel="Clear filters" onAction={clearFilters} />
				{:else}
					<ul class="pri-list st-card" aria-label="Pull requests">
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
										<span>by {pr.author}</span>
										<span class="pri-projects" title={pr.projects.map((p) => p.name).join(', ')}>{pr.projects.map((p) => p.name).join(', ')}</span>
									</div>
								</div>
								<div class="pri-badges">
									{#if pr.review !== 'none'}
										<span class="pri-chip" data-review={pr.review} title={REVIEW_LABELS[pr.review]} aria-label={REVIEW_LABELS[pr.review]}>
            {#if pr.review === 'approved'}<Check size={14} />{:else if pr.review === 'changes_requested'}<X size={14} />{:else}<MessageSquare size={14} />{/if}<span>{pr.review === 'review_required' ? 'Review pending' : REVIEW_LABELS[pr.review]}</span>
           </span>
									{/if}
									{#if pr.checks !== 'none'}
										<span class="pri-checks" data-checks={pr.checks} title={CHECKS_LABELS[pr.checks]} aria-label={CHECKS_LABELS[pr.checks]}>
											{#if pr.checks === 'passing'}<CircleCheck size={14} />
											{:else if pr.checks === 'failing'}<CircleX size={14} />
											{:else}<LoaderCircle size={14} />{/if}<span>{pr.checks === 'passing' ? 'Checks passed' : pr.checks === 'failing' ? 'Checks failed' : 'Checks running'}</span>
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
  </DetailPane>
 {/snippet}
</ViewShell>
<SidebarSheet open={filtersOpen} label="Filter pull requests" phone={stacked} onClose={() => filtersOpen = false}>
 <div class="pri-filters">
  <h3>Filter pull requests</h3>
  <PanelField label="Review"><SelectField bind:value={filters.review} label="Review" options={[{value:'any', label:'Any'}, ...Object.entries(REVIEW_LABELS).map(([value, label]) => ({value, label}))]} /></PanelField>
  <PanelField label="Checks"><SelectField bind:value={filters.checks} label="Checks" options={[{value:'any', label:'Any'}, ...Object.entries(CHECKS_LABELS).map(([value, label]) => ({value, label}))]} /></PanelField>
  <PanelField label="Project"><SelectField bind:value={filters.project} label="Project" options={[{value:'any', label:'All projects'}, ...projects.map(p => ({value:p.id, label:p.name}))]} /></PanelField>
  {#if serverState === 'open' || serverState === 'all'}<SettingRow label="Hide drafts"><Switch bind:checked={filters.hideDrafts} label="Hide drafts" /></SettingRow>{/if}
  <div class="pri-filter-actions">{#if filtered}<button type="button" class="st-btn" onclick={clearFilters}>Clear filters</button>{/if}<button type="button" class="st-btn" onclick={() => filtersOpen = false}>Done</button></div>
 </div>
</SidebarSheet>
</div>
<style>
 .pri-surface { position: relative; display: block; height: 100%; min-height: 0; min-width: 0; }
 .pri-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
 .pri-heading { display: flex; width: 100%; align-items: flex-start; gap: 16px; }
 .pri-heading h2 { margin: 0; font-size: 24px; font-weight: 600; letter-spacing: -.01em; line-height: 1.25; overflow-wrap: anywhere; }
 .pri-filter { margin-left: auto; flex: none; }
 .pri-count { font-variant-numeric: tabular-nums; }
 .pri-filters { color: var(--foreground); padding: 8px; }
 .pri-filters h3 { font-size: 14px; font-weight: 600; margin: 0 0 12px; }
 .pri-filter-actions { display: flex; justify-content: flex-end; gap: 8px; margin: 16px 0 8px; }
 .pri-filters :global(.panel-field) { margin-bottom: 12px; }
 .pri-list { list-style: none; padding: 0; margin: 0; overflow: hidden; }
 .pri-row { display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-bottom: 1px solid var(--st-border); }
 .pri-row:last-child { border-bottom: 0; }
 .pri-state { flex: none; color: var(--st-success); }
 .pri-row[data-state='draft'] .pri-state { color: var(--st-mfg); }
 .pri-row[data-state='merged'] .pri-state { color: var(--chart-3, var(--st-mfg)); }
 .pri-row[data-state='closed'] .pri-state { color: var(--st-destructive); }
 .pri-main { flex: 1; min-width: 0; }
 .pri-title { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 500; color: var(--st-fg); }
 .pri-title:hover { color: var(--st-primary); }
 .pri-meta { display: flex; flex-wrap: wrap; gap: 4px 8px; margin-top: 4px; color: var(--sidebar-muted); font-size: 12px; overflow-wrap: anywhere; }
 .pri-meta > span + span::before { content: '·'; display: inline-block; margin-right: 8px; color: var(--st-mfg); font-family: inherit; }
 .pri-repo { color: var(--secondary-foreground); }
 .pri-branch { font-family: var(--st-mono); }
 .pri-badges { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; color: var(--st-mfg); font-size: 12px; }
 .pri-chip, .pri-checks { display: inline-flex; align-items: center; gap: 5px; color: var(--st-mfg); white-space: nowrap; }
 .pri-age { font-variant-numeric: tabular-nums; }
 .pri-empty { padding: 48px 0; color: var(--st-mfg); display: grid; justify-items: center; gap: 16px; text-align: center; }
 .pri-skipped { margin-top: 24px; color: var(--st-mfg); font-size: 13px; }
 .pri-skipped summary { cursor: pointer; }
 @media (max-width:600px) { .pri-toolbar :global(.st-seg button) { min-height: 44px; padding-inline: 10px; } .pri-filter { min-height: 44px; } .pri-filter-actions button { min-height: 44px; } .pri-row { flex-wrap: wrap; } .pri-badges { width: 100%; justify-content: flex-start; padding-left: 28px; } }
</style>
