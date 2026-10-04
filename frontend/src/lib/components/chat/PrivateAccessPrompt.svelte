<script lang="ts">
	/**
	 * A private-access request (RequestChatAccess) or one-shot approval waiting
	 * in the open chat, shown above the composer where the owner is reading
	 * (#1807). The private inputs stay in the access dialog: Review opens it.
	 * ChatAccess owns the data and publishes it through chatAccessSummary.
	 */
	import '../deck/cards/card-design-system.css';
	import './private-access.css';
	import { chatAccessSummary, type PendingAccessItem } from '$lib/stores/chat-access';

	let { sessionId }: { sessionId: string | null | undefined } = $props();

	const summary = $derived($chatAccessSummary);
	const items = $derived<PendingAccessItem[]>(
		summary && sessionId && summary.sessionId === sessionId && summary.pending > 0 ? summary.items ?? [] : []
	);
	const count = $derived(summary && summary.sessionId === sessionId ? summary.pending : 0);

	const KIND_LABEL: Record<PendingAccessItem['kind'], string> = {
		machine: 'SSH machine',
		folder: 'Folder',
		service: 'HTTPS service',
		action: 'One-shot approval'
	};
	const heading = $derived(
		items.length && items.every((i) => i.kind === 'action') ? 'Approval waiting for you' : 'Private access requested'
	);
</script>

{#if count > 0}
	<div class="access-prompt-dock">
		<section class="pa-card access-prompt" aria-label="Private access waiting for you" aria-live="polite">
			<header class="pa-head">
				<div class="pa-icon" aria-hidden="true">
					<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path stroke-linecap="round" stroke-linejoin="round" d="M9 12l2 2 4-4m5.6-4.6A12 12 0 0112 3a12 12 0 01-8.6 3.4A12 12 0 003 9c0 5.6 3.8 10.3 9 11.6 5.2-1.3 9-6 9-11.6 0-1.2-.2-2.4-.4-3.6z" /></svg>
				</div>
				<div class="pa-title">
					<h3><span>{heading}</span><span class="card-badge card-badge--warning">Needs you</span></h3>
					<p class="pa-subtitle">The agent waits until you answer.{#if count > 1} {count} items.{/if}</p>
				</div>
				<button type="button" class="card-btn-primary review" onclick={() => summary?.open()}>
					Review
				</button>
			</header>
			{#if items.length}
				<ul class="items">
					{#each items.slice(0, 3) as item (item.id)}
						<li>
							<span class="kind">{KIND_LABEL[item.kind]}</span>
							<span class="detail" title={item.detail}>{item.detail}</span>
						</li>
					{/each}
					{#if items.length > 3}
						<li class="more">and {items.length - 3} more</li>
					{/if}
				</ul>
			{/if}
		</section>
	</div>
{/if}

<style>
	.access-prompt-dock { padding: 0 0.75rem 0.625rem; }
	:global(html[data-platform='desktop']) .access-prompt-dock { padding-right: 1.5rem; padding-left: 1.5rem; }
	.access-prompt { max-width: 48rem; margin: 0 auto; border-color: color-mix(in oklch, var(--warning) 45%, var(--border)); }
	.access-prompt .pa-head { padding: 0.625rem 0.75rem; }
	.review { flex-shrink: 0; min-height: 44px; padding: 0 1rem; }
	.review:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.items { list-style: none; margin: 0; padding: 0.5rem 0.75rem 0.625rem; display: grid; gap: 0.375rem; }
	.items li { display: flex; align-items: baseline; gap: 0.5rem; min-width: 0; font-size: 0.75rem; }
	.kind { flex-shrink: 0; font-size: 0.625rem; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: var(--muted-foreground); }
	.detail { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--foreground); }
	.more { color: var(--muted-foreground); }
	@media (max-width: 480px) {
		.items li { flex-direction: column; gap: 0.125rem; }
		.detail { white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; }
	}
	@media (prefers-reduced-motion: reduce) {
		.access-prompt { animation: none; }
	}
</style>
