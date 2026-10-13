<script lang="ts">
	/**
	 * JoinNetworkPanel — join another worker's network with its invite, as the
	 * setup wizard shows it. The logic is JoinFlow, which Settings > Devices >
	 * This worker > Network shares.
	 *
	 * Paste the invite link (or the QR payload), or type the address and code
	 * by hand. The inviter admits this worker on contact; the parent polls
	 * `/network/status` and passes it in so the pending / syncing / failed
	 * states render here. With `replace` an enrolled worker leaves its own
	 * network for the invited one (the invited network's records win).
	 */
	import { Loader2, LogIn, ShieldAlert, X } from 'lucide-svelte';
	import type { NetworkStatus } from '$lib/api/network';
	import { JoinFlow } from '$lib/components/settings/devices/join-flow.svelte';

	let { status = null, replace = false, deviceName = '', onStarted, onChanged }: {
		status?: NetworkStatus | null;
		replace?: boolean;
		deviceName?: string;
		onStarted?: () => void;
		onChanged?: () => void;
	} = $props();

	const flow = new JoinFlow({
		status: () => status,
		replace: () => replace,
		deviceName: () => deviceName,
		onStarted: () => onStarted?.(),
		onChanged: () => onChanged?.()
	});
	const parsed = $derived(flow.parsed);
	const pairingLinkPasted = $derived(flow.pairingLinkPasted);
	const enrollment = $derived(flow.enrollment);
	const pending = $derived(flow.pending);
	const failed = $derived(flow.failed);
	const ready = $derived(flow.ready);
	const busy = $derived(flow.busy);
	const confirmReplace = $derived(flow.confirmReplace);
</script>

<div class="join" aria-label="Join with an invite">
	{#if pending && enrollment}
		<div class="waiting" role="status">
			<h5><Loader2 size={14} class="spin" /> {enrollment.status === 'syncing' ? 'Copying settings from' : 'Joining'} {enrollment.endpoint_url || 'the inviting worker'}…</h5>
			<p>The invite admits this worker on its own. If the other worker went offline, this finishes when it is back.</p>
			<button type="button" class="btn sm" onclick={() => flow.cancel()} disabled={busy}><X size={13} /> Cancel</button>
		</div>
	{:else}
		<p>Invite codes are for a worker on your home network or a VPN such as Tailscale; workers signed in to the same Shuffle account find each other and always talk over the encrypted tunnel.</p>
		{#if failed && enrollment}
			<div class="fail" role="alert">
				<ShieldAlert size={14} />
				<span>The last join {enrollment.status}{enrollment.error ? `: ${enrollment.error}` : ''}.</span>
				<button type="button" class="btn sm" onclick={() => flow.cancel()} disabled={busy}>Dismiss</button>
			</div>
		{/if}
		<label class="field">
			<span>Invite link or code</span>
			<input class="inp mono" bind:value={flow.input} placeholder="shuffle://network/join?… or ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" oninput={() => flow.inputChanged()} />
		</label>
		{#if pairingLinkPasted}
			<p class="dim error">That is a phone or browser pairing link. On this worker use the <strong>Worker</strong> invite from the other worker's Add device panel.</p>
		{:else if parsed}
			<div class="parsed">
				<div><span class="k">Worker</span><code>{parsed.endpoint || '—'}</code></div>
				<div><span class="k">Code</span><code>{parsed.code || '—'}</code></div>
			</div>
		{:else}
			<div class="manual">
				<label class="field"><span>Worker address</span><input class="inp" bind:value={flow.endpoint} placeholder="http://10.0.0.5:8000" autocomplete="off" spellcheck="false" /></label>
				<label class="field"><span>Code</span><input class="inp mono" bind:value={flow.code} placeholder="ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" /></label>
			</div>
		{/if}
		{#if replace && confirmReplace}
			<div class="fail warn" role="alert">
				<ShieldAlert size={14} />
				<span>This worker leaves its current network. Settings the invited network already has replace this worker's copies; anything only this worker has is added to it.</span>
			</div>
		{/if}
		<div class="actions">
			<button type="button" class="btn primary" onclick={() => flow.join()} disabled={busy || !ready || pairingLinkPasted}>
				{#if busy}<Loader2 size={13} class="spin" />{:else}<LogIn size={13} />{/if}
				{replace ? (confirmReplace ? 'Leave and join' : 'Join another network') : 'Join'}
			</button>
			{#if replace && confirmReplace}<button type="button" class="btn" onclick={() => flow.keepCurrent()} disabled={busy}>Keep current network</button>{/if}
		</div>
	{/if}
	{#if flow.error}<p class="error" role="alert">{flow.error}</p>{/if}
</div>

<style>
	.join { display: flex; flex-direction: column; gap: 10px; color: var(--foreground); }
	h5 { display: flex; align-items: center; gap: 6px; margin: 0 0 6px; font-size: 13px; font-weight: 600; }
	p { margin: 0; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.waiting { padding: 12px; border: 1px solid var(--border); border-radius: 10px; background: color-mix(in oklch, var(--primary) 5%, var(--card)); }
	.waiting p { margin-bottom: 8px; }
	.fail { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 10px 12px; border: 1px solid color-mix(in oklch, var(--destructive) 40%, var(--border)); border-radius: 10px; font-size: 12px; color: var(--destructive); }
	.fail.warn { color: var(--foreground); border-color: color-mix(in oklch, var(--warning, orange) 50%, var(--border)); }
	.field { display: block; }
	.field span { display: block; margin-bottom: 4px; font-size: 12px; color: var(--muted-foreground); }
	.inp { box-sizing: border-box; width: 100%; min-height: 36px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 13px; }
	.inp.mono { font-family: var(--font-mono); font-size: 12px; }
	.inp:focus-visible, .btn:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.manual { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 8px; }
	.parsed { display: grid; gap: 4px; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; font-size: 12px; }
	.parsed div { display: flex; gap: 8px; align-items: baseline; }
	.parsed .k { flex: 0 0 60px; color: var(--muted-foreground); }
	.parsed code { font-family: var(--font-mono); font-size: 12px; overflow-wrap: anywhere; }
	.actions { display: flex; flex-wrap: wrap; gap: 8px; }
	.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; cursor: pointer; }
	.btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn:disabled { opacity: .55; cursor: default; }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.btn.sm { min-height: 32px; padding: 4px 10px; }
	.error { color: var(--destructive); }
	.dim.error { color: var(--destructive); }
	:global(.join .spin) { animation: join-spin 1s linear infinite; }
	@keyframes join-spin { to { transform: rotate(360deg); } }
	@media (max-width: 520px) { .manual { grid-template-columns: 1fr; } .btn, .inp { min-height: 44px; } }
</style>
