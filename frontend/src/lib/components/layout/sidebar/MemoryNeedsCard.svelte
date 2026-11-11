<script lang="ts">
	/**
	 * The memory-health item in the sidebar's "Needs you" lane (#1042): one
	 * row while the worker's memory check reports attention, in a session
	 * row's anatomy (state column with the amber Needs you diamond, a title,
	 * one line of detail), so the lane keeps one visual language. It has no
	 * dismiss or swipe: it follows the check and goes when that is healthy.
	 * Selecting it opens Settings > System at Memory health.
	 */
	import { platform } from '$lib/stores/platform.svelte';

	interface Props {
		/** The first check's name and reason ("Capacity: …"), one line. */
		detail: string;
		onOpen: () => void;
	}

	let { detail, onOpen }: Props = $props();
</script>

<div class="mcard-frame" class:phone={platform.isMobile} data-needs-item="memory-health">
	<button type="button" class="mcard" title={detail} onclick={onOpen}>
		<span class="lead" aria-hidden="true"><span class="pip"></span></span>
		<span class="body">
			<span class="l1"><span class="title">Memory needs attention</span></span>
			<span class="l2"><span class="detail">{detail}</span></span>
		</span>
		<span class="sr-only">Opens Settings, System, Memory health.</span>
	</button>
</div>

<style>
	/* The same measures as SessionCard's active row (6 + 20 + 17 + 6, phone
	   8 + 22 + 19 + 8), so it lines up with the chats above it. */
	.mcard-frame {
		width: 100%;
		border-radius: var(--radius-control);
		overflow: hidden;
	}
	.mcard {
		display: grid;
		grid-template-columns: var(--state-col) minmax(0, 1fr);
		column-gap: 6px;
		align-items: start;
		width: 100%;
		padding: 6px 8px;
		border: none;
		border-radius: var(--radius-control);
		background: var(--sidebar);
		color: inherit;
		font: inherit;
		text-align: left;
		cursor: pointer;
		transition: background-color 120ms ease;
		--state-col: 16px;
		--line-1: 20px;
		--line-2: 17px;
	}
	.mcard:hover { background: var(--row-hover); }
	.mcard:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }

	.lead {
		display: grid;
		place-items: center;
		width: var(--state-col);
		height: var(--line-1);
	}
	/* Needs you: the amber diamond the waiting chats carry. */
	.pip {
		width: 8px;
		height: 8px;
		border-radius: 1.5px;
		background: var(--warning);
		forced-color-adjust: none;
		transform: rotate(45deg);
		animation: mcard-pulse 2.4s ease-in-out infinite;
	}
	@keyframes mcard-pulse {
		0%, 100% { transform: rotate(45deg) scale(1); }
		50% { transform: rotate(45deg) scale(0.82); }
	}

	.body { display: block; min-width: 0; }
	.l1,
	.l2 {
		display: flex;
		min-width: 0;
		white-space: nowrap;
	}
	.l1 {
		height: var(--line-1);
		line-height: var(--line-1);
	}
	.l2 {
		height: var(--line-2);
		line-height: var(--line-2);
		font-size: var(--fs-label);
		color: var(--subtle-foreground);
	}
	.title,
	.detail {
		flex: 1 1 auto;
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.title {
		font-size: 13.5px;
		font-weight: 450;
		color: var(--sidebar-foreground);
	}

	.mcard-frame.phone .mcard {
		column-gap: 8px;
		padding: 8px 12px 8px 10px;
		--line-1: 22px;
		--line-2: 19px;
	}
	.mcard-frame.phone .title { font-size: 14.5px; }
	.mcard-frame.phone .l2 { font-size: 13px; }

	@media (prefers-reduced-motion: reduce) {
		.pip { animation: none; }
		.mcard { transition: none; }
	}
</style>
