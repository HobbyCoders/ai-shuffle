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
	import { ViewShell, ViewPanel, PanelIconButton, ListItem, ListGroupLabel, SettingRow, SelectField, Switch, DetailPane, EmptyDetail, InlineBanner } from '$lib/components/layout/view';
	import PanelField from '$lib/components/layout/view/PanelField.svelte';
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

	let detailOpen = $state(false);
</script>

<ViewShell label="Pull requests" {detailOpen} onBack={() => detailOpen = false}>
 {#snippet panel()}
  <ViewPanel title="Pull requests" {loading}>
   {#snippet actions()}<PanelIconButton label="Refresh pull requests" icon={RefreshCw} onclick={load} disabled={loading} />{/snippet}
   <div role="group" aria-label="Pull request state">
    {#each STATE_OPTIONS as option (option.value)}
     <ListItem variant="plain" id={option.value} name={option.label} selected={serverState === option.value} onselect={() => { setServerState(option.value); detailOpen = true; }} />
    {/each}
   </div>
   <ListGroupLabel label="Filters" compact />
   <div role="group" aria-label="Filter pull requests">
    <PanelField label="Review"><SelectField bind:value={filters.review} label="Review" options={[{value:'any', label:'Any'}, ...Object.entries(REVIEW_LABELS).map(([value, label]) => ({value, label}))]} /></PanelField>
    <PanelField label="Checks"><SelectField bind:value={filters.checks} label="Checks" options={[{value:'any', label:'Any'}, ...Object.entries(CHECKS_LABELS).map(([value, label]) => ({value, label}))]} /></PanelField>
    <PanelField label="Project"><SelectField bind:value={filters.project} label="Project" options={[{value:'any', label:'All projects'}, ...projects.map(p => ({value:p.id, label:p.name}))]} /></PanelField>
    {#if serverState === 'open' || serverState === 'all'}<SettingRow label="Hide drafts"><Switch bind:checked={filters.hideDrafts} label="Hide drafts" /></SettingRow>{/if}
    {#if filtered}<button type="button" class="st-btn" onclick={clearFilters}>Clear filters</button>{/if}
   </div>
  </ViewPanel>
 {/snippet}
 {#snippet main(back)}
  <DetailPane width="data" name={`${STATE_OPTIONS.find(o => o.value === serverState)?.label} pull requests`} icon={GitPullRequest} subtitle={`${shown.length} ${shown.length === 1 ? 'pull request' : 'pull requests'}`} onBack={back} backLabel="Pull requests">
   {#if error}<div role="alert"><InlineBanner tone="warn">{error}</InlineBanner></div>
   {:else if loading && !inbox}<EmptyDetail title="Loading pull requests" message="Fetching repository status…" />
   {:else if inbox}
				{#if inbox.items.length === 0}
					<div class="pri-empty">
						{inbox.repos.length === 0 ? 'No project has a GitHub remote yet.' : `No ${serverState === 'all' ? '' : `${serverState} `}pull requests.`}
					</div>
				{:else if shown.length === 0}
					<div class="pri-empty">
						No pull requests match these filters.
						<button type="button" class="st-btn" onclick={clearFilters}>Clear filters</button>
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
  </DetailPane>
 {/snippet}
</ViewShell>
<style>
 .pri-list { list-style: none; padding: 0; margin: 0; }
 .pri-row { display: flex; align-items: center; gap: 12px; padding: 16px 0; border-bottom: 1px solid var(--st-border); }
 .pri-state { flex: none; color: var(--st-success); }
 .pri-row[data-state='draft'] .pri-state { color: var(--st-mfg); }
 .pri-row[data-state='merged'] .pri-state { color: var(--chart-3, var(--st-mfg)); }
 .pri-row[data-state='closed'] .pri-state { color: var(--st-destructive); }
 .pri-main { flex: 1; min-width: 0; }
 .pri-title { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; font-weight: 500; color: var(--st-fg); }
 .pri-title:hover { color: var(--st-primary); }
 .pri-meta { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 6px; color: var(--st-mfg); font-size: 12px; overflow-wrap: anywhere; }
 .pri-branch { font-family: var(--st-mono); }
 .pri-badges { display: flex; align-items: center; justify-content: flex-end; flex-wrap: wrap; gap: 8px; color: var(--st-mfg); font-size: 12px; }
 .pri-chip[data-review='approved'], .pri-checks[data-checks='passing'] { color: var(--st-success); }
 .pri-chip[data-review='changes_requested'], .pri-checks[data-checks='failing'] { color: var(--st-destructive); }
 .pri-checks[data-checks='pending'] { color: var(--st-warning); }
 .pri-age { font-variant-numeric: tabular-nums; }
 .pri-empty { padding: 48px 0; color: var(--st-mfg); display: grid; justify-items: center; gap: 16px; text-align: center; }
 .pri-skipped { margin-top: 24px; color: var(--st-mfg); font-size: 13px; }
 .pri-skipped summary { cursor: pointer; }
 @media (max-width:600px) { .pri-row { flex-wrap: wrap; } .pri-badges { width: 100%; justify-content: flex-start; padding-left: 28px; } }
</style>
