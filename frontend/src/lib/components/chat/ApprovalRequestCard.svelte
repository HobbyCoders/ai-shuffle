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
	import '../deck/cards/card-design-system.css';
	import type { ApprovalCardData, ApprovalStatus } from '$lib/utils/assistant-confirm';
	import { GUARDRAIL_TOOL, guardrailView } from '$lib/utils/guardrail';

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

	// Shared card badges: the waiting request reads in the theme's primary,
	// an authorized one in success, and the rest stay neutral.
	const BADGE: Record<ApprovalStatus, string> = {
		pending: 'card-badge--muted',
		approved: 'card-badge--success',
		declined: 'card-badge--muted',
		expired: 'card-badge--muted',
		cancelled: 'card-badge--muted'
	};

	// A provider safety check reads as one, not as "use ModelGuardrail with {…}":
	// yes continues this turn on the suggested model, no stops it.
	const guardrail = $derived(
		data.guardrail
			? guardrailView({ tool_name: GUARDRAIL_TOOL, tool_input: data.guardrail as Record<string, unknown> })
			: null
	);
	const GUARDRAIL_STATUS: Partial<Record<ApprovalStatus, string>> = {
		approved: 'Continued',
		declined: 'Stopped'
	};

	const status = $derived<ApprovalStatus>(data.status in STATUS ? data.status : 'pending');
	const waiting = $derived(status === 'pending');
	const label = $derived(
		waiting
			? active
				? 'Waiting for you'
				: 'Next'
			: (guardrail && GUARDRAIL_STATUS[status]) || STATUS[status]
	);
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
	class="approval card-system"
	class:is-active={waiting && active}
	class:is-guardrail={!!guardrail}
	data-status={status}
	data-approval-card={data.request_id}
	data-permission-request={data.request_id}
	data-testid={guardrail ? 'guardrail-card' : undefined}
	role="group"
	aria-label={guardrail ? 'Safety check' : 'Approval request'}
>
	<div class="head">
		<span class="kicker">
			<svg class="icon" viewBox="0 0 16 16" aria-hidden="true">
				<path d="M8 1.5 2.75 3.6v4.1c0 3.2 2.2 5.6 5.25 6.8 3.05-1.2 5.25-3.6 5.25-6.8V3.6L8 1.5Z" />
				{#if guardrail}<path class="mark" d="M8 5v3.2M8 10.4v.1" />{/if}
			</svg>
			{#if guardrail}
				<span class="asker">Safety check{guardrail.category ? ` · ${guardrail.category}` : ''}</span>
			{:else if data.asker}
				<span class="asker" title={data.asker}>{data.asker}</span>
			{:else}
				<span class="asker">Approval needed</span>
			{/if}
		</span>
		{#if data.attempt > 1}
			<span class="retry card-badge card-badge--muted" title="An earlier request asked the same thing; this is a new request">
				Asked again · {ordinal(data.attempt)} request
			</span>
		{/if}
		<span class="status card-badge {waiting && active ? 'card-badge--primary' : BADGE[status]}" data-testid="approval-status">
			{#if waiting && active}<span class="pulse" aria-hidden="true"></span>{/if}
			{label}
		</span>
	</div>

	{#if guardrail}
		<p class="action g-title">{guardrail.heading}</p>
		{#if guardrail.explanation}
			<p class="g-why">{guardrail.explanation}</p>
		{/if}
		{#if guardrail.requestedLabel || guardrail.suggestedLabel}
			<dl class="g-models">
				{#if guardrail.requestedLabel}
					<div>
						<dt>Requested</dt>
						<dd class="g-req" title={guardrail.requestedModel}>{guardrail.requestedLabel}</dd>
					</div>
				{/if}
				{#if guardrail.suggestedLabel}
					<div>
						<dt>Suggested{guardrail.suggestedByApp ? ' by AI Shuffle' : ''}</dt>
						<dd class="g-sug" title={guardrail.suggestedModel}>{guardrail.suggestedLabel}</dd>
					</div>
				{/if}
			</dl>
		{/if}
		{#if guardrail.partialDiscarded}
			<p class="hint">The partial reply was discarded.</p>
		{/if}
	{:else if actionParts.length > 0}
		<p class="action">
			<span class="verb">Wants to</span>
			{#each actionParts as part, i (i)}
				{#if part.code}<code>{part.text}</code>{:else}{part.text}{/if}
			{/each}
		</p>
	{:else}
		<p class="action question">{data.question}</p>
	{/if}

	{#if guardrail && waiting && active}
		<p class="hint">
			Reply <strong>yes</strong> to continue this turn on {guardrail.suggestedLabel || 'the suggested model'},
			or <strong>no</strong> to stop. Your next message goes back to {guardrail.requestedLabel || 'the chat’s model'}.
		</p>
	{:else if waiting && active}
		<p class="hint">Reply <strong>yes</strong> or <strong>no</strong> in the message box. Other messages go to the agent and leave this open.</p>
	{:else if waiting}
		<p class="hint">Asked after the request before it. Answer that one first.</p>
	{/if}
</div>

<style>
	/* The chat's card language (QuestionTray, the private-access cards): one
	   neutral surface on the theme radius, a sentence-case card badge for the
	   state, and no colour wash. The request waiting for the owner's reply
	   carries the strong border, as a picked option does. */
	.approval {
		display: flex;
		flex-direction: column;
		gap: 0.375rem;
		width: 100%;
		max-width: 640px;
		min-width: 0;
		padding: 0.75rem;
		border: 1px solid var(--border);
		border-radius: var(--radius, 10px);
		background: var(--card);
		color: var(--card-foreground, var(--foreground));
		transition: border-color 0.2s ease-out, background-color 0.2s ease-out;
	}

	.approval.is-active {
		border-color: var(--border-strong, var(--border));
	}

	/* A settled request steps back so the one waiting stands out. */
	.approval:not([data-status='pending']) {
		background: transparent;
	}

	.head {
		display: flex;
		align-items: center;
		flex-wrap: wrap;
		gap: 0.375rem 0.5rem;
		min-width: 0;
	}

	.kicker {
		display: inline-flex;
		align-items: center;
		gap: 0.375rem;
		min-width: 0;
		flex: 1 1 auto;
		font-size: 0.75rem;
		font-weight: 500;
		color: var(--muted-foreground);
	}

	.icon {
		width: 14px;
		height: 14px;
		flex: 0 0 auto;
		fill: none;
		stroke: currentColor;
		stroke-width: 1.5;
		stroke-linejoin: round;
	}

	.asker {
		min-width: 0;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.card-badge {
		padding: 1px 6px;
	}

	.status {
		margin-left: auto;
	}

	.pulse {
		width: 6px;
		height: 6px;
		border-radius: 50%;
		background: currentColor;
		animation: approval-pulse 1.6s ease-in-out infinite;
	}

	@keyframes approval-pulse {
		0%, 100% { opacity: 1; }
		50% { opacity: 0.35; }
	}

	.action {
		margin: 0;
		font-size: 0.875rem;
		line-height: 1.5;
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.approval:not([data-status='pending']) .action {
		font-weight: 400;
		color: var(--muted-foreground);
	}

	.verb { color: var(--muted-foreground); font-weight: 400; }

	.action code {
		font-family: var(--font-mono);
		font-size: 0.75rem;
		padding: 1px 5px;
		border-radius: calc(var(--radius, 10px) - 6px);
		background: var(--muted);
		color: var(--foreground);
	}

	.hint {
		margin: 0;
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.hint strong { color: var(--foreground); font-weight: 600; }

	/* ── A provider safety check ─────────────────────────────────────────
	   Same card, with the theme's warning hue on the edge and the icon, so
	   it reads as "the provider stopped this", not as a tool asking. */
	.approval.is-guardrail {
		border-left: 3px solid var(--warning);
		padding-left: calc(0.75rem - 2px);
	}

	.approval.is-guardrail:not([data-status='pending']) {
		border-left-color: color-mix(in oklab, var(--warning) 45%, var(--border));
	}

	.is-guardrail .kicker { color: var(--foreground); }
	.is-guardrail .icon { stroke: var(--warning); }
	.icon .mark { stroke-linecap: round; stroke-width: 1.75; }

	.g-title { font-weight: 600; }

	.g-why {
		margin: 0;
		font-size: 0.8125rem;
		line-height: 1.5;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}

	.g-models {
		display: flex;
		flex-wrap: wrap;
		align-items: flex-end;
		gap: 0.25rem 0.75rem;
		margin: 0.125rem 0 0;
	}

	.g-models > div {
		display: flex;
		flex-direction: column;
		gap: 1px;
		min-width: 0;
	}

	/* The arrow between Requested and Suggested; CSS keeps the <dl> valid. */
	.g-models > div + div {
		position: relative;
		padding-left: 1.25rem;
	}

	.g-models > div + div::before {
		content: '→';
		position: absolute;
		left: 0;
		bottom: 0;
		color: var(--muted-foreground);
		font-size: 0.875rem;
		line-height: 1.5;
	}

	.g-models dt {
		font-size: 0.6875rem;
		color: var(--muted-foreground);
	}

	.g-models dd {
		margin: 0;
		font-size: 0.875rem;
		line-height: 1.5;
		font-weight: 500;
		overflow-wrap: anywhere;
	}

	.g-req {
		color: var(--muted-foreground);
		text-decoration: line-through;
		text-decoration-color: color-mix(in oklab, var(--muted-foreground) 60%, transparent);
	}

	.g-sug { color: var(--foreground); }

	.approval:not([data-status='pending']) .g-sug { color: var(--muted-foreground); }

	@media (prefers-reduced-motion: reduce) {
		.approval { transition: none; }
		.pulse { animation: none; }
	}
</style>
