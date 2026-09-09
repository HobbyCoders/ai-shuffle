<script lang="ts">
	import { copyText, COPY_ERROR_MESSAGE } from '$lib/utils/clipboard';
	/**
	 * AddDevicePanel — one code for one device.
	 *
	 * "Worker" mints a network invite (QR + link + code) that another worker
	 * scans or pastes to join this worker's network. "Phone or browser" mints a
	 * pairing code the Shuffle app scans or a browser opens. Presenting the
	 * code is the authorization: nobody approves anything afterwards.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { Check, Copy, Monitor, QrCode, Smartphone, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { createDeviceGrant, listPairingEndpoints, revokePairingGrant, type DeviceGrant, type PairingEndpoint } from '$lib/api/pairing';
	import { formatCountdown, secondsUntil } from '$lib/api/network-status';

	let { enrolled = false, allowLegacyRunner = false, onCreated }: {
		/** Enrolled workers can invite other workers; an unenrolled one only pairs phones and browsers. */
		enrolled?: boolean;
		/** Legacy shared-database runner enrollment (code only). */
		allowLegacyRunner?: boolean;
		onCreated?: (grant: DeviceGrant) => void;
	} = $props();

	type Kind = 'worker' | 'client' | 'runner';
	let kind = $state<Kind>('client');
	let endpoints = $state<PairingEndpoint[]>([]);
	let selectedEndpoint = $state('');
	let grant = $state<DeviceGrant | null>(null);
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

	const secondsLeft = $derived(grant ? secondsUntil(grant.grant.expires_at, now) : 0);
	const usableEndpoints = $derived(endpoints.filter((endpoint) => !endpoint.loopback));
	const link = $derived(grant?.join?.url ?? grant?.pairing_url ?? null);
	const address = $derived(endpoints.find((endpoint) => endpoint.url === selectedEndpoint) ?? null);

	onMount(() => {
		kind = enrolled ? 'worker' : 'client';
		timer = setInterval(() => {
			now = Date.now();
			if (grant && new Date(grant.grant.expires_at).getTime() <= now) { grant = null; expired = true; }
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
		} catch (e) { endpointError = getErrorMessage(e, 'Could not load this worker\'s addresses'); }
		finally { loading = false; }
	}

	function choose(next: Kind) {
		if (creating) return;
		kind = next; error = ''; expired = false;
	}

	async function create() {
		if (creating || cancelling) return;
		if (kind === 'worker' && !enrolled) return;
		if (kind === 'runner' && !allowLegacyRunner) return;
		creating = true; error = ''; expired = false; copied = null;
		try {
			const endpoint = kind === 'runner' ? null : (address && !address.loopback ? address.url : null);
			if (kind === 'worker' && !endpoint) throw new Error('Choose an address other workers can reach.');
			grant = await createDeviceGrant(kind === 'worker' ? 'network:join' : kind === 'runner' ? 'runner' : 'controller', endpoint);
			now = Date.now();
			onCreated?.(grant);
		} catch (e) { error = getErrorMessage(e, 'Could not create the code'); }
		finally { creating = false; }
	}

	async function cancel() {
		if (!grant || cancelling) return;
		const current = grant;
		cancelling = true; error = '';
		try {
			await revokePairingGrant(current.grant.id);
			if (grant?.grant.id === current.grant.id) grant = null;
		} catch (e) { error = getErrorMessage(e, 'Could not cancel the code'); }
		finally { cancelling = false; }
	}

	async function copy(value: string, what: 'code' | 'link') {
		try {
			await copyText(value);
			copied = what;
			clearTimeout(copyTimer);
			copyTimer = setTimeout(() => { copied = null; }, 1800);
		} catch { copied = null; error = COPY_ERROR_MESSAGE; }
	}
</script>

<section class="add-device" aria-label="Add device">
	{#if loading}
		<p class="hint" role="status">Loading…</p>
	{:else if grant}
		<div class="grant">
			<div class="grant-main">
				<h4>{grant.grant.purpose === 'network:join' ? 'Worker invite' : grant.grant.purpose === 'runner' ? 'Worker enrollment code' : 'Phone or browser code'}</h4>
				<p class="hint">
					{#if grant.grant.purpose === 'network:join'}
						On the other worker choose <strong>Join with an invite</strong> during setup or in Settings → Devices, then scan this code or paste the link. It joins immediately.
					{:else if grant.grant.purpose === 'runner'}
						On the worker configured for this database, run the command below.
					{:else if link}
						Scan with the Shuffle app, or open the link in the browser you want to pair. It connects immediately.
					{:else}
						Enter this code in the Shuffle app or in a browser already pointed at this worker.
					{/if}
				</p>
				<div class="code-line">
					<code>{grant.display_code}</code>
					<button type="button" class="btn icon" aria-label="Copy code" title="Copy code" onclick={() => copy(grant!.display_code, 'code')}>{#if copied === 'code'}<Check size={15} />{:else}<Copy size={15} />{/if}</button>
				</div>
				<p class="hint">Expires in {formatCountdown(secondsLeft)} · works once</p>
				<div class="actions">
					{#if grant.grant.purpose === 'runner'}
						<button type="button" class="btn command" aria-label="Copy enrollment command" onclick={() => copy(`python run_local.py --join ${grant!.display_code}`, 'link')}>
							{#if copied === 'link'}<Check size={14} /> Copied{:else}<Copy size={14} /><code>python run_local.py --join {grant.display_code}</code>{/if}
						</button>
					{:else if link}
						<button type="button" class="btn" aria-label={grant.grant.purpose === 'network:join' ? 'Copy invite link' : 'Copy pairing link'} onclick={() => copy(link!, 'link')}>{#if copied === 'link'}<Check size={14} /> Copied{:else}<Copy size={14} /> {grant.grant.purpose === 'network:join' ? 'Copy invite link' : 'Copy pairing link'}{/if}</button>
					{/if}
					<button type="button" class="btn subtle" disabled={cancelling} onclick={cancel}><X size={14} /> {cancelling ? 'Cancelling…' : 'Cancel code'}</button>
				</div>
			</div>
			{#if grant.qr_svg}<div class="qr" aria-label={grant.grant.purpose === 'network:join' ? 'Worker invite QR code' : 'Pairing QR code'}>{@html grant.qr_svg}</div>{/if}
		</div>
	{:else}
		<div class="kinds" role="tablist" aria-label="Device type">
			{#if enrolled}
				<button type="button" role="tab" class="kind" class:active={kind === 'worker'} aria-selected={kind === 'worker'} onclick={() => choose('worker')}><Monitor size={15} /> Worker</button>
			{/if}
			<button type="button" role="tab" class="kind" class:active={kind === 'client'} aria-selected={kind === 'client'} onclick={() => choose('client')}><Smartphone size={15} /> Phone or browser</button>
			{#if allowLegacyRunner}
				<button type="button" role="tab" class="kind" class:active={kind === 'runner'} aria-selected={kind === 'runner'} onclick={() => choose('runner')}><Monitor size={15} /> Shared-database worker</button>
			{/if}
		</div>
		<p class="hint">
			{#if kind === 'worker'}
				Another computer running Shuffle joins this worker's network: it gets your settings, profiles and provider keys, and every device can see and drive sessions on either worker. The invite carries this worker's address, so the other worker must be able to reach it (same network, Tailscale, or a public address).
			{:else if kind === 'runner'}
				Enroll a worker that shares this worker's database. Code only; no address is embedded.
			{:else}
				Give a phone the Shuffle app, or a browser on another computer, control of this worker. For your phone, signing in to the same Shuffle account on both works too.
			{/if}
		</p>
		{#if kind !== 'runner'}
			<label class="field">
				<span>Address the device will use</span>
				<select bind:value={selectedEndpoint} disabled={creating}>
					{#each endpoints as endpoint (endpoint.url)}<option value={endpoint.url}>{endpoint.label} · {endpoint.url}{endpoint.loopback ? ' (this computer only)' : ''}</option>{/each}
					{#if !endpoints.length}<option value="">No address detected</option>{/if}
				</select>
			</label>
			{#if kind === 'worker' && usableEndpoints.length === 0}
				<p class="error" role="alert">No address other computers can reach was detected. Set one under Advanced → This worker's address.</p>
			{/if}
		{/if}
		<div class="actions">
			<button type="button" class="btn primary" disabled={creating || (kind === 'worker' && usableEndpoints.length === 0)} onclick={create}>
				<QrCode size={15} /> {creating ? 'Creating…' : kind === 'worker' ? 'Create worker invite' : kind === 'runner' ? 'Create enrollment code' : 'Create pairing code'}
			</button>
		</div>
	{/if}
	{#if endpointError}<p class="error" role="alert">{endpointError}. You can still create a code without an address.</p><button type="button" class="btn" disabled={loading} onclick={load}>Retry addresses</button>{/if}
	{#if error}<p class="error" role="alert">{error}</p>{/if}
	{#if expired}<p class="hint" role="status">The code expired. Create a new one.</p>{/if}
</section>

<style>
	.add-device { padding: 14px 18px; border-bottom: 1px solid var(--border); background: color-mix(in oklch, var(--primary) 3%, var(--card)); color: var(--foreground); }
	h4 { margin: 0 0 6px; font-family: var(--font-sans); font-size: 13px; font-weight: 600; }
	.hint, .error { margin: 0 0 10px; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.error { color: var(--destructive); }
	.kinds { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
	.kind { display: inline-flex; align-items: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 999px; background: transparent; color: var(--muted-foreground); font: inherit; font-size: 12px; cursor: pointer; }
	.kind.active { border-color: var(--primary); background: color-mix(in oklch, var(--primary) 12%, transparent); color: var(--foreground); }
	.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; cursor: pointer; }
	.btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn:focus-visible, select:focus-visible, .kind:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.btn:disabled { opacity: .55; cursor: default; }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.btn.subtle { border-color: transparent; color: var(--muted-foreground); }
	.btn.icon { width: 36px; padding: 0; }
	.command { justify-content: flex-start; text-align: left; max-width: 100%; }
	.command code { font-family: var(--font-mono); font-size: 11px; white-space: normal; overflow-wrap: anywhere; }
	.actions, .code-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
	.field { display: block; margin-bottom: 10px; }
	.field span { display: block; margin-bottom: 4px; font-size: 12px; color: var(--muted-foreground); }
	select { box-sizing: border-box; width: 100%; min-width: 0; min-height: 36px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 12px; }
	.grant { display: flex; align-items: flex-start; flex-wrap: wrap; gap: 16px; }
	.grant-main { flex: 1 1 240px; min-width: 0; }
	.code-line { margin-bottom: 4px; }
	.code-line code { font-family: var(--font-mono); font-size: 21px; line-height: 1.5; letter-spacing: .04em; user-select: all; overflow-wrap: anywhere; }
	.qr { width: 164px; max-width: 100%; box-sizing: border-box; padding: 10px; background: white; border-radius: 8px; }
	.qr :global(svg) { display: block; width: 100%; height: auto; }
	@media (max-width: 480px) { .add-device { padding: 12px; } .btn, select, .kind { min-height: 44px; } .btn.icon { width: 44px; } }
</style>
