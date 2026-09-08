<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { Check, Copy, Plus, QrCode, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		createPairingGrant, listPairingEndpoints, revokePairingGrant,
		type CreatedPairingGrant, type PairingEndpoint
	} from '$lib/api/pairing';

	let endpoints = $state<PairingEndpoint[]>([]);
	let selectedEndpoint = $state('');
	let activeGrant = $state<CreatedPairingGrant | null>(null);
	let loading = $state(true);
	let creating = $state(false);
	let cancelling = $state(false);
	let endpointError = $state('');
	let error = $state('');
	let expired = $state(false);
	let copied = $state<'code' | 'link' | null>(null);
	let now = $state(Date.now());
	let timer: ReturnType<typeof setInterval> | undefined;
	let copyTimer: ReturnType<typeof setTimeout> | undefined;
	const secondsLeft = $derived(activeGrant ? Math.max(0, Math.ceil((new Date(activeGrant.grant.expires_at).getTime() - now) / 1000)) : 0);

	onMount(() => {
		timer = setInterval(() => {
			now = Date.now();
			if (activeGrant && new Date(activeGrant.grant.expires_at).getTime() <= now) {
				activeGrant = null; expired = true;
			}
		}, 1000);
		void load();
	});
	onDestroy(() => { clearInterval(timer); clearTimeout(copyTimer); });

	async function load() {
		loading = true;
		try {
			endpoints = await listPairingEndpoints();
			if (!endpoints.some((endpoint) => endpoint.url === selectedEndpoint)) {
				selectedEndpoint = endpoints.find((endpoint) => endpoint.qr_eligible)?.url ?? endpoints[0]?.url ?? '';
			}
			endpointError = '';
		} catch (e) { endpointError = getErrorMessage(e, 'Could not load pairing addresses'); }
		finally { loading = false; }
	}

	async function create(purpose: 'controller' | 'runner') {
		if (creating || cancelling) return;
		creating = true; error = ''; expired = false; copied = null;
		try {
			const endpoint = endpoints.find((item) => item.url === selectedEndpoint);
			activeGrant = await createPairingGrant(purpose === 'controller' && endpoint?.qr_eligible ? endpoint.url : null, undefined, purpose);
			now = Date.now();
		} catch (e) { error = getErrorMessage(e, 'Could not create pairing code'); }
		finally { creating = false; }
	}

	async function cancelGrant() {
		if (!activeGrant || cancelling) return;
		const grant = activeGrant;
		cancelling = true; error = '';
		try {
			await revokePairingGrant(grant.grant.id);
			if (activeGrant?.grant.id === grant.grant.id) activeGrant = null;
		} catch (e) { error = getErrorMessage(e, 'Could not cancel pairing code'); }
		finally { cancelling = false; }
	}

	async function copy(value: string, kind: 'code' | 'link') {
		try {
			await navigator.clipboard.writeText(value);
			copied = kind;
			clearTimeout(copyTimer);
			copyTimer = setTimeout(() => { copied = null; }, 1800);
		} catch { error = 'Clipboard access is unavailable. Select and copy the value manually.'; }
	}
</script>

<section class="pair-panel" aria-label="Add device">
	{#if loading}
		<p class="pair-hint" role="status">Loading pairing options…</p>
	{:else if activeGrant}
		<div class="grant">
			<div class="grant-main">
				<h4>{activeGrant.grant.purpose === 'runner' ? 'Worker enrollment code' : 'One-time pairing code'}</h4>
				<div class="code-line">
					<code>{activeGrant.display_code}</code>
					<button type="button" class="pair-button copy-code" aria-label="Copy code" title="Copy code" onclick={() => copy(activeGrant!.display_code, 'code')}>{#if copied === 'code'}<Check size={15} />{:else}<Copy size={15} />{/if}</button>
				</div>
				<p class="pair-hint">Expires in {Math.floor(secondsLeft / 60)}:{String(secondsLeft % 60).padStart(2, '0')} · Use once</p>
				{#if activeGrant.grant.purpose === 'runner'}
					<p class="pair-hint">On the worker configured for this database, run:</p>
					<button type="button" class="pair-button command" aria-label="Copy enrollment command" onclick={() => copy(`python run_local.py --join ${activeGrant!.display_code}`, 'link')}>
						{#if copied === 'link'}<Check size={14} /> Copied{:else}<Copy size={14} /><code>python run_local.py --join {activeGrant.display_code}</code>{/if}
					</button>
				{:else if activeGrant.pairing_url}
					<p class="pair-hint">Scan with your phone, or open the link in another browser.</p>
					<button type="button" class="pair-button" aria-label="Copy pairing link" onclick={() => copy(activeGrant!.pairing_url!, 'link')}>{#if copied === 'link'}<Check size={14} /> Copied{:else}<Copy size={14} /> Copy pairing link{/if}</button>
				{:else}
					<p class="pair-hint">Enter this code in a phone or browser already configured for this server.</p>
				{/if}
				<div><button type="button" class="pair-button cancel" disabled={cancelling} onclick={cancelGrant}><X size={14} /> {cancelling ? 'Cancelling…' : 'Cancel code'}</button></div>
			</div>
			{#if activeGrant.qr_svg}<div class="qr" aria-label="Pairing QR code">{@html activeGrant.qr_svg}</div>{/if}
		</div>
	{:else}
		<p class="pair-hint">Pair a phone or browser, or enroll a worker on this database.</p>
		<div class="create-actions">
			<button type="button" class="pair-button primary" disabled={creating} onclick={() => create('controller')}><QrCode size={15} /> Pair phone or browser</button>
			<button type="button" class="pair-button" disabled={creating} onclick={() => create('runner')}><Plus size={15} /> Enroll worker</button>
		</div>
		{#if creating}<p class="pair-hint" role="status">Creating code…</p>{/if}
		<details class="pair-connection">
			<summary>Pairing address</summary>
			<label for="pair-endpoint">Address for the pairing link</label>
			<select id="pair-endpoint" bind:value={selectedEndpoint} disabled={creating}>
				{#each endpoints as endpoint (endpoint.url)}<option value={endpoint.url}>{endpoint.label} · {endpoint.url}{endpoint.loopback ? ' (code only)' : ''}</option>{/each}
				{#if !endpoints.length}<option value="">Code only — no address detected</option>{/if}
			</select>
		</details>
	{/if}
	{#if endpointError}<p class="pair-error" role="alert">{endpointError}. You can still create a code without an address.</p><button type="button" class="pair-button" disabled={loading} onclick={load}>Retry pairing addresses</button>{/if}
	{#if error}<p class="pair-error" role="alert">{error}</p>{/if}
	{#if expired}<p class="pair-hint" role="status">Pairing code expired. Create a new code.</p>{/if}
</section>

<style>
	.pair-panel { padding: 14px 18px; border-bottom: 1px solid var(--border); background: color-mix(in oklch, var(--primary) 3%, var(--card)); color: var(--foreground); }
	h4 { margin: 0 0 8px; font-family: var(--font-sans); font-size: 13px; font-weight: 600; }
	.pair-hint, .pair-error { margin: 0 0 10px; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.pair-error { color: var(--destructive); margin-top: 10px; }
	.pair-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; cursor: pointer; }
	.pair-button:hover:not(:disabled) { background: var(--hover-overlay); }
	.pair-button:focus-visible, select:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.pair-button:disabled { opacity: .55; cursor: default; }
	.pair-button.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.create-actions, .code-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
	.pair-connection { margin-top: 6px; }
	summary { width: fit-content; padding: 12px 0; min-height: 44px; box-sizing: border-box; color: var(--muted-foreground); font-size: 12px; cursor: pointer; }
	label { display: block; margin-bottom: 6px; font-size: 12px; }
	select { box-sizing: border-box; width: 100%; min-width: 0; min-height: 40px; padding: 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 12px; }
	.grant { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 16px; }
	.grant-main { flex: 1 1 220px; min-width: 0; }
	.code-line { margin-bottom: 4px; }
	.code-line code { font-family: var(--font-mono); font-size: 21px; line-height: 1.5; letter-spacing: .04em; user-select: all; overflow-wrap: anywhere; }
	.copy-code { width: 40px; padding: 0; }
	.command { justify-content: flex-start; text-align: left; max-width: 100%; }
	.command code { font-family: var(--font-mono); font-size: 11px; white-space: normal; overflow-wrap: anywhere; }
	.command :global(svg) { flex-shrink: 0; }
	.cancel { margin-top: 8px; }
	.qr { width: 164px; max-width: 100%; box-sizing: border-box; padding: 10px; background: white; border-radius: 8px; }
	.qr :global(svg) { display: block; width: 100%; height: auto; }
	@media (max-width: 480px) { .pair-panel { padding: 12px; } .pair-button, select { min-height: 44px; } .copy-code { width: 44px; } }
</style>
