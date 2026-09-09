<script lang="ts">
	/**
	 * JoinNetworkPanel — join another worker's network with its invite.
	 *
	 * Paste the invite link (or the QR payload), or type the address and code
	 * by hand. The inviter admits this worker on contact; the parent polls
	 * `/network/status` and passes it in so the pending / syncing / failed
	 * states render here. With `replace` an enrolled worker leaves its own
	 * network for the invited one (the invited network's records win).
	 */
	import { Loader2, LogIn, ShieldAlert, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { cancelEnrollment, startEnrollment, type NetworkStatus } from '$lib/api/network';
	import { looksLikePairingLink, parseJoinLink } from '$lib/api/network-status';

	let { status = null, replace = false, deviceName = '', onStarted, onChanged }: {
		status?: NetworkStatus | null;
		replace?: boolean;
		deviceName?: string;
		onStarted?: () => void;
		onChanged?: () => void;
	} = $props();

	let input = $state('');
	let endpoint = $state('');
	let code = $state('');
	let busy = $state(false);
	let error = $state('');
	let confirmReplace = $state(false);

	const parsed = $derived(parseJoinLink(input));
	const pairingLinkPasted = $derived(looksLikePairingLink(input));
	const enrollment = $derived(status && !status.enrolled ? status.enrollment ?? null : null);
	const pending = $derived(enrollment?.status === 'pending' || enrollment?.status === 'syncing');
	const failed = $derived(enrollment !== null && !pending && enrollment.status !== 'complete');
	const ready = $derived(parsed ? Boolean(parsed.endpoint && parsed.code) : Boolean(endpoint.trim() && code.trim()));

	async function join() {
		if (!ready || busy) return;
		if (replace && !confirmReplace) { confirmReplace = true; return; }
		busy = true; error = '';
		try {
			const name = deviceName.trim() || undefined;
			const extra = { ...(name ? { device_name: name } : {}), ...(replace ? { replace: true } : {}) };
			await startEnrollment(parsed ? { link: input, ...extra } : { endpoint_url: endpoint, code, ...extra });
			input = ''; endpoint = ''; code = ''; confirmReplace = false;
			onStarted?.(); onChanged?.();
		} catch (e) {
			error = getErrorMessage(e, 'Could not join');
		} finally { busy = false; }
	}

	async function cancel() {
		busy = true; error = '';
		try { await cancelEnrollment(); onChanged?.(); }
		catch (e) { error = getErrorMessage(e, 'Could not cancel'); }
		finally { busy = false; }
	}
</script>

<div class="join" aria-label="Join with an invite">
	{#if pending && enrollment}
		<div class="waiting" role="status">
			<h5><Loader2 size={14} class="spin" /> {enrollment.status === 'syncing' ? 'Copying settings from' : 'Joining'} {enrollment.endpoint_url || 'the inviting worker'}…</h5>
			<p>The invite admits this worker on its own. If the other worker went offline, this finishes when it is back.</p>
			<button type="button" class="btn sm" onclick={cancel} disabled={busy}><X size={13} /> Cancel</button>
		</div>
	{:else}
		{#if failed && enrollment}
			<div class="fail" role="alert">
				<ShieldAlert size={14} />
				<span>The last join {enrollment.status}{enrollment.error ? `: ${enrollment.error}` : ''}.</span>
				<button type="button" class="btn sm" onclick={cancel} disabled={busy}>Dismiss</button>
			</div>
		{/if}
		<label class="field">
			<span>Invite link or code</span>
			<input class="inp mono" bind:value={input} placeholder="shuffle://network/join?… or ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" oninput={() => { if (!parsed && /^[A-Za-z0-9-\s]{12,15}$/.test(input.trim()) && !input.includes('://')) code = input.trim(); }} />
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
				<label class="field"><span>Worker address</span><input class="inp" bind:value={endpoint} placeholder="http://10.0.0.5:8000" autocomplete="off" spellcheck="false" /></label>
				<label class="field"><span>Code</span><input class="inp mono" bind:value={code} placeholder="ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" /></label>
			</div>
		{/if}
		{#if replace && confirmReplace}
			<div class="fail warn" role="alert">
				<ShieldAlert size={14} />
				<span>This worker leaves its current network. Settings the invited network already has replace this worker's copies; anything only this worker has is added to it.</span>
			</div>
		{/if}
		<div class="actions">
			<button type="button" class="btn primary" onclick={join} disabled={busy || !ready || pairingLinkPasted}>
				{#if busy}<Loader2 size={13} class="spin" />{:else}<LogIn size={13} />{/if}
				{replace ? (confirmReplace ? 'Leave and join' : 'Join another network') : 'Join'}
			</button>
			{#if replace && confirmReplace}<button type="button" class="btn" onclick={() => (confirmReplace = false)} disabled={busy}>Keep current network</button>{/if}
		</div>
	{/if}
	{#if error}<p class="error" role="alert">{error}</p>{/if}
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
