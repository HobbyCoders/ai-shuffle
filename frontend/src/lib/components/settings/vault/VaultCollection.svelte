<script lang="ts">
	/**
	 * Settings > Vault: the owner's saved sign-ins on this worker (SSH machines
	 * and web services). The agent never sees a sign-in; using one still needs
	 * the owner's approval in the chat that asks. Stored secrets never come back
	 * to this page: a blank secret keeps the stored one.
	 */
	import { onMount } from 'svelte';
	import { Plus, Vault } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { setVaultSearchItems } from '$lib/settings/entries/vault';
	import CollectionPage from '../shell/CollectionPage.svelte';
	import EmptyDetail from '../shell/EmptyDetail.svelte';
	import ListItem from '../rows/ListItem.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import VaultEntryDetail from './VaultEntryDetail.svelte';
	import { DRAFT_ID, VaultModel, kindIcon, kindLabel } from './vault-model.svelte';
	import '../pages/editors/editors.css';

	const CATEGORY = 'vault' as const;
	const model = new VaultModel();
	let filter = $state('');

	onMount(() => void model.load());
	watchResources(['vault'], () => model.load());

	$effect(() => setVaultSearchItems(model.entries));

	const selected = $derived(settingsNav.selectedItem(CATEGORY));
	const q = $derived(filter.trim().toLowerCase());
	const matches = (...parts: (string | null | undefined)[]) =>
		!q || parts.filter(Boolean).join(' ').toLowerCase().includes(q);

	const shown = $derived(model.entries.filter((e) => matches(e.label, e.target, kindLabel(e.kind))));
	const showDraft = $derived(model.draft !== null && matches(model.draft.label || 'New sign-in'));
	const known = $derived(selected === DRAFT_ID ? !!model.draft : !!(selected && model.entry(selected)));

	function add() {
		settingsNav.selectItem(model.addDraft(), CATEGORY);
	}
</script>

<CollectionPage
	category={CATEGORY}
	count={model.entries.length}
	addLabel="Add sign-in"
	filterPlaceholder={model.entries.length + Number(!!model.draft) > 6 ? 'Filter sign-ins' : null}
	onAdd={add}
	addDisabled={!model.editable}
	bind:filter
>
	{#snippet list()}
		<div data-setting-anchor="vault.entries">
			{#if model.blocked}
				<InlineBanner tone="warn">
					<span role="alert">{model.blocked}</span>
				</InlineBanner>
			{:else if model.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{model.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" onclick={() => model.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{:else if model.loaded && !model.forbidden && !model.available}
				<InlineBanner tone="warn">
					<span role="alert">The Vault is locked on this worker. Sign-ins cannot be added or changed until it restarts with its encryption key.</span>
				</InlineBanner>
			{/if}
			{#if showDraft || shown.length}
				<RowGroup title="Sign-ins">
					{#if showDraft && model.draft}
						<ListItem
							id={DRAFT_ID}
							name={model.draft.label.trim() || 'New sign-in'}
							icon={kindIcon(model.draft.kind)}
							category={CATEGORY}
							status="busy"
							summary="Not added yet"
						/>
					{/if}
					{#each shown as e (e.id)}
						<ListItem
							id={e.id}
							name={e.label}
							icon={kindIcon(e.kind)}
							showIcon
							category={CATEGORY}
							status={model.dirty(e.id) ? 'warn' : undefined}
							summary={model.dirty(e.id) ? 'Unsaved changes' : e.target}
							meta={e.kind === 'service' ? 'web' : 'ssh'}
						/>
					{/each}
				</RowGroup>
			{/if}
			{#if model.blocked}
				<!-- The banner above says why; nothing else to show. -->
			{:else if model.forbidden}
				<div class="st-card"><p class="st-empty">{FORBIDDEN_TEXT}</p></div>
			{:else if model.loading && !model.loaded}
				<div class="st-card"><p class="st-empty" role="status">Loading the Vault…</p></div>
			{:else if model.loaded && model.entries.length === 0 && !model.draft}
				<EmptyDetail
					icon={Vault}
					title="Nothing in the Vault yet"
					message="Save a machine or web service sign-in. The agent can use it after you approve, and never sees it."
					full={!selected}
				>
					<button type="button" class="st-btn primary" disabled={!model.editable} onclick={add}><Plus />Add sign-in</button>
				</EmptyDetail>
			{:else if q && shown.length === 0 && !showDraft}
				<EmptyDetail message="Nothing matches." actionLabel="Clear" onAction={() => (filter = '')} />
			{/if}
			{#if model.loaded && !model.blocked && !model.forbidden}
				<p class="st-note">
					Encrypted on this worker; never synced or sent to a model. Using a sign-in still needs your approval in the chat that
					asks for it.
				</p>
			{/if}
		</div>
	{/snippet}
	{#snippet detail()}
		{#if selected && known}
			{#key selected}
				<VaultEntryDetail {model} id={selected} />
			{/key}
		{:else if selected && selected !== DRAFT_ID && model.loaded && !model.blocked}
			<EmptyDetail icon={Vault} title="Not found" message="This sign-in was removed." />
		{:else}
			<EmptyDetail icon={Vault} message="Select a sign-in, or add one." idle />
		{/if}
	{/snippet}
</CollectionPage>
