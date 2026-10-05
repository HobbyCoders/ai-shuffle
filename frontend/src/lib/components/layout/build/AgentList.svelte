<script lang="ts">
	/**
	 * AgentList: the list column of the Agents page. Sections: the personal
	 * assistant (one pinned, tinted row), "Yours" and "Built-in". Each row is a
	 * Settings ListItem: avatar, name with at most two chips (Draft,
	 * Modified), one line of description, and the agent's model on the right.
	 * Up/Down moves between rows. ProfileCard owns the search and filters and
	 * passes the sections in.
	 */
	import type { Profile } from '$lib/api/client';
	import type { AvatarId } from '$lib/api/assistant';
	import { Plus } from 'lucide-svelte';
	import ListItem from '$lib/components/settings/rows/ListItem.svelte';
	import ListGroupLabel from '$lib/components/settings/rows/ListGroupLabel.svelte';
	import AgentAvatar from '$lib/components/assistant/AgentAvatar.svelte';
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
	const total = $derived(sections.assistant.length + sections.yours.length + sections.builtin.length);
</script>

{#snippet row(p: Profile, pinned = false)}
	<ListItem
		id={p.id}
		name={p.name}
		summary={p.description || 'No description'}
		meta={metaOf(p)}
		selected={selectedId === p.id}
		onselect={() => onSelect(p)}
		variant={pinned ? 'pinned' : 'default'}
	>
		{#snippet lead()}
			<AgentAvatar avatar={avatarOf(p.id)} size={pinned ? 36 : 32} color={colorOf(p)} neutral={p.is_builtin} />
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
	{#if sections.assistant.length}
		<ListGroupLabel label="Your assistant" />
		{#each sections.assistant as p (p.id)}{@render row(p, true)}{/each}
	{/if}

	{#if showYours}
		{#if yoursCount}
			<ListGroupLabel label="Yours"><span class="c">{yoursCount}</span></ListGroupLabel>
			{#if draft}
				<ListItem
					id="__draft"
					name={draft.name.trim() || 'New agent'}
					summary="Name it to save"
					selected={selectedId === ''}
					onselect={onSelectDraft}
				>
					{#snippet lead()}<AgentAvatar avatar="robot" size={32} color={draft.color} />{/snippet}
					{#snippet badges()}<span class="chip">Draft</span>{/snippet}
				</ListItem>
			{/if}
			{#each sections.yours as p (p.id)}{@render row(p)}{/each}
		{:else if noneOfYours && !narrowed}
			<ListGroupLabel label="Yours"><span class="c">0</span></ListGroupLabel>
			<div class="l-empty">
				<span class="avs" aria-hidden="true">
					<AgentAvatar avatar="fox" size={30} color="warning" />
					<AgentAvatar avatar="bolt" size={30} color="blue" />
					<AgentAvatar avatar="sprout" size={30} color="emerald" />
				</span>
				<b>No agents of your own yet</b>
				<span>Start blank, from a built-in, or describe one.</span>
				<button type="button" class="st-btn sm" onclick={onNew}><Plus />New agent</button>
			</div>
		{/if}
	{/if}

	{#if sections.builtin.length}
		<ListGroupLabel label="Built-in"><span class="c">{sections.builtin.length}</span></ListGroupLabel>
		{#each sections.builtin as p (p.id)}{@render row(p)}{/each}
	{/if}

	{#if narrowed && sections.matches === 0 && !draft}
		<p class="l-none">
			No agents match{query.trim() ? ` “${query.trim()}”` : ' this filter'}.
			<button type="button" class="lnk" onclick={onClear}>Clear</button>
		</p>
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
	.l-empty {
		margin: 4px 2px;
		padding: 18px 16px;
		border: 1px dashed var(--st-border-strong);
		border-radius: var(--st-radius);
		display: grid;
		gap: 4px;
		justify-items: start;
		color: var(--st-mfg);
		font-size: 13px;
	}
	.l-empty b {
		color: var(--st-fg);
		font-weight: 600;
		font-size: 13.5px;
	}
	.avs {
		display: flex;
		margin-bottom: 8px;
	}
	.avs > :global(* + *) {
		margin-left: -8px;
	}
	.l-empty .st-btn {
		margin-top: 8px;
	}
	.l-none {
		padding: 18px 10px;
		margin: 0;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.lnk {
		padding: 0;
		border: 0;
		background: none;
		color: var(--st-primary);
		font-weight: 600;
		font-size: inherit;
	}
	.lnk:hover {
		text-decoration: underline;
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
	.sk-row {
		display: grid;
		grid-template-columns: 32px 1fr;
		gap: 12px;
		align-items: center;
		padding: 12px 10px;
	}
	.sk.a {
		width: 32px;
		height: 32px;
		border-radius: 9px;
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
