<script lang="ts">
	/**
	 * AgentList: the list column of the Agents page. Sections: "Yours" and
	 * "Built-in". Each row is a
	 * Settings ListItem: avatar, name with at most two chips (Draft,
	 * Modified), one line of description, and the agent's model on the right.
	 * Up/Down moves between rows. ProfileCard owns the search and filters and
	 * passes the sections in.
	 */
	import type { Profile } from '$lib/api/client';
	import type { AvatarId } from '$lib/api/agent-identity';
	import { Plus } from 'lucide-svelte';
	import ListItem from '$lib/components/settings/rows/ListItem.svelte';
	import ListGroupLabel from '$lib/components/settings/rows/ListGroupLabel.svelte';
 import EmptyDetail from '$lib/components/settings/shell/EmptyDetail.svelte';
	import AgentAvatar from '$lib/components/agents/AgentAvatar.svelte';
	import type { AgentSections } from '$lib/components/deck/cards/profiles/agentList';

	interface Props {
		sections: AgentSections;
		/** The unsaved new agent, listed first under Yours. */
		draft: { name: string; color: string | null } | null;
		/** Open agent id; '' while the draft is open. */
		selectedId: string | null;
		loading: boolean;
		/** No agents of your own at all (shows the "make one" card). */
		noneOfYours: boolean;
		/** The worker has built-in agents to start from (the "make one" card says so). */
		hasBuiltins?: boolean;
		/** Search or a filter narrows the list. */
		narrowed: boolean;
		query: string;
		/** Built-in section hidden by the filter. */
		showYours: boolean;
		avatarOf: (id: string) => AvatarId;
		colorOf: (p: Profile) => string | null;
		metaOf: (p: Profile) => string;
		onSelect: (p: Profile) => void;
		onSelectDraft: () => void;
		onNew: () => void;
		onClear: () => void;
	}

	let {
		sections,
		draft,
		selectedId,
		loading,
		noneOfYours,
		hasBuiltins = true,
		narrowed,
		query,
		showYours,
		avatarOf,
		colorOf,
		metaOf,
		onSelect,
		onSelectDraft,
		onNew,
		onClear
	}: Props = $props();

	const yoursCount = $derived(sections.yours.length + (draft ? 1 : 0));
	const total = $derived(sections.yours.length + sections.builtin.length);
</script>

{#snippet row(p: Profile)}
	<ListItem
		id={p.id}
		name={p.name}
		summary={p.description || 'No description'}
		meta={metaOf(p)}
		selected={selectedId === p.id}
		onselect={() => onSelect(p)}
	>
		{#snippet lead()}
			<AgentAvatar avatar={avatarOf(p.id)} size={20} color={colorOf(p)} neutral={p.is_builtin} />
		{/snippet}
		{#snippet badges()}
			{#if p.is_builtin && p.is_modified}<span class="chip warn">Modified</span>{/if}
		{/snippet}
	</ListItem>
{/snippet}

{#if loading && total === 0 && !draft}
	<div aria-hidden="true">
		<div class="sk-lg"><span class="sk" style:width="70px" style:height="9px"></span></div>
		{#each Array.from({ length: 6 }, (_, i) => i) as i (i)}
			<div class="sk-row">
				<span class="sk a"></span>
				<span>
					<span class="sk l1" style:width="{40 + ((i * 13) % 30)}%"></span>
					<span class="sk l2"></span>
				</span>
			</div>
		{/each}
	</div>
	<p class="st-vh" role="status">Loading agents</p>
{:else}
	{#if showYours}
		{#if yoursCount}
			<ListGroupLabel compact label="Yours"><span class="c">{yoursCount}</span></ListGroupLabel>
			{#if draft}
				<ListItem
					id="__draft"
					name={draft.name.trim() || 'New agent'}
					summary="Name it to save"
					selected={selectedId === ''}
					onselect={onSelectDraft}
				>
					{#snippet lead()}<AgentAvatar avatar="robot" size={20} color={draft.color} />{/snippet}
					{#snippet badges()}<span class="chip">Draft</span>{/snippet}
				</ListItem>
			{/if}
			{#each sections.yours as p (p.id)}{@render row(p)}{/each}
		{:else if noneOfYours && !narrowed}
			<ListGroupLabel compact label="Yours"><span class="c">0</span></ListGroupLabel>
			<EmptyDetail message="No agents of your own yet" />
		{/if}
	{/if}

	{#if sections.builtin.length}
		<ListGroupLabel compact label="Built-in"><span class="c">{sections.builtin.length}</span></ListGroupLabel>
		{#each sections.builtin as p (p.id)}{@render row(p)}{/each}
	{/if}

	{#if narrowed && sections.matches === 0 && !draft}
		<EmptyDetail message={`No agents match${query.trim() ? ` “${query.trim()}”` : ' this filter'}.`} actionLabel="Clear" onAction={onClear} />
	{/if}
{/if}

<style>
	.c {
		font-variant-numeric: tabular-nums;
	}
	.chip {
		flex: none;
		display: inline-flex;
		align-items: center;
		padding: 1px 7px;
		border-radius: 999px;
		font-size: 10.5px;
		font-weight: 600;
		background: var(--st-muted);
		color: var(--st-mfg);
		line-height: 1.5;
	}
	.chip.warn {
		background: var(--st-warning-soft);
		color: var(--st-warning);
	}

	/* Skeleton rows */
	.sk {
		display: block;
		border-radius: 6px;
		background: linear-gradient(
			90deg,
			var(--st-muted) 0%,
			color-mix(in oklch, var(--st-muted) 55%, var(--st-card)) 50%,
			var(--st-muted) 100%
		);
		background-size: 200% 100%;
		animation: al-sk 1.3s ease-in-out infinite;
	}
	.sk-lg {
		padding: 18px 10px 6px;
	}
	/* Same geometry as the rows: the 20px avatar centred on the 16px slot, text on the Chats title x. */
	.sk-row {
		display: grid;
		grid-template-columns: 20px 1fr;
		gap: 4px;
		align-items: center;
		padding: 12px 10px 12px 6px;
	}
	.sk.a {
		width: 20px;
		height: 20px;
		border-radius: 50%;
	}
	.sk.l1 {
		height: 11px;
		margin-bottom: 7px;
	}
	.sk.l2 {
		height: 9px;
		width: 78%;
	}
	@keyframes al-sk {
		from {
			background-position: 100% 0;
		}
		to {
			background-position: -100% 0;
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.sk {
			animation: none;
		}
	}
</style>
