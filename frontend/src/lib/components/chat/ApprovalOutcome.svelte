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
	.outcome {
		--tone: var(--muted-foreground);
		display: flex;
		align-items: flex-start;
		gap: 8px;
		width: 100%;
		min-width: 0;
		font-size: 13px;
		line-height: 1.5;
		color: var(--muted-foreground);
	}

	.outcome[data-decision='approved'] { --tone: var(--success); }
	.outcome[data-decision='declined'] { --tone: var(--destructive); }
	.outcome[data-decision='asked_again'] { --tone: var(--warning); }

	.mark {
		flex: 0 0 18px;
		height: 18px;
		margin-top: 1px;
		display: inline-flex;
		align-items: center;
		justify-content: center;
		border-radius: 50%;
		background: color-mix(in oklch, var(--tone) 15%, transparent);
	}

	.mark svg {
		width: 12px;
		height: 12px;
		fill: none;
		stroke: var(--tone);
		stroke-width: 2;
		stroke-linecap: round;
		stroke-linejoin: round;
	}

	.text { min-width: 0; overflow-wrap: anywhere; }

	.verdict { color: var(--tone); font-weight: 600; }

	.outcome code {
		font-family: var(--font-mono);
		font-size: 12px;
		padding: 0 5px;
		border-radius: 5px;
		background: var(--muted);
	}

	.asker { white-space: nowrap; }
</style>
