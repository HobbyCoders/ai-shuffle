<script lang="ts">
	/**
	 * Integrations: GitHub (the worker's gh sign-in) and API users for
	 * connectors. "Add API user" adds one draft row; the key shows once after
	 * Create or Rotate.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Bot, Github, Plug } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { setApiUserSearchItems } from '$lib/settings/entries/integrations';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import ListGroupLabel from '../rows/ListGroupLabel.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import GitHubDetail from './GitHubDetail.svelte';
	import ApiUserDetail from './ApiUserDetail.svelte';
	import { GitHubAuthFlow } from './github-auth.svelte';
	import { ApiUsersModel, DRAFT_ID } from './api-users.svelte';

	const CATEGORY = 'integrations' as const;
	const model = new ApiUsersModel();
	const github = new GitHubAuthFlow();
	let filter = $state('');

	onMount(() => {
		void model.load();
		void github.load();
	});
	onDestroy(() => {
		github.dispose();
		model.dispose();
	});
	watchResources(['api_users', 'settings', 'agents', 'projects'], () => model.load());

	$effect(() => setApiUserSearchItems(model.users));

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const q = $derived(filter.trim().toLowerCase());
	const matches = (...parts: (string | null | undefined)[]) =>
		!q || parts.filter(Boolean).join(' ').toLowerCase().includes(q);

	const githubSummary = $derived(
		github.forbidden
			? FORBIDDEN_TEXT
			: github.authenticated === null
				? 'Checking'
				: github.authenticated
					? github.user
						? `@${github.user}`
						: 'Connected'
					: 'Not connected'
	);
	const userRows = $derived(
		model.users.map((u) => {
			// No assignments means unrestricted (every agent), not "0 agents";
			// a key assigned only removed agents stays restricted and reads 0.
			const agents = model.existingProfileCount(u.profile_ids);
			const reach = u.profile_ids?.length ? `${agents} agent${agents === 1 ? '' : 's'}` : 'all agents';
			return {
				id: u.id,
				name: u.name,
				status: u.is_active ? ('ok' as const) : ('off' as const),
				summary: u.is_active ? `${u.model || 'Agent default'}, ${reach}` : 'Disabled'
			};
		})
	);
	const shownUsers = $derived(userRows.filter((r) => matches(r.name, r.summary, 'api users')));
	const showDraft = $derived(model.draft !== null && matches(model.draft.name || 'New API user', 'api users'));
	const showGithub = $derived(matches('GitHub', githubSummary, 'accounts'));

	function add() {
		const id = model.addDraft();
		settingsNav.setTab(CATEGORY, id, 'Overview');
		settingsNav.selectItem(id, CATEGORY);
	}
</script>

<CollectionPage
	category={CATEGORY}
	count={1 + model.users.length}
	addLabel="Add API user"
	onAdd={add}
	addDisabled={model.forbidden}
	bind:filter
>
	{#snippet list()}
		{#if showGithub}
			<ListGroupLabel label="Accounts" />
			<ListItem
				id="github"
				name="GitHub"
				icon={Github}
				category={CATEGORY}
				status={github.authenticated ? 'ok' : 'off'}
				summary={githubSummary}
			/>
		{/if}
		<div data-setting-anchor="integrations.apiUsers">
			<ListGroupLabel label="API users" />
			{#if model.error === 'Failed to load data'}
				<InlineBanner tone="warn">
					<span role="alert">Could not load API users</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
			{#if showDraft && model.draft}
				<ListItem
					id={DRAFT_ID}
					name={model.draft.name.trim() || 'New API user'}
					icon={Bot}
					category={CATEGORY}
					status="busy"
					summary="Not created yet"
				/>
			{/if}
			{#each shownUsers as row (row.id)}
				<ListItem
					id={row.id}
					name={row.name}
					icon={Bot}
					category={CATEGORY}
					status={row.status}
					summary={row.summary}
				/>
			{/each}
			{#if model.forbidden}
				<p class="st-empty">{FORBIDDEN_TEXT}</p>
			{:else if model.loading}
				<p class="st-empty" role="status">Loading API users…</p>
			{:else if model.users.length === 0 && !model.draft}
				<p class="st-empty">No API users yet.</p>
			{:else if q && shownUsers.length === 0 && !showDraft}
				<p class="st-empty">Nothing matches.</p>
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if selected === 'github'}
			<GitHubDetail flow={github} />
		{:else if selected === DRAFT_ID && model.draft}
			<ApiUserDetail {model} id={DRAFT_ID} />
		{:else if selected && model.userById(selected)}
			<ApiUserDetail {model} id={selected} />
		{:else if selected && selected !== DRAFT_ID && !model.loading}
			<EmptyDetail icon={Plug} title="Not found" message="This API user was deleted." />
		{:else}
			<EmptyDetail icon={Plug} message="Select GitHub or an API user." />
		{/if}
	{/snippet}
</CollectionPage>
