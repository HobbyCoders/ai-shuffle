<script lang="ts">
	import Banner from '$lib/components/Banner.svelte';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { Link2, ShieldCheck, Smartphone } from 'lucide-svelte';
	import { exchangePairingGrant } from '$lib/api/pairing';
	import { getErrorMessage } from '$lib/api/client';
	import { auth, isAdmin } from '$lib/stores/auth.svelte';
	import { desktop } from '$lib/services/desktop';

	const DEVICE_ID_KEY = 'shuffle-paired-device-id';
	let code = $state('');
	let deviceName = $state('');
	let connecting = $state(false);
	let error = $state('');
	let linkSupplied = $state(false);

	function platformName(): string {
		if (desktop) {
			const os = desktop.platform === 'darwin' ? 'macos' : desktop.platform === 'win32' ? 'windows' : 'linux';
			return `${os}-desktop`;
		}
		const ua = navigator.userAgent.toLowerCase();
		if (ua.includes('android')) return 'android-browser';
		if (ua.includes('iphone') || ua.includes('ipad')) return 'ios-browser';
		if (ua.includes('windows')) return 'windows-browser';
		if (ua.includes('mac')) return 'macos-browser';
		if (ua.includes('linux')) return 'linux-browser';
		return 'web-browser';
	}

	function defaultDeviceName(): string {
		const platform = platformName().replace(/-(browser|desktop)$/, '');
		if (desktop) return `AI Shuffle Desktop on ${platform.charAt(0).toUpperCase()}${platform.slice(1)}`;
		const browser = navigator.userAgent.includes('Firefox')
			? 'Firefox'
			: navigator.userAgent.includes('Edg/')
				? 'Edge'
				: navigator.userAgent.includes('Chrome')
					? 'Chrome'
					: navigator.userAgent.includes('Safari')
						? 'Safari'
						: 'Browser';
		return `${browser} on ${platform.charAt(0).toUpperCase()}${platform.slice(1)}`;
	}

	function getDeviceId(): string {
		const existing = localStorage.getItem(DEVICE_ID_KEY);
		if (existing) return existing;
		const created = crypto.randomUUID();
		localStorage.setItem(DEVICE_ID_KEY, created);
		return created;
	}

	onMount(() => {
		deviceName = defaultDeviceName();
		const fragment = new URLSearchParams(window.location.hash.slice(1));
		const supplied = fragment.get('token');
		if (supplied) {
			code = supplied;
			linkSupplied = true;
		}
		// The one-time token is no longer needed in browser history. It remains
		// only in component memory until the user confirms the exchange.
		if (window.location.hash) {
			window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
		}
	});

	async function connect() {
		if (!code.trim() || !deviceName.trim() || connecting || $isAdmin) return;
		connecting = true;
		error = '';
		try {
			await exchangePairingGrant({
				code,
				deviceId: getDeviceId(),
				deviceName: deviceName.trim(),
				platform: platformName(),
				clientType: window.matchMedia('(display-mode: standalone)').matches ? 'pwa' : 'browser'
			});
			await auth.checkAuth();
			await goto('/');
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Could not pair this device');
		} finally {
			connecting = false;
		}
	}
</script>

<svelte:head><title>Pair device · AI Shuffle</title></svelte:head>

<main class="pair-shell">
	<section class="pair-card">
		<div class="mark"><Link2 size={24} /></div>
		<p class="eyebrow">AI Shuffle</p>
		<h1>Connect this device</h1>
		<p class="intro">Pair this browser with the Shuffle server you opened the link from. The code works once and expires after five minutes.</p>

		{#if $isAdmin}
			<div class="notice warning">
				<ShieldCheck size={18} />
				<div><strong>This browser is already an administrator.</strong><br />Open the QR or link on the device you want to connect.</div>
			</div>
		{:else}
			<label>
				<span>Device name</span>
				<input bind:value={deviceName} maxlength="100" autocomplete="off" />
			</label>

			<label>
				<span>Pairing code</span>
				<input
					class="code-input"
					bind:value={code}
					maxlength="64"
					placeholder="ABCD-EFGH-IJKL"
					autocapitalize="characters"
					autocomplete="one-time-code"
				/>
			</label>

			{#if linkSupplied}
				<div class="notice"><Smartphone size={18} /><span>Pairing link received. Confirm the device name, then connect.</span></div>
			{/if}
			{#if error}<Banner message={error} />{/if}

			<button onclick={connect} disabled={connecting || !code.trim() || !deviceName.trim()}>
				{connecting ? 'Connecting…' : 'Connect device'}
			</button>
		{/if}

		<p class="foot">This creates a controller session. It cannot change credentials, pairing, or other administrator settings.</p>
	</section>
</main>

<style>
	.pair-shell { min-height: 100vh; display: grid; place-items: center; padding: 24px; background: radial-gradient(circle at 50% 0%, color-mix(in oklch, var(--primary) 13%, transparent), transparent 42%), var(--background); color: var(--foreground); }
	.pair-card { width: min(100%, 430px); padding: 34px; border: 1px solid var(--border); border-radius: 20px; background: color-mix(in oklch, var(--background) 96%, var(--primary)); box-shadow: 0 24px 70px rgba(0,0,0,.18); }
	.mark { width: 48px; height: 48px; display: grid; place-items: center; border-radius: 14px; color: var(--primary-foreground); background: var(--primary); margin-bottom: 22px; }
	.eyebrow { margin: 0 0 5px; font-size: 11px; font-weight: 700; letter-spacing: .12em; text-transform: uppercase; color: var(--primary); }
	h1 { margin: 0; font-family: var(--font-serif, serif); font-size: 31px; line-height: 1.08; }
	.intro { margin: 12px 0 26px; color: var(--muted-foreground); line-height: 1.55; font-size: 14px; }
	label { display: grid; gap: 7px; margin-top: 17px; }
	label span { font-size: 12px; font-weight: 650; }
	input { width: 100%; box-sizing: border-box; border: 1px solid var(--border); border-radius: 10px; padding: 11px 12px; color: var(--foreground); background: var(--background); outline: none; }
	input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px color-mix(in oklch, var(--primary) 15%, transparent); }
	.code-input { font-family: var(--font-mono, monospace); letter-spacing: .11em; text-transform: uppercase; }
	button { width: 100%; margin-top: 20px; border: 0; border-radius: 11px; padding: 12px 16px; font-weight: 700; color: var(--primary-foreground); background: var(--primary); cursor: pointer; }
	button:disabled { opacity: .55; cursor: default; }
	.notice { display: flex; gap: 10px; align-items: flex-start; margin-top: 18px; padding: 11px 12px; border-radius: 10px; font-size: 12.5px; line-height: 1.4; color: var(--muted-foreground); background: var(--muted); }
	.notice :global(svg) { flex: 0 0 auto; margin-top: 1px; color: var(--primary); }
	.notice.warning :global(svg) { color: var(--warning, #d97706); }
	.foot { margin: 22px 0 0; font-size: 11.5px; line-height: 1.45; color: var(--muted-foreground); }
	@media (max-width: 520px) { .pair-shell { padding: 0; place-items: stretch; } .pair-card { width: auto; min-height: 100vh; box-sizing: border-box; border: 0; border-radius: 0; padding: 42px 24px; } }
</style>
