<script lang="ts">
	/**
	 * ApprovalOutcome — what the owner's yes or no did, right after that answer
	 * (#484). The backend writes it after the owner's message, so the transcript
	 * reads request card, owner's answer, outcome. It names the request it
	 * settled (`request_id`, and the action in words), so with several requests
	 * open the outcome is never read against the wrong one.
	 */
	interface Props {
		data: Record<string, unknown> | undefined;
		content?: string;
	}

	let { data, content = '' }: Props = $props();

	const decision = $derived(typeof data?.decision === 'string' ? data.decision : '');
	const action = $derived(typeof data?.action === 'string' ? data.action : '');
	const asker = $derived(typeof data?.asker === 'string' ? data.asker : '');
	const question = $derived(typeof data?.question === 'string' ? data.question : content);
	const requestId = $derived(typeof data?.request_id === 'string' ? data.request_id : '');

	const VERDICT: Record<string, string> = {
		approved: 'Authorized',
		declined: 'Declined',
		asked_again: 'Not a firm yes or no'
	};
	const verdict = $derived(VERDICT[decision] ?? 'Answered');
	const parts = $derived(
		(action || question).split(/(`[^`]+`)/g).filter(Boolean).map((part) =>
			part.length > 1 && part.startsWith('`') && part.endsWith('`')
				? { code: true, text: part.slice(1, -1) }
				: { code: false, text: part }
		)
	);
</script>

<div class="outcome" data-decision={decision} data-request-id={requestId} role="note">
	<span class="mark" aria-hidden="true">
		{#if decision === 'approved'}
			<svg viewBox="0 0 16 16"><path d="m3.5 8.5 3 3 6-7" /></svg>
		{:else if decision === 'declined'}
			<svg viewBox="0 0 16 16"><path d="m4.5 4.5 7 7m0-7-7 7" /></svg>
		{:else}
			<svg viewBox="0 0 16 16"><path d="M8 4.5v4.5m0 2.5v.01" /></svg>
		{/if}
	</span>
	<span class="text">
		<strong class="verdict">{verdict}</strong>
		{#if decision === 'asked_again'}
			<span class="detail">— reply yes or no to</span>
		{/if}
		<span class="detail">
			{#each parts as part, i (i)}
				{#if part.code}<code>{part.text}</code>{:else}{part.text}{/if}
			{/each}
		</span>
		{#if asker}<span class="asker">· {asker}</span>{/if}
	</span>
</div>

<style>
	/* Reads as a transcript row: the tool rows' inset, icon size and type
	   scale, with the state carried by the glyph's colour. */
	.outcome {
		--tone: var(--subtle-foreground, var(--muted-foreground));
		display: flex;
		align-items: flex-start;
		gap: 9px;
		width: 100%;
		min-width: 0;
		padding: 6px 8px;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}

	.outcome[data-decision='approved'] { --tone: var(--success); }
	.outcome[data-decision='declined'] { --tone: var(--destructive); }

	.mark {
		flex: 0 0 14px;
		height: 14px;
		margin-top: 3px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
	}

	.mark svg {
		width: 14px;
		height: 14px;
		fill: none;
		stroke: var(--tone);
		stroke-width: 2;
		stroke-linecap: round;
		stroke-linejoin: round;
	}

	.text { min-width: 0; overflow-wrap: anywhere; }

	.verdict { color: var(--foreground); font-weight: 600; }

	.outcome code {
		font-family: var(--font-mono);
		font-size: 11.5px;
		padding: 0 4px;
		border-radius: calc(var(--radius, 10px) - 6px);
		background: var(--muted);
	}

	.asker { white-space: nowrap; }
</style>
