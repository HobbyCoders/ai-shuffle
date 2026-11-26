<script lang="ts">
	/**
	 * Settings > Memory > Proposed memories: what session reflection noticed
	 * but was not sure enough to save. Accept saves one as an agent-saved
	 * memory; Dismiss drops it. Unanswered ones expire on their own. Agents
	 * never see a proposal until it is saved.
	 */
	import { onMount } from 'svelte';
	import { Inbox } from 'lucide-svelte';
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import InlineBanner from '../rows/InlineBanner.svelte';
	import { FORBIDDEN_TEXT } from '$lib/settings/access.svelte';
	import { dateText, type MemoryModel } from './memory-model.svelte';
	import { chatTitle, expiryText, sessionText, type ProposalModel } from './proposal-model.svelte';

	interface Props {
		proposals: ProposalModel;
		memories: MemoryModel;
	}

	let { proposals, memories }: Props = $props();

	onMount(() => {
		void proposals.load();
	});

	const count = $derived(proposals.items.length);

	async function decide(id: string, accept: boolean) {
		const saved = await proposals.decide(id, accept);
		// The new memory joins the list behind this pane.
		if (saved) void memories.load();
	}
</script>

<div class="pr-detail">
	<DetailPane
		name="Proposed memories"
		category="memory"
		item="proposals"
		icon={Inbox}
		status={proposals.loaded ? (count ? 'warn' : 'ok') : undefined}
		statusText={proposals.loaded ? (count ? `${count.toLocaleString()} waiting` : 'None waiting') : undefined}
		subtitle="What the learner noticed but wasn’t sure enough to save"
	>
		{#snippet banner()}
			{#if proposals.error}
				<InlineBanner tone="warn"><span role="alert">{proposals.error}</span></InlineBanner>
			{/if}
			{#if proposals.notice}
				<InlineBanner tone="info" compact><span role="status">{proposals.notice}</span></InlineBanner>
			{/if}
			{#if proposals.loadError}
				<InlineBanner tone="warn">
					<span role="alert">{proposals.loadError}</span>
					{#snippet actions()}
						<button type="button" class="st-btn sm" disabled={proposals.loading} onclick={() => proposals.load()}>Retry</button>
					{/snippet}
				</InlineBanner>
			{/if}
		{/snippet}
		{#snippet children()}
			{#if proposals.forbidden}
				<p class="st-empty">{FORBIDDEN_TEXT}</p>
			{:else if !proposals.loaded}
				{#if proposals.loading}<p class="st-empty" role="status">Loading proposed memories…</p>{/if}
			{:else if !count}
				<RowGroup title="Waiting for you">
					<p class="st-empty pr-pad">
						{proposals.mode === 'continuous'
							? 'Nothing waiting. This worker runs the older continuous learner, which saves memories directly.'
							: 'Nothing waiting. When a chat teaches something the learner isn’t sure about, it appears here.'}
					</p>
				</RowGroup>
			{:else}
				<RowGroup title="Waiting for you" count={count}>
					<ul class="pr-list">
						{#each proposals.items as p (p.id)}
							<li class="pr-item" data-testid="memory-proposal">
								<p class="pr-text">{p.text}</p>
								{#if p.quote}
									<blockquote class="pr-quote"><span class="st-vh">You said: </span>“{p.quote}”</blockquote>
								{/if}
								<p class="pr-meta">
									{[p.kind_label, p.scope_label, sessionText(p), expiryText(p.expires_at)].filter(Boolean).join(' · ')}
								</p>
								{#if p.sessions.length}
									<ul class="pr-chats" aria-label="Chats that proposed this">
										{#each p.sessions as s (s.id)}
											<li>{chatTitle(s)}</li>
										{/each}
									</ul>
								{/if}
								<div class="pr-actions">
									<span class="pr-when" title={p.when ?? undefined}>{p.when ?? ''}</span>
									<button
										type="button"
										class="st-btn ghost sm"
										disabled={!!proposals.busy}
										aria-label={`Dismiss: ${p.text}`}
										onclick={() => decide(p.id, false)}>Dismiss</button
									>
									<button
										type="button"
										class="st-btn primary sm"
										disabled={!!proposals.busy}
										aria-label={`Accept: ${p.text}`}
										onclick={() => decide(p.id, true)}>{proposals.busy === p.id ? 'Saving…' : 'Accept'}</button
									>
								</div>
								{#if p.created_at}<p class="st-vh">Proposed {dateText(p.created_at)}</p>{/if}
							</li>
						{/each}
					</ul>
				</RowGroup>
			{/if}
			{#if proposals.loaded && !proposals.forbidden}
				<div class="pr-note">
					<InlineBanner tone="info" compact>
						A proposal is saved as a memory when a second chat finds the same thing, or when you accept it. Otherwise it
						expires after {proposals.ttlDays} days. Agents don’t use a proposal until it’s saved. Proposals and your
						decisions are shared with all your workers.
					</InlineBanner>
				</div>
			{/if}
		{/snippet}
	</DetailPane>
</div>

<style>
	.pr-pad {
		padding: 12px 14px;
	}
	.pr-list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.pr-item {
		padding: 12px 14px;
		border-top: 1px solid var(--st-line);
	}
	.pr-item:first-child {
		border-top: 0;
	}
	.pr-text {
		margin: 0;
		font-size: 13.5px;
		line-height: 1.45;
		color: var(--st-fg);
		overflow-wrap: anywhere;
	}
	.pr-quote {
		margin: 6px 0 0;
		padding-left: 10px;
		border-left: 2px solid var(--st-line);
		font-size: 12.5px;
		font-style: italic;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
	.pr-meta {
		margin: 6px 0 0;
		font-size: 12px;
		color: var(--st-mfg);
	}
	.pr-chats {
		margin: 4px 0 0;
		padding-left: 18px;
		font-size: 12px;
		color: var(--st-mfg);
		overflow-wrap: anywhere;
	}
	.pr-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-top: 10px;
	}
	.pr-when {
		flex: 1;
		min-width: 0;
		font-size: 12px;
		color: var(--st-subtle);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pr-note {
		margin-top: 12px;
	}
</style>
