<script lang="ts">
	import '$lib/components/ui/surfaces.css';
	import Banner from '$lib/components/Banner.svelte';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { Link2, ShieldCheck, Smartphone } from 'lucide-svelte';
	import { exchangePairingGrant } from '$lib/api/pairing';
	import { getErrorMessage } from '$lib/api/client';
	import { auth, isAdmin } from '$lib/stores/auth.svelte';
	import { desktop } from '$lib/services/desktop';
	import { randomUUID } from '$lib/utils/id';

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
		const created = randomUUID();
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

<main class="ui-surface ui-onboarding">
	<section class="ui-onboarding-card">
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
				<input class="ui-input" bind:value={deviceName} maxlength="100" autocomplete="off" />
			</label>

			<label>
				<span>Pairing code</span>
				<input
					class="ui-input code-input"
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

			<button class="ui-button primary connect" onclick={connect} disabled={connecting || !code.trim() || !deviceName.trim()}>
				{connecting ? 'Connecting…' : 'Connect device'}
			</button>
		{/if}

		<p class="foot">This creates a controller session. It cannot change credentials, pairing, or other administrator settings.</p>
	</section>
</main>

<style>
 .intro, .foot { color: var(--muted-foreground); font-size: 13px; line-height: 1.55; }
 .intro { margin: 12px 0 24px; }
 label { display: grid; gap: 6px; margin-top: 16px; }
 label span { font-size: 12px; color: var(--muted-foreground); }
 .code-input { font-family: var(--font-mono); }
 .notice { display: flex; gap: 10px; align-items: start; padding-block: 16px; color: var(--muted-foreground); }
 .notice :global(svg) { flex: none; }
 .warning :global(svg) { color: var(--warning); }
 .connect { margin-top: 20px; width: 100%; }
 .foot { border-top: 1px solid var(--border); padding-top: 16px; margin-top: 24px; }
</style>
