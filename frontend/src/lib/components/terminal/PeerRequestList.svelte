<script lang="ts">
	/**
	 * Terminal requests from other workers, on the worker that would run the
	 * shell (#196). The owner compares the six-character code with the one the
	 * requesting worker shows, then approves one interactive shell here, or
	 * denies it. Approved shells can be revoked from here at any time.
	 *
	 * Approval is a decision on this worker only: the requesting worker cannot
	 * approve on its own behalf. The code is a comparison aid, not authority.
	 */
	import { onMount } from 'svelte';
	import { MonitorSmartphone, ShieldCheck } from 'lucide-svelte';
	import { peerTerminals } from '$lib/stores/peer-terminals.svelte';
	import { age, countdown, PENDING_SECONDS } from '$lib/terminal/peer';
	import PeerCode from './PeerCode.svelte';

	let { workerName }: { workerName: string } = $props();

	onMount(() => peerTerminals.watch());

	const list = $derived(peerTerminals.requests);
	/** Per request: the owner ticked "the code matches". Never carried to a different request. */
	let matched = $state<Record<string, boolean>>({});
	const titleId = (id: string) => `peer-req-${id.replace(/[^a-z0-9_-]/gi, '_')}`;
</script>

{#if list.length || peerTerminals.error}
	<section class="peer-requests" aria-label="Terminal requests from other workers">
		{#if peerTerminals.error && !list.length}
			<p class="err" role="alert">{peerTerminals.error}</p>
		{/if}
		{#each list as request (request.request_id)}
			{@const timing = peerTerminals.timing(request)}
			{@const decision = peerTerminals.decision(request.request_id)}
			{@const notice = peerTerminals.notice(request.request_id)}
			<article class="req" class:approved={request.status === 'approved'} aria-labelledby={titleId(request.request_id)}>
				<header class="req-head">
					<span class="glyph" aria-hidden="true">
						{#if request.status === 'approved'}<ShieldCheck size={16} />{:else}<MonitorSmartphone size={16} />{/if}
					</span>
					<div class="req-title">
						{#if request.status === 'approved'}
							<h4 id={titleId(request.request_id)}>{request.used ? `${request.source_name} has a shell on ${workerName}` : `Approved for ${request.source_name}`}</h4>
							<p class="meta">{request.used ? 'Open now' : 'Waiting for it to open'} · ends in <time>{countdown(timing.left)}</time> · Revoke closes it</p>
						{:else}
							<h4 id={titleId(request.request_id)}>{request.source_name} wants a shell on {workerName}</h4>
							<p class="meta">Requested {age(timing.age)}</p>
						{/if}
					</div>
					{#if request.status === 'approved'}
						<button type="button" class="ghost danger head-action" disabled={!!decision}
							onclick={() => peerTerminals.revoke(request.request_id, 'revoke')}>{decision === 'revoke' ? 'Revoking…' : 'Revoke'}</button>
					{/if}
				</header>

				{#if request.status !== 'approved'}
					<div class="req-body">
						<PeerCode code={request.confirmation_code} left={timing.left} total={PENDING_SECONDS} label={`Code from ${request.source_name}`} />
						<p class="why">Approve only if <strong>{request.source_name}</strong> shows this same code. Approving opens <strong>one</strong> interactive shell on {workerName} for up to 10 minutes; you can revoke it here at any time.</p>
						<label class="match">
							<input type="checkbox" checked={!!matched[request.request_id]} disabled={!!decision}
								onchange={(e) => (matched = { ...matched, [request.request_id]: (e.currentTarget as HTMLInputElement).checked })} />
							<span>The code matches on {request.source_name}</span>
						</label>
					</div>
				{/if}

				{#if notice}<p class="err" role="alert">{notice}</p>{/if}

				{#if request.status !== 'approved'}
				<div class="req-actions">
						<button type="button" class="ghost" disabled={!!decision}
							onclick={() => peerTerminals.revoke(request.request_id, 'deny')}>{decision === 'deny' ? 'Denying…' : 'Deny'}</button>
						<button type="button" class="go" disabled={!!decision || !matched[request.request_id]}
							aria-describedby={matched[request.request_id] ? undefined : `${titleId(request.request_id)}-hint`}
							onclick={() => peerTerminals.approve(request.request_id)}>{decision === 'approve' ? 'Approving…' : 'Approve one shell'}</button>
						{#if !matched[request.request_id]}<span class="hint" id={`${titleId(request.request_id)}-hint`}>Compare the code first</span>{/if}
				</div>
				{/if}
			</article>
		{/each}
	</section>
{/if}

<style>
	.peer-requests { display: grid; grid-auto-rows: max-content; gap: 8px; padding: 10px; max-height: 60%; overflow-y: auto; flex-shrink: 0; overscroll-behavior: contain;
		border-bottom: 1px solid var(--border); background: color-mix(in oklab, var(--primary) 4%, var(--background)); font-family: var(--font-sans); }
	.req { position: relative; display: grid; gap: 10px; padding: 12px 14px; border: 1px solid color-mix(in oklab, var(--primary) 30%, var(--border));
		border-radius: var(--radius); background: var(--card); animation: req-in 0.24s cubic-bezier(0.2, 0.8, 0.2, 1); }
	.req::before { content: ''; position: absolute; inset: -1px auto -1px -1px; width: 3px; border-radius: var(--radius) 0 0 var(--radius); background: var(--primary); }
	.req.approved { border-color: color-mix(in oklab, var(--success) 30%, var(--border)); }
	.req.approved::before { background: var(--success); }
	.req-head { display: flex; gap: 10px; align-items: flex-start; }
	.glyph { display: grid; place-items: center; width: 30px; height: 30px; flex-shrink: 0; border-radius: 8px; color: var(--primary);
		background: color-mix(in oklch, var(--primary) 12%, transparent); }
	.approved .glyph { color: var(--success); background: color-mix(in oklch, var(--success) 12%, transparent); }
	.req-title { flex: 1; min-width: 0; }
	.head-action { flex-shrink: 0; align-self: center; }
	h4 { margin: 0; font-size: 13px; font-weight: 600; line-height: 1.35; color: var(--foreground); overflow-wrap: anywhere; }
	.meta { margin: 2px 0 0; font-size: 11.5px; color: var(--muted-foreground); }
	.meta time { font-family: var(--font-mono); font-variant-numeric: tabular-nums; color: var(--foreground); }
	.req-body { display: grid; gap: 10px; }
	.why { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin: 0; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); }
	.req-body .why { display: block; }
	.why strong { color: var(--foreground); font-weight: 600; }
	.match { display: flex; align-items: center; gap: 8px; width: fit-content; min-height: 32px; padding: 0 10px 0 8px; border-radius: calc(var(--radius) - 4px);
		border: 1px solid var(--border-strong); font-size: 12.5px; color: var(--foreground); cursor: pointer; transition: border-color 0.15s, background-color 0.15s; }
	.match:hover { background: var(--hover-overlay); }
	.match:has(input:checked) { border-color: color-mix(in oklch, var(--primary) 60%, transparent); background: color-mix(in oklch, var(--primary) 9%, transparent); }
	.match:has(input:focus-visible) { outline: 2px solid var(--ring); outline-offset: 2px; }
	.match input { width: 15px; height: 15px; margin: 0; accent-color: var(--primary); }
	.req-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
	.hint { order: -1; margin-right: auto; font-size: 11.5px; color: var(--subtle-foreground); }
	button { min-height: 32px; padding: 0 14px; border-radius: calc(var(--radius) - 4px); font: 500 12.5px var(--font-sans); cursor: pointer;
		transition: background-color 0.15s ease-out, opacity 0.15s; }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:active:not(:disabled) { transform: translateY(1px); }
	button:disabled { opacity: 0.45; cursor: not-allowed; }
	.ghost { border: 1px solid var(--border-strong); background: transparent; color: var(--foreground); }
	.ghost:hover:not(:disabled) { background: var(--hover-overlay); }
	.ghost.danger { color: var(--destructive); border-color: color-mix(in oklab, var(--destructive) 45%, var(--border-strong)); }
	.ghost.danger:hover:not(:disabled) { background: color-mix(in oklch, var(--destructive) 10%, transparent); }
	.go { border: 1px solid var(--primary); background: var(--primary); color: var(--primary-foreground); }
	.go:hover:not(:disabled) { background: color-mix(in oklch, var(--primary) 88%, white); }
	.err { margin: 0; font-size: 12px; color: var(--destructive); }
	@keyframes req-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
	/* Narrow screens: tighter cards and a taller list, so a pending card and an
	   approved one (with its Revoke) both fit without scrolling. */
	@media (max-width: 700px) {
		.peer-requests { max-height: 75%; padding: 8px; gap: 6px; }
		.req { gap: 8px; padding: 10px 12px; }
		.req-body { gap: 8px; }
		button, .match { min-height: 44px; }
		.req-actions button { flex: 1 1 auto; }
		.hint { flex-basis: 100%; }
	}
	@media (prefers-reduced-motion: reduce) { .req { animation: none; } }
</style>
