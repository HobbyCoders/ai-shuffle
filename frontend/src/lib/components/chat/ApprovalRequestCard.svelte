<script lang="ts">
	/**
	 * ApprovalRequestCard — one request asked in words (`assistant_confirm`), at
	 * the point in the transcript where it was raised (#483).
	 *
	 * The owner answers by typing or saying yes or no; the app decides. This card
	 * is the only place the request is shown: the model is told not to repeat it,
	 * and nothing floats after the transcript. It is keyed by request id and its
	 * status is settled in place by the durable `interaction.*` events, so it
	 * keeps its position and its outcome live, after a reload, and when several
	 * jobs ask at once. `active` marks the one the owner's next reply answers
	 * (the oldest still waiting); others that wait read "Next".
	 *
	 * No buttons, deliberately: the decision comes from the owner's own message.
	 */
	import type { ApprovalCardData, ApprovalStatus } from '$lib/utils/assistant-confirm';

	interface Props {
		data: ApprovalCardData;
		/** The request the owner's next yes or no answers. */
		active?: boolean;
	}

	let { data, active = false }: Props = $props();

	const STATUS: Record<ApprovalStatus, string> = {
		pending: 'Waiting',
		approved: 'Authorized',
		declined: 'Declined',
		expired: 'Expired',
		cancelled: 'Cancelled'
	};

	const status = $derived<ApprovalStatus>(data.status in STATUS ? data.status : 'pending');
	const waiting = $derived(status === 'pending');
	const label = $derived(waiting ? (active ? 'Waiting for you' : 'Next') : STATUS[status]);
	// "run `ls -la`" → plain and code parts; the server's wording uses backticks.
	const actionParts = $derived(splitCode(data.action || ''));

	function splitCode(value: string): { code: boolean; text: string }[] {
		if (!value) return [];
		return value.split(/(`[^`]+`)/g).filter(Boolean).map((part) =>
			part.length > 1 && part.startsWith('`') && part.endsWith('`')
				? { code: true, text: part.slice(1, -1) }
				: { code: false, text: part }
		);
	}

	function ordinal(n: number): string {
		const tail = n % 100 >= 11 && n % 100 <= 13 ? 'th' : (['th', 'st', 'nd', 'rd'][n % 10] ?? 'th');
		return `${n}${tail}`;
	}
</script>

<div
	class="approval"
	class:is-active={waiting && active}
	data-status={status}
	data-approval-card={data.request_id}
	data-permission-request={data.request_id}
	role="group"
	aria-label="Approval request"
>
	<span class="rail" aria-hidden="true"></span>
	<div class="body">
		<div class="head">
			<span class="kicker">
				<svg class="icon" viewBox="0 0 16 16" aria-hidden="true">
					<path d="M8 1.5 2.75 3.6v4.1c0 3.2 2.2 5.6 5.25 6.8 3.05-1.2 5.25-3.6 5.25-6.8V3.6L8 1.5Z" />
				</svg>
				{#if data.asker}
					<span class="asker" title={data.asker}>{data.asker}</span>
				{:else}
					<span class="asker">Approval needed</span>
				{/if}
			</span>
			{#if data.attempt > 1}
				<span class="retry" title="An earlier request asked the same thing; this is a new request">
					Asked again · {ordinal(data.attempt)} request
				</span>
			{/if}
			<span class="status" data-testid="approval-status">
				{#if waiting && active}<span class="pulse" aria-hidden="true"></span>{/if}
				{label}
			</span>
		</div>

		{#if actionParts.length > 0}
			<p class="action">
				<span class="verb">Wants to</span>
				{#each actionParts as part, i (i)}
					{#if part.code}<code>{part.text}</code>{:else}{part.text}{/if}
				{/each}
			</p>
		{:else}
			<p class="action question">{data.question}</p>
		{/if}

		{#if waiting && active}
			<p class="hint">Reply <strong>yes</strong> or <strong>no</strong> in the message box. Other messages go to the agent and leave this open.</p>
		{:else if waiting}
			<p class="hint">Asked after the request before it. Answer that one first.</p>
		{/if}
	</div>
</div>

<style>
	.approval {
		--approval-tone: var(--muted-foreground);
		position: relative;
		display: flex;
		width: 100%;
		max-width: 640px;
		min-width: 0;
		border: 1px solid var(--border);
		border-radius: 12px;
		background: var(--card);
		color: var(--card-foreground, var(--foreground));
		overflow: hidden;
		transition: border-color 0.25s ease-out, opacity 0.25s ease-out, background-color 0.25s ease-out;
	}

	.approval[data-status='pending'] { --approval-tone: var(--warning); }
	.approval[data-status='approved'] { --approval-tone: var(--success); }
	.approval[data-status='declined'] { --approval-tone: var(--destructive); }

	.approval.is-active {
		border-color: color-mix(in oklch, var(--warning) 45%, var(--border));
		background: color-mix(in oklch, var(--warning) 5%, var(--card));
	}

	/* A settled request steps back so the one waiting stands out. */
	.approval:not([data-status='pending']) { opacity: 0.88; }

	.rail {
		flex: 0 0 3px;
		background: var(--approval-tone);
	}

	.body {
		flex: 1 1 auto;
		min-width: 0;
		padding: 10px 14px 11px;
		display: flex;
		flex-direction: column;
		gap: 6px;
	}

	.head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 6px 10px;
		min-width: 0;
	}

	.kicker {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
		flex: 1 1 auto;
		font-size: 12px;
		font-weight: 600;
		letter-spacing: 0.01em;
		color: var(--muted-foreground);
	}

	.icon {
		width: 14px;
		height: 14px;
		flex: 0 0 auto;
		fill: none;
		stroke: var(--approval-tone);
		stroke-width: 1.5;
		stroke-linejoin: round;
	}

	.asker {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.retry {
		font-size: 11px;
		font-weight: 600;
		padding: 1px 8px;
		border-radius: 999px;
		border: 1px dashed color-mix(in oklch, var(--primary) 55%, var(--border));
		color: var(--primary);
		white-space: nowrap;
	}

	.status {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		margin-left: auto;
		font-size: 11px;
		font-weight: 700;
		letter-spacing: 0.06em;
		text-transform: uppercase;
		padding: 2px 8px;
		border-radius: 999px;
		color: var(--approval-tone);
		background: color-mix(in oklch, var(--approval-tone) 13%, transparent);
		white-space: nowrap;
	}

	.approval[data-status='expired'] .status,
	.approval[data-status='cancelled'] .status {
		color: var(--muted-foreground);
		background: var(--muted);
	}

	.pulse {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: currentColor;
		animation: approval-pulse 1.6s ease-in-out infinite;
	}

	@keyframes approval-pulse {
		0%, 100% { opacity: 1; transform: scale(1); }
		50% { opacity: 0.35; transform: scale(0.7); }
	}

	.action {
		margin: 0;
		font-size: 14px;
		line-height: 1.55;
		overflow-wrap: anywhere;
	}

	.verb { color: var(--muted-foreground); }

	.action code {
		font-family: var(--font-mono);
		font-size: 12.5px;
		padding: 1px 6px;
		border-radius: 6px;
		background: var(--muted);
		border: 1px solid var(--border-subtle, var(--border));
	}

	.hint {
		margin: 0;
		font-size: 12.5px;
		color: var(--muted-foreground);
	}

	.hint strong { color: var(--foreground); font-weight: 600; }

	@media (max-width: 480px) {
		.body { padding: 9px 12px 10px; }
		.status { margin-left: 0; }
	}

	@media (prefers-reduced-motion: reduce) {
		.approval { transition: none; }
		.pulse { animation: none; }
	}
</style>
