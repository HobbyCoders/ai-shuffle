<script lang="ts">
	/**
	 * HandoffCard — the one transcript row of a hand-off.
	 *
	 * The agent's HandOffSession call (or the owner's queued move) is the row;
	 * every later record of the same hand-off updates it in place instead of
	 * adding a row (see $lib/utils/handoff-episodes). It reads, in order:
	 * Starting hand-off to X → Sending to X → Handed off to X, or settles on why
	 * it did not.
	 *
	 * Signature: a three-stop route line (Queued · Sending · Arrived). The leg
	 * being travelled carries a moving dash; a settled line fills in the
	 * outcome's colour, and a failed one breaks at the stop where it ended.
	 */
	import type { HandoffEpisode } from '$lib/utils/handoff-episodes';

	interface Props {
		episode: HandoffEpisode;
	}

	let { episode }: Props = $props();

	const owner = $derived(episode.by === 'owner');
	const noun = $derived(owner ? 'Move' : 'Hand-off');
	const target = $derived(episode.worker || 'another worker');
	const timedOut = $derived(episode.reason === 'timeout');

	const title = $derived.by(() => {
		switch (episode.phase) {
			case 'starting':
				return owner ? `Starting move to ${target}` : `Starting hand-off to ${target}`;
			case 'sending':
				return `Sending to ${target}`;
			case 'done':
				return owner ? `Moved to ${target}` : `Handed off to ${target}`;
			case 'failed':
				return timedOut ? `${noun} to ${target} timed out` : `${noun} to ${target} failed`;
			case 'cancelled':
				return `${noun} to ${target} cancelled`;
			case 'refused':
				return episode.worker ? `${noun} to ${target} not queued` : `${noun} not queued`;
		}
	});

	/** The backend's sentence, without the part the title already says. */
	function detailOf(outcome: string | undefined): string {
		if (!outcome) return '';
		const lead = [
			`Handed off to ${target}.`,
			`${noun} to ${target} failed:`,
			`${noun} to ${target} cancelled:`,
			`${noun} to ${target} cancelled.`,
			`Queued move to ${target} cancelled.`
		].find((prefix) => outcome.startsWith(prefix));
		return (lead ? outcome.slice(lead.length) : outcome).trim();
	}

	const detail = $derived.by(() => {
		switch (episode.phase) {
			case 'starting':
				if (episode.cancelling) return 'Cancelling…';
				if (episode.requesting) return 'Queuing the move…';
				return episode.afterQueued
					? 'Waits for the queued messages, this turn and background work to finish.'
					: 'Waits for this turn and its background work to finish.';
			case 'sending':
				if (episode.cancelling) return 'Cancelling… (a move already under way finishes)';
				return 'Committing, pushing and moving this chat.';
			case 'cancelled':
				if (episode.reason === 'agent') return 'The agent cancelled it before it started.';
				if (episode.reason === 'restart') return 'The worker restarted before it could start.';
				if (episode.reason === 'replaced' && !episode.outcome) return 'A newer move replaced it.';
				return detailOf(episode.outcome);
			default:
				return detailOf(episode.outcome);
		}
	});

	type Tone = 'live' | 'done' | 'failed' | 'warn' | 'quiet';
	const tone: Tone = $derived(
		episode.phase === 'starting' || episode.phase === 'sending' ? 'live'
		: episode.phase === 'done' ? 'done'
		: episode.phase === 'failed' ? (timedOut ? 'warn' : 'failed')
		: 'quiet'
	);

	/** How far along the route the hand-off got: 0 queued, 1 sending, 2 arrived. */
	const reached = $derived(
		episode.phase === 'done' ? 2
		: episode.phase === 'sending' ? 1
		: episode.sent ? 1
		: 0
	);
	const settled = $derived(!(episode.phase === 'starting' || episode.phase === 'sending'));
	const stops = $derived([
		{ label: 'Queued' },
		{ label: 'Sending' },
		{ label: episode.phase === 'done' ? 'Arrived' : settled ? 'Stopped' : 'Arrives' }
	]);
	const stopState = (i: number): 'past' | 'here' | 'ahead' | 'broken' => {
		if (!settled) return i < reached ? 'past' : i === reached ? 'here' : 'ahead';
		if (episode.phase === 'done') return 'past';
		return i < reached ? 'past' : i === reached ? 'broken' : 'ahead';
	};
	const phaseLabel = $derived(
		episode.cancelling ? 'cancelling'
		: episode.phase === 'starting' ? 'queued'
		: episode.phase === 'sending' ? 'sending'
		: episode.phase === 'done' ? 'done'
		: episode.phase === 'refused' ? 'refused'
		: timedOut ? 'timed out' : episode.phase
	);
</script>

<div
	class="handoff"
	data-tone={tone}
	data-phase={episode.phase}
	data-testid="handoff-card"
	data-handoff-key={episode.key}
>
	<span class="glyph" aria-hidden="true">
		<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
			{#if episode.phase === 'done'}
				<path d="M3 10h9" /><path d="m9 6 4 4-4 4" /><rect x="14.5" y="4" width="3" height="12" rx="1.2" />
			{:else if tone === 'failed' || tone === 'warn'}
				<path d="M3 10h5" /><path d="M12 10h1.5" /><path d="m8.6 7.4 2.8 5.2" /><rect x="14.5" y="4" width="3" height="12" rx="1.2" />
			{:else if tone === 'quiet'}
				<path d="M3 10h10" stroke-dasharray="1.5 2.6" /><rect x="14.5" y="4" width="3" height="12" rx="1.2" />
			{:else}
				<circle cx="4.5" cy="10" r="2" /><path d="M7 10h6" /><path d="m11 7 3 3-3 3" /><rect x="15.5" y="5" width="2.5" height="10" rx="1" />
			{/if}
		</svg>
	</span>

	<div class="body">
		<div class="head">
			<!-- Polite live region: a phase change is announced once, as the title. -->
			<p class="title" role="status" aria-live="polite">
				{title}{#if !settled}<span class="ellipsis" aria-hidden="true"></span>{/if}
			</p>
			<span class="badge">{phaseLabel}</span>
		</div>

		{#if detail}
			<p class="detail">{detail}</p>
		{/if}

		{#if episode.note}
			<p class="note"><span class="note-label">Note</span><span class="note-text">{episode.note}</span></p>
		{/if}

		{#if episode.followUps.length > 0}
			<ul class="follow-ups">
				{#each episode.followUps as line, i (i)}
					<li>{line}</li>
				{/each}
			</ul>
		{/if}

		{#if episode.phase !== 'refused'}
			<div class="route" data-reached={reached}>
				<div class="track" aria-hidden="true">
					{#each stops as _stop, i (i)}
						{#if i > 0}
							{@const s = stopState(i)}
							<span class="leg" data-state={s === 'ahead' ? 'ahead' : s === 'here' ? 'travelling' : s === 'broken' ? 'broken' : 'past'}></span>
						{/if}
						<span class="dot" data-state={stopState(i)}></span>
					{/each}
				</div>
				<ol class="stops" aria-label="{noun} progress">
					{#each stops as stop, i (i)}
						<li class="stop" data-state={stopState(i)} aria-current={stopState(i) === 'here' ? 'step' : undefined}>{stop.label}</li>
					{/each}
				</ol>
			</div>
		{/if}
	</div>
</div>

<style>
	.handoff {
		--hc-accent: var(--primary);
		--hc-wash: color-mix(in oklch, var(--primary) 7%, transparent);
		--hc-edge: color-mix(in oklch, var(--primary) 55%, transparent);
		container-type: inline-size;
		position: relative;
		display: flex;
		gap: 12px;
		width: 100%;
		min-width: 0;
		margin: 4px 0;
		padding: 12px 14px 12px 12px;
		border: 1px solid var(--border);
		border-left: 2px solid var(--hc-edge);
		border-radius: 10px;
		background:
			linear-gradient(90deg, var(--hc-wash), transparent 62%),
			var(--card);
		transition: border-color 320ms ease-out, background-color 320ms ease-out;
	}

	.handoff[data-tone='done'] {
		--hc-accent: var(--success);
		--hc-wash: color-mix(in oklch, var(--success) 7%, transparent);
		--hc-edge: color-mix(in oklch, var(--success) 60%, transparent);
	}
	.handoff[data-tone='failed'] {
		--hc-accent: var(--destructive);
		--hc-wash: color-mix(in oklch, var(--destructive) 7%, transparent);
		--hc-edge: color-mix(in oklch, var(--destructive) 65%, transparent);
	}
	.handoff[data-tone='warn'] {
		--hc-accent: var(--warning);
		--hc-wash: color-mix(in oklch, var(--warning) 7%, transparent);
		--hc-edge: color-mix(in oklch, var(--warning) 60%, transparent);
	}
	.handoff[data-tone='quiet'] {
		--hc-accent: var(--muted-foreground);
		--hc-wash: transparent;
		--hc-edge: var(--border-strong, var(--border));
	}

	.glyph {
		flex: none;
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		color: var(--hc-accent);
		background: color-mix(in oklch, var(--hc-accent) 12%, transparent);
		box-shadow: inset 0 0 0 1px color-mix(in oklch, var(--hc-accent) 22%, transparent);
	}
	.glyph svg {
		width: 17px;
		height: 17px;
	}

	.body {
		flex: 1;
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 4px;
	}

	.head {
		display: flex;
		align-items: baseline;
		gap: 10px;
		min-width: 0;
	}

	.title {
		margin: 0;
		font-size: 13.5px;
		font-weight: 600;
		line-height: 1.35;
		letter-spacing: -0.005em;
		color: var(--foreground);
		min-width: 0;
		overflow-wrap: anywhere;
	}

	/* A trailing ellipsis that breathes while the hand-off is under way. */
	.ellipsis::after {
		content: '…';
		display: inline-block;
		margin-left: 1px;
		color: var(--hc-accent);
		animation: hc-breathe 1.6s ease-in-out infinite;
	}

	.badge {
		margin-left: auto;
		flex: none;
		font-family: var(--font-mono);
		font-size: 9.5px;
		font-weight: 600;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		padding: 2px 6px;
		border-radius: 999px;
		color: var(--hc-accent);
		background: color-mix(in oklch, var(--hc-accent) 13%, transparent);
	}

	.detail {
		margin: 0;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--muted-foreground);
		overflow-wrap: anywhere;
	}

	.note {
		margin: 2px 0 0;
		display: flex;
		gap: 8px;
		align-items: baseline;
		font-size: 12.5px;
		line-height: 1.5;
		color: var(--foreground);
		min-width: 0;
	}
	.note-label {
		flex: none;
		font-family: var(--font-mono);
		font-size: 9.5px;
		letter-spacing: 0.08em;
		text-transform: uppercase;
		color: var(--subtle-foreground, var(--muted-foreground));
	}
	.note-text {
		min-width: 0;
		overflow-wrap: anywhere;
		padding-left: 8px;
		border-left: 1px solid var(--border-strong, var(--border));
		color: color-mix(in oklch, var(--foreground) 88%, var(--muted-foreground));
	}

	.follow-ups {
		margin: 4px 0 0;
		padding: 6px 10px;
		list-style: none;
		border-radius: 6px;
		font-size: 12px;
		line-height: 1.5;
		color: var(--foreground);
		background: var(--status-warning-bg, color-mix(in oklch, var(--warning) 12%, transparent));
		border: 1px solid var(--status-warning-border, color-mix(in oklch, var(--warning) 26%, transparent));
	}
	.follow-ups li + li {
		margin-top: 4px;
	}

	/* ── The route line ──────────────────────────────────────────────── */
	.route {
		margin-top: 8px;
		max-width: 380px;
		display: flex;
		flex-direction: column;
		gap: 5px;
	}

	.track {
		display: flex;
		align-items: center;
		height: 9px;
	}

	.dot {
		flex: none;
		width: 9px;
		height: 9px;
		border-radius: 50%;
		background: var(--card);
		border: 1.5px solid var(--border-strong, var(--border));
		transition: background-color 280ms ease-out, border-color 280ms ease-out;
	}
	.dot[data-state='past'] {
		background: var(--hc-accent);
		border-color: var(--hc-accent);
	}
	.dot[data-state='here'] {
		border-color: var(--hc-accent);
		background: color-mix(in srgb, var(--hc-accent) 35%, var(--card));
		animation: hc-ping 1.8s ease-out infinite;
	}
	.dot[data-state='broken'] {
		border-color: var(--hc-accent);
		border-radius: 2px;
		transform: rotate(45deg) scale(0.9);
	}

	.leg {
		position: relative;
		flex: 1;
		height: 1.5px;
		margin: 0 3px;
		overflow: hidden;
		border-radius: 1px;
		background: var(--border-strong, var(--border));
		transition: background-color 280ms ease-out;
	}
	.leg[data-state='past'] { background: var(--hc-accent); }
	.leg[data-state='broken'] {
		background: repeating-linear-gradient(90deg, var(--hc-accent) 0 4px, transparent 4px 7px);
		opacity: 0.75;
	}
	.leg[data-state='travelling'] {
		background: color-mix(in oklch, var(--hc-accent) 22%, transparent);
	}
	.leg[data-state='travelling']::after {
		content: '';
		position: absolute;
		inset: 0 auto 0 0;
		width: 40%;
		background: linear-gradient(90deg, transparent, var(--hc-accent), transparent);
		animation: hc-travel 1.4s cubic-bezier(0.45, 0, 0.35, 1) infinite;
	}

	.stops {
		margin: 0;
		padding: 0;
		list-style: none;
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
	}
	.stop {
		font-family: var(--font-mono);
		font-size: 10px;
		letter-spacing: 0.04em;
		color: var(--subtle-foreground, var(--muted-foreground));
		white-space: nowrap;
	}
	.stop:nth-child(2) { text-align: center; }
	.stop:nth-child(3) { text-align: right; }
	.stop[data-state='past'] { color: var(--muted-foreground); }
	.stop[data-state='here'],
	.stop[data-state='broken'] {
		color: var(--hc-accent);
		font-weight: 600;
	}

	@keyframes hc-travel {
		from { transform: translateX(-100%); }
		to { transform: translateX(270%); }
	}
	@keyframes hc-ping {
		0% { box-shadow: 0 0 0 0 color-mix(in oklch, var(--hc-accent) 45%, transparent); }
		70%, 100% { box-shadow: 0 0 0 6px transparent; }
	}
	@keyframes hc-breathe {
		0%, 100% { opacity: 0.35; }
		50% { opacity: 1; }
	}

	/* Narrow columns (phones, split panes): the badge drops under the title,
	   and the route spans the full width. */
	@container (max-width: 360px) {
		.head {
			flex-wrap: wrap;
			row-gap: 4px;
		}
		.badge {
			margin-left: 0;
		}
		.route {
			max-width: none;
		}
	}

	@media (prefers-reduced-motion: reduce) {
		.ellipsis::after,
		.dot[data-state='here'],
		.leg[data-state='travelling']::after {
			animation: none;
		}
		.leg[data-state='travelling']::after {
			width: 100%;
			opacity: 0.55;
		}
		.handoff,
		.dot,
		.leg {
			transition: none;
		}
	}
</style>
