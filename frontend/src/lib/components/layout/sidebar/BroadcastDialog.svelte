<script lang="ts" module>
	/** One chat the broadcast can reach, as the sidebar sees it right now. */
	export interface BroadcastCandidate {
		id: string;
		title: string;
		state: 'working' | 'parked' | 'idle' | 'needs' | 'offline';
		/** The worker running it when that is not this one. */
		worker: string | null;
	}

	/** Chats waiting on you, or on a worker that is offline, start unticked:
	 *  the message would only sit there until you answer or it reconnects. */
	export const tickedByDefault = (c: BroadcastCandidate) => c.state !== 'needs' && c.state !== 'offline';
</script>

<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	/**
	 * "Message all active chats": one message to every chat in Needs you and
	 * Active, parked ones included, on every worker. Each chat gets it as a
	 * normal send (a working chat has it steered into its running turn). A chip
	 * per chat; click one to leave it out or add it back. After sending, the
	 * chips become a receipt per chat.
	 */
	import { Megaphone, X, Plus, Check, AlertTriangle, Loader2 } from 'lucide-svelte';
	import { focusTrap } from '$lib/components/ui/focus-trap';
	import { broadcastMessage, MAX_BROADCAST_CHATS, type BroadcastResult } from '$lib/api/broadcast';
	import type { ApiError } from '$lib/api/client';
	import { broadcastDialog } from '$lib/stores/broadcast-dialog.svelte';

	interface Props {
		open: boolean;
		candidates: BroadcastCandidate[];
		onClose: () => void;
	}

	let { open, candidates, onClose }: Props = $props();

	// The draft is kept in the store so it outlives this component: see there.
	// `picked` holds only the chips the user changed; everything else follows its
	// default, so a chat that joins Active while the dialog is open is ticked.
	const draft = broadcastDialog.draft;
	let doneButton = $state<HTMLButtonElement | null>(null);

	const included = (c: BroadcastCandidate) => draft.picked[c.id] ?? tickedByDefault(c);
	const recipients = $derived(candidates.filter(included));
	// The server takes a bounded list; say so here rather than have it refuse.
	const tooMany = $derived(recipients.length > MAX_BROADCAST_CHATS);
	const canSend = $derived(!draft.sending && recipients.length > 0 && !tooMany && draft.message.trim().length > 0);

	const summary = $derived.by(() => {
		const count = (state: BroadcastCandidate['state']) => candidates.filter((c) => c.state === state).length;
		const parts = [`${candidates.length} ${candidates.length === 1 ? 'chat' : 'chats'}`];
		const working = count('working');
		const parked = count('parked');
		const needs = count('needs');
		const offline = count('offline');
		const remote = candidates.filter((c) => c.worker).length;
		if (working) parts.push(`${working} working`);
		if (parked) parts.push(`${parked} parked`);
		if (needs) parts.push(`${needs} waiting on you`);
		if (remote) parts.push(`${remote} on other workers`);
		if (offline) parts.push(`${offline} offline`);
		return parts.join(' · ');
	});

	// The Send button is gone once receipts show: keep focus inside the dialog.
	$effect(() => {
		if (draft.receipts && doneButton) doneButton.focus();
	});

	function toggle(c: BroadcastCandidate) {
		draft.picked = { ...draft.picked, [c.id]: !included(c) };
	}

	function close() {
		if (draft.sending) return;
		onClose();
	}

	// Writes land in the store, so a send still on its way when this dialog
	// unmounts shows its receipts in the one that replaces it.
	async function send() {
		if (!canSend) return;
		const chats = recipients.map((c) => ({ ...c }));
		draft.sending = true;
		draft.error = null;
		try {
			const answer = await broadcastMessage(chats.map((c) => c.id), draft.message.trim());
			const byId = new Map(answer.results.map((r) => [r.session_id, r]));
			draft.receipts = chats.map((chat) => ({
				chat,
				result: byId.get(chat.id) ?? { session_id: chat.id, status: 'failed', detail: 'No answer for this chat' }
			}));
		} catch (e) {
			const { status, detail } = (e ?? {}) as Partial<ApiError>;
			// A 4xx refused the whole request before any chat got it. Anything else
			// (no connection, a lost answer, a server error) may have come after
			// some chats took the message, and a resend would give them a second copy.
			draft.error = typeof status === 'number' && status >= 400 && status < 500
				? detail || 'Could not send. Nothing was sent.'
				: 'Could not confirm the send. Some chats may already have it: check them before sending again.';
		} finally {
			draft.sending = false;
		}
	}

	function onKeydown(e: KeyboardEvent) {
		if (!open) return;
		if (e.key === 'Escape') {
			e.preventDefault();
			e.stopPropagation();
			close();
		} else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !draft.receipts) {
			e.preventDefault();
			void send();
		}
	}

	const STATE_WORD: Record<BroadcastCandidate['state'], string> = {
		working: 'Working',
		parked: 'Parked',
		idle: 'Idle',
		needs: 'Waiting on you',
		offline: 'Offline'
	};

	/** What the chat's intake said. It reports admission only, not whether a
	 *  working chat steered it in or queued it for after the turn. */
	function receiptWord(result: BroadcastResult): string {
		if (result.status === 'failed') return 'Not sent';
		if (result.status === 'maybe_sent') return 'May have been sent';
		if (result.status === 'held') return 'Held until its move finishes';
		return 'Sent';
	}

	const sentCount = $derived(draft.receipts?.filter((r) => r.result.status === 'sent' || r.result.status === 'held').length ?? 0);

	function portal(node: HTMLElement) {
		document.body.appendChild(node);
		return { destroy: () => node.remove() };
	}
</script>

<svelte:window onkeydown={onKeydown} />

{#if open}
	<div
		use:portal
		use:focusTrap={{ initial: 'textarea' }}
		class="ui-overlay ui-surface"
		role="dialog"
		aria-modal="true"
		aria-labelledby="broadcast-title"
		tabindex="-1"
	>
		<button class="ui-scrim" type="button" tabindex="-1" aria-label="Close" onclick={close}></button>
		<div class="ui-dialog bc">
			<header class="ui-dialog-header">
				<div class="head">
					<h2 id="broadcast-title" class="ui-dialog-title"><Megaphone size={16} strokeWidth={2} />Message all active chats</h2>
					<p class="ui-dialog-subtitle">
						{#if draft.receipts}
							Sent to {sentCount} of {draft.receipts.length} {draft.receipts.length === 1 ? 'chat' : 'chats'}
						{:else}
							{summary}
						{/if}
					</p>
				</div>
				<button class="ui-button icon ghost" type="button" aria-label="Close" onclick={close} disabled={draft.sending}>
					<X size={16} strokeWidth={2} />
				</button>
			</header>

			<div class="ui-dialog-body">
				{#if draft.receipts}
					<ul class="receipts" aria-label="What each chat did">
						{#each draft.receipts as { chat, result } (chat.id)}
							<li class="receipt" data-status={result.status}>
								<span class="mark" aria-hidden="true">
									{#if result.status === 'failed' || result.status === 'maybe_sent'}
										<AlertTriangle size={14} strokeWidth={2} />
									{:else}
										<Check size={14} strokeWidth={2.25} />
									{/if}
								</span>
								<span class="r-title" title={chat.title}>{chat.title}</span>
								<span class="r-word">{receiptWord(result)}</span>
								{#if result.detail && result.status !== 'sent'}
									<span class="r-detail">{result.detail}</span>
								{/if}
							</li>
						{/each}
					</ul>
				{:else if candidates.length === 0}
					<p class="empty">No active chats to message.</p>
				{:else}
					<div class="chips" role="group" aria-label="Recipients">
						{#each candidates as c (c.id)}
							{@const on = included(c)}
							<button
								class="chip"
								class:off={!on}
								type="button"
								aria-pressed={on}
								title={`${c.title} · ${STATE_WORD[c.state]}${c.worker ? ` · ${c.worker}` : ''}`}
								onclick={() => toggle(c)}
							>
								<span class="dot {c.state}" aria-hidden="true"></span>
								<span class="c-title">{c.title}</span>
								<span class="sr-only">, {STATE_WORD[c.state]}{c.worker ? `, on ${c.worker}` : ''}</span>
								<span class="c-act" aria-hidden="true">
									{#if on}<X size={12} strokeWidth={2.25} />{:else}<Plus size={12} strokeWidth={2.25} />{/if}
								</span>
							</button>
						{/each}
					</div>
					<textarea
						bind:value={draft.message}
						class="ui-input msg"
						rows="4"
						placeholder="One message for every ticked chat"
						aria-label="Message"
						disabled={draft.sending}
					></textarea>
					<p class="hint">
						A working chat gets it mid-turn, or after its turn when it can't take it sooner. Chats waiting on you or on an offline worker start unticked; click a chip to add or remove it.
					</p>
					{#if tooMany}<p class="err">One message can go to {MAX_BROADCAST_CHATS} chats at most. Untick {recipients.length - MAX_BROADCAST_CHATS}.</p>{/if}
					{#if draft.error}<p class="err" role="alert">{draft.error}</p>{/if}
				{/if}
			</div>

			<div class="ui-dialog-footer">
				{#if draft.receipts}
					<button class="ui-button primary" type="button" data-broadcast-done bind:this={doneButton} onclick={close}>Done</button>
				{:else}
					<button class="ui-button" type="button" onclick={close} disabled={draft.sending}>Cancel</button>
					<button class="ui-button primary" type="button" onclick={send} disabled={!canSend}>
						{#if draft.sending}<Loader2 size={14} class="spin" />{/if}
						Send to {recipients.length} {recipients.length === 1 ? 'chat' : 'chats'}
					</button>
				{/if}
			</div>
		</div>
	</div>
{/if}

<style>
	.bc { --ui-dialog-width: 560px; }
	.head { min-width: 0; }
	.ui-dialog-title { display: flex; align-items: center; gap: 8px; }
	.ui-dialog-title :global(svg) { color: var(--primary); flex: none; }
	.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 4px 0 12px; max-height: 196px; overflow-y: auto; }
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		max-width: 100%;
		min-height: 28px;
		padding: 3px 6px 3px 9px;
		border: 1px solid var(--border);
		border-radius: 999px;
		background: var(--muted);
		color: var(--foreground);
		font: inherit;
		font-size: 12.5px;
		cursor: pointer;
	}
	.chip:hover { border-color: var(--border-strong); }
	.chip.off { background: transparent; border-style: dashed; color: var(--muted-foreground); }
	.chip.off .dot { opacity: 0.55; }
	.c-title { min-width: 0; max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.c-act { display: inline-flex; color: var(--subtle-foreground); }
	.dot { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--subtle-foreground); }
	.dot.working { background: var(--primary); }
	.dot.parked { background: var(--status-merged); }
	.dot.idle { background: transparent; box-shadow: inset 0 0 0 1.5px var(--subtle-foreground); }
	.dot.needs { background: var(--warning); }
	.dot.offline { background: var(--subtle-foreground); }
	.msg { resize: vertical; min-height: 92px; line-height: 1.45; }
	.hint { margin: 8px 0 0; font-size: 12px; color: var(--muted-foreground); }
	.err { margin: 8px 0 0; font-size: 12.5px; color: var(--destructive); }
	.empty { margin: 4px 0 8px; color: var(--muted-foreground); }
	.receipts { list-style: none; margin: 4px 0 0; padding: 0; display: flex; flex-direction: column; gap: 2px; max-height: 50dvh; overflow-y: auto; }
	.receipt {
		display: grid;
		grid-template-columns: 18px minmax(0, 1fr) auto;
		align-items: center;
		column-gap: 8px;
		padding: 6px 2px;
		border-bottom: 1px solid var(--border);
	}
	.receipt:last-child { border-bottom: 0; }
	.mark { display: inline-flex; color: var(--success); }
	.receipt[data-status='failed'] .mark { color: var(--destructive); }
	.receipt[data-status='maybe_sent'] .mark { color: var(--warning); }
	.r-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	.r-word { font-size: 12px; color: var(--muted-foreground); white-space: nowrap; }
	.r-detail { grid-column: 2 / -1; font-size: 12px; color: var(--muted-foreground); }
	.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
	.ui-button :global(.spin) { animation: bc-spin 0.9s linear infinite; }
	@keyframes bc-spin { to { transform: rotate(360deg); } }
	@media (max-width: 600px) {
		.chip { min-height: 36px; font-size: 13.5px; }
		.c-title { max-width: 60vw; }
		.r-word { white-space: normal; text-align: right; }
	}
	@media (prefers-reduced-motion: reduce) { .ui-button :global(.spin) { animation: none; } }
</style>
