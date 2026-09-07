<script lang="ts">
	import '@fontsource-variable/inter';
	import '@fontsource-variable/jetbrains-mono';
	import '@fontsource/playfair-display/700.css';
	import '@fontsource/playfair-display/900.css';
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { auth, isAuthenticated } from '$lib/stores/auth.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { pwa } from '$lib/stores/pwa.svelte';
	import { ensureModelRegistryHydrated } from '$lib/constants/models';
	import { onDestroy } from 'svelte';
	import type { Snippet } from 'svelte';
	import InstallPrompt from '$lib/components/InstallPrompt.svelte';
	import BannerHost from '$lib/components/BannerHost.svelte';
	import ComputerUseBanner from '$lib/components/ComputerUseBanner.svelte';
	import { notify } from '$lib/stores/notifications';
	import { installGlobalErrorHandlers } from '$lib/services/error-reporter';
	import { bindVisualViewport } from '$lib/utils/visual-viewport';
	import { startLiveSync } from '$lib/services/live-sync';
	import { initDesktop } from '$lib/services/desktop';
	let teardownLiveSync: (() => void) | undefined;
	let teardownDesktop: (() => void) | null = null;

	let { children }: { children: Snippet } = $props();

	let initialized = $state(false);
	let mounted = $state(false);
	let bootstrapPreparation: Promise<unknown> | undefined;
	let teardownErrorHandlers: (() => void) | null = null;
	let teardownVisualViewport: (() => void) | null = null;

	// When a new build activates while THIS tab is visible, the PWA store defers
	// the reload (so it can't wipe an in-progress composer draft) and flips
	// `updateReady`. Surface that as a non-blocking, sticky banner whose action
	// applies the update on demand. Fires once per pending update.
	let updateBannerShown = false;
	$effect(() => {
		if (pwa.current.updateReady && !updateBannerShown) {
			updateBannerShown = true;
			notify.banner('A new version is ready — click to refresh', {
				duration_ms: 0,
				action: { label: 'Refresh', onClick: () => pwa.applyUpdate() }
			});
		}
	});

	// Auth poll: detects gh login/logout happening in a terminal.
	// Auth changes are rare, so 2-minute polling is plenty.
	// Pauses entirely when tab is hidden; checks immediately on tab return.
	const AUTH_POLL_INTERVAL_MS = 120_000;
	let authPollTimer: ReturnType<typeof setInterval> | null = null;

	// Unknown/loading/error states cannot substitute for a confirmed denial.
	const authDestination = $derived.by(() => {
		const { status, loading, error } = $auth;
		const path = $page.url.pathname;
		if (!initialized || loading || error || !status || path === '/pair') return null;
		if (status.authenticated === false) return path === '/login' ? null : '/login';
		if (status.authenticated !== true) return null;
		if (status.setup_required || status.onboarding_complete === false) return path === '/setup' ? null : '/setup';
		return path === '/login' || path === '/setup' ? '/' : null;
	});
	$effect(() => {
		if (authDestination) void goto(authDestination);
	});

	// Delay protected children until bootstrap has real auth evidence. A failed
	// refresh after mounting retains the existing authenticated UI and its draft.
	$effect(() => {
		const { status, loading, error } = $auth;
		if (!mounted || initialized || loading || error || typeof status?.authenticated !== 'boolean') return;
		let current = true;
		// Metadata failures can start another auth check through the 401 handler.
		// Reuse this work so that confirmation cannot restart failing requests.
		const preparation = status.authenticated
			? (bootstrapPreparation ??= Promise.allSettled([ensureModelRegistryHydrated(), notify.loadHistory()]))
			: Promise.resolve();
		void preparation.then(() => { if (current) initialized = true; });
		// A newer auth check or unmount retires this bootstrap continuation.
		return () => { current = false; };
	});

	async function refreshAuth() {
		try { await auth.checkAuth(); }
		catch { /* The store retains prior auth and exposes a connection error. */ }
	}

	function startAuthPoll() {
		if (authPollTimer) return;
		authPollTimer = setInterval(() => {
			if (!auth.current.loading) void refreshAuth();
		}, AUTH_POLL_INTERVAL_MS);
	}

	function stopAuthPoll() {
		if (authPollTimer) {
			clearInterval(authPollTimer);
			authPollTimer = null;
		}
	}

	function handleAuthVisibility() {
		if (document.hidden) {
			stopAuthPoll();
		} else {
			// Check immediately on tab return, then resume interval
			void refreshAuth();
			startAuthPoll();
		}
	}

	onMount(() => {
		// Size the app shell to the visible viewport so the composer stays
		// above the virtual keyboard (100dvh does not shrink with it).
		teardownVisualViewport = bindVisualViewport();

		// Initialize theme on mount (applies from localStorage immediately to prevent flash)
		theme.init();
		// Desktop shell bridge (no-op in a browser): title-bar colours, menu commands.
		teardownDesktop = initDesktop();
		teardownLiveSync = startLiveSync();

		// Initialize PWA features
		pwa.init();

		// Funnel uncaught frontend errors into the durable error_log so agents
		// can read them via GetAppErrors. Gated on auth inside the reporter.
		teardownErrorHandlers = installGlobalErrorHandlers();

		mounted = true;
		void refreshAuth();

		// Start visibility-aware auth polling
		startAuthPoll();
		document.addEventListener('visibilitychange', handleAuthVisibility);
	});

	onDestroy(() => {
		teardownDesktop?.();
		teardownDesktop = null;
		teardownLiveSync?.();
		stopAuthPoll();
		document.removeEventListener('visibilitychange', handleAuthVisibility);
		teardownErrorHandlers?.();
		teardownErrorHandlers = null;
		teardownVisualViewport?.();
		teardownVisualViewport = null;
	});
</script>

<svelte:head>
	<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content" />
	<meta name="theme-color" content="#0f0f0f" />
	<meta name="mobile-web-app-capable" content="yes" />
	<meta name="apple-mobile-web-app-capable" content="yes" />
	<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent" />
	<!-- Prevent flash of wrong theme by applying theme class before paint -->
	{@html `<script>
		(function() {
			var stored = localStorage.getItem('ai-shuffle-theme');
			var theme = stored || 'system';
			var resolved = theme;
			if (theme === 'system') {
				resolved = window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
			}
			document.documentElement.classList.add(resolved);
			document.documentElement.classList.remove(resolved === 'dark' ? 'light' : 'dark');
			var meta = document.querySelector('meta[name="theme-color"]');
			if (meta) meta.setAttribute('content', resolved === 'dark' ? '#0f0f0f' : '#ffffff');
		})();
	</script>`}
</svelte:head>

{#if $page.url.pathname !== '/pair' && (!initialized || authDestination || (!['/login', '/setup'].includes($page.url.pathname) && !$isAuthenticated))}
	<div class="min-h-screen flex items-center justify-center">
		<div class="text-center">
			{#if $auth.error && !$auth.loading}
				<p role="status" class="text-muted-foreground mb-4">Could not verify your connection to this worker.</p>
				<button type="button" class="btn" onclick={() => void refreshAuth()}>Try again</button>
			{:else}
				<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto mb-4"></div>
				<p class="text-gray-400">Loading...</p>
			{/if}
		</div>
	</div>
{:else}
	<!-- PWA Components -->
	<InstallPrompt />
	<BannerHost />
	{#if $isAuthenticated}<ComputerUseBanner />{/if}

	{@render children?.()}
{/if}
