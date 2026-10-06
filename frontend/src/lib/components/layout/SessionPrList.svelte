<script lang="ts">
	/**
	 * SessionPrList — the list behind a sidebar card's multi-PR chip.
	 *
	 * Opens in the top layer (native `popover`) because the card frame clips
	 * its overflow and moves with swipe transforms. Placed under the anchor,
	 * or above it when the viewport has no room below. Escape, a press
	 * outside, or focus leaving closes it; Escape hands focus back to the
	 * control that opened it.
	 */
	import { onMount } from 'svelte';
	import { GitMerge, GitPullRequest, GitPullRequestClosed } from 'lucide-svelte';
	import type { PullRequestSummary, SessionPullRequest } from './session-card-data';

	interface Props {
		prs: SessionPullRequest[];
		summary: PullRequestSummary;
		/** The element the list is placed against (the chip). */
		anchor: HTMLElement | null;
		/** Where Escape returns focus. */
		returnFocus: HTMLElement | null;
		onClose: () => void;
	}

	let { prs, summary, anchor, returnFocus, onClose }: Props = $props();

	const labelId = $props.id();
	let node = $state<HTMLDivElement | null>(null);
	const STATE_WORD = { open: 'Open', merged: 'Merged', closed: 'Closed' } as const;
	const TONE = { open: 'green', merged: 'purple', closed: 'muted' } as const;
	// Newest first: the PR the user just made is the one they look for.
	const ordered = $derived([...prs].reverse());

	function place() {
		if (!node || !anchor) return;
		const a = anchor.getBoundingClientRect();
		const r = node.getBoundingClientRect();
		const edge = 8;
		const below = window.innerHeight - a.bottom - edge;
		const top = below >= r.height + 6 || a.top < r.height + edge
			? Math.min(a.bottom + 6, window.innerHeight - r.height - edge)
			: a.top - r.height - 6;
		const left = Math.max(edge, Math.min(window.innerWidth - r.width - edge, a.left - 10));
		node.style.setProperty('--pr-pop-top', `${Math.max(edge, top)}px`);
		node.style.setProperty('--pr-pop-left', `${left}px`);
	}

	function close(restore: boolean) {
		onClose();
		if (restore) returnFocus?.focus({ preventScroll: true });
	}

	function onKeydown(event: KeyboardEvent) {
		if (event.key !== 'Escape') return;
		event.preventDefault();
		event.stopPropagation();
		close(true);
	}

	function onFocusOut(event: FocusEvent) {
		const next = event.relatedTarget as Node | null;
		if (next && !node?.contains(next) && next !== returnFocus && next !== anchor) close(false);
	}

	onMount(() => {
		const el = node;
		if (!el) return;
		if (typeof el.showPopover === 'function') {
			el.setAttribute('popover', 'manual');
			el.showPopover();
		}
		place();
		el.focus({ preventScroll: true });
		const outside = (event: PointerEvent) => {
			const target = event.target as Node | null;
			if (!target || el.contains(target) || anchor?.contains(target) || returnFocus?.contains(target)) return;
			close(false);
		};
		// Defer so the press that opened the list does not close it.
		const timer = setTimeout(() => document.addEventListener('pointerdown', outside, true));
		window.addEventListener('resize', place);
		window.addEventListener('scroll', place, true);
		return () => {
			clearTimeout(timer);
			document.removeEventListener('pointerdown', outside, true);
			window.removeEventListener('resize', place);
			window.removeEventListener('scroll', place, true);
			if (el.matches?.(':popover-open')) el.hidePopover();
		};
	});
</script>

<div
	bind:this={node}
	class="pr-pop"
	role="dialog"
	aria-labelledby={labelId}
	tabindex="-1"
	onkeydown={onKeydown}
	onfocusout={onFocusOut}
>
	<div class="pr-pop-head">
		<span id={labelId} class="pr-pop-title">Pull requests</span>
		<span class="pr-pop-count">
			{#each summary.parts as part, i (part.state)}{#if i > 0}<span class="sep" aria-hidden="true">{' · '}</span>{/if}<span class={TONE[part.state]}>{part.text.replace(/ PRs? /, ' ')}</span>{/each}
		</span>
	</div>
	<ul class="pr-pop-list">
		{#each ordered as pr (`${pr.url ?? ''}#${pr.number}`)}
			{@const label = pr.title || pr.branch || 'Pull request'}
			<li>
				<svelte:element
					this={pr.url ? 'a' : 'div'}
					class="pr-row"
					href={pr.url ?? undefined}
					target={pr.url ? '_blank' : undefined}
					rel={pr.url ? 'noopener noreferrer' : undefined}
					aria-label={`#${pr.number} ${label}, ${STATE_WORD[pr.state].toLowerCase()}${pr.source === 'subagent' ? ', by a sub-agent' : ''}`}
				>
					<span class="pr-icon {TONE[pr.state]}" aria-hidden="true">
						{#if pr.state === 'open'}
							<GitPullRequest size={14} strokeWidth={2} />
						{:else if pr.state === 'merged'}
							<GitMerge size={14} strokeWidth={2} />
						{:else}
							<GitPullRequestClosed size={14} strokeWidth={2} />
						{/if}
					</span>
					<span class="pr-num" aria-hidden="true">#{pr.number}</span>
					<span class="pr-title" aria-hidden="true" title={label}>{label}</span>
					{#if pr.source === 'subagent'}<span class="pr-tag" aria-hidden="true">sub-agent</span>{/if}
					<span class="pr-state {TONE[pr.state]}" aria-hidden="true">{STATE_WORD[pr.state]}</span>
				</svelte:element>
			</li>
		{/each}
	</ul>
</div>

<style>
	.pr-pop {
		position: fixed;
		inset: auto;
		top: var(--pr-pop-top, 0);
		left: var(--pr-pop-left, 0);
		z-index: 60;
		width: min(320px, calc(100vw - 16px));
		max-height: min(360px, calc(100vh - 16px));
		margin: 0;
		padding: 6px;
		display: flex;
		flex-direction: column;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--popover);
		color: var(--popover-foreground);
		box-shadow: var(--shadow-m);
		overflow: hidden;
		animation: pr-pop-in 140ms cubic-bezier(0.16, 1, 0.3, 1);
	}
	.pr-pop:focus { outline: none; }
	.pr-pop-head {
		display: flex;
		align-items: baseline;
		justify-content: space-between;
		gap: 8px;
		padding: 4px 8px 6px;
		font-size: 11.5px;
	}
	.pr-pop-title { font-weight: 600; }
	.pr-pop-count { color: var(--muted-foreground); white-space: nowrap; }
	.pr-pop-list {
		list-style: none;
		margin: 0;
		padding: 0;
		overflow-y: auto;
		overscroll-behavior: contain;
	}
	.pr-row {
		display: flex;
		align-items: center;
		gap: 7px;
		min-height: 36px;
		padding: 4px 8px;
		border-radius: 8px;
		color: inherit;
		text-decoration: none;
		font-size: 12px;
	}
	a.pr-row { cursor: pointer; }
	a.pr-row:hover { background: var(--sidebar-accent-active); }
	a.pr-row:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.pr-icon { display: grid; place-items: center; flex: none; }
	.pr-num {
		flex: none;
		font-family: var(--font-mono);
		font-size: 11px;
		font-variant-numeric: tabular-nums;
		color: var(--muted-foreground);
	}
	.pr-title {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.pr-tag {
		flex: none;
		padding: 0 5px;
		border: 1px solid var(--border);
		border-radius: 999px;
		font-size: 10px;
		line-height: 15px;
		color: var(--muted-foreground);
	}
	.pr-state { flex: none; font-size: 11px; font-weight: 500; }
	.green { color: var(--success); }
	.purple { color: var(--status-merged); }
	.muted { color: var(--muted-foreground); }

	/* Phones: thumb-sized rows. */
	@media (hover: none) {
		.pr-row { min-height: 44px; font-size: 13px; }
	}

	@keyframes pr-pop-in {
		from { opacity: 0; transform: translateY(-3px); }
		to { opacity: 1; transform: none; }
	}
	@media (prefers-reduced-motion: reduce) {
		.pr-pop { animation: none; }
	}
</style>
