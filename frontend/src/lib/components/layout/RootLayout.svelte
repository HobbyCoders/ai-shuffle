<script lang="ts">
	/**
	 * RootLayout — bootstrap shared by the worker web UI and the mobile app:
	 * theme, live sync, error reporting, the auth poll and the loading gate.
	 *
	 * `mobileApp` marks the phone UI build (`mobile/`, served at /mobile/ and
	 * shown by the Android and iOS apps): there are no /setup or /pair
	 * routes there (the app owns the session and passes a Bearer token), there
	 * is no service worker, and an expired session shows a reconnect message.
	 *
	 * A browser the worker does not recognise sees the owner-password sign-in
	 * (or, before any password exists, the step that creates it), or a notice
	 * when it cannot sign in from where it is. An owner whose install has no
	 * password yet sees the blocking "set an owner password" step first.
	 */
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
	import SupportReportHost from '$lib/components/support/SupportReportHost.svelte';
	import OwnerAccessGate from '$lib/components/OwnerAccessGate.svelte';
	import { ownerAccess } from '$lib/stores/owner-access.svelte';
	import { notify } from '$lib/stores/notifications';
	import { installGlobalErrorHandlers } from '$lib/services/error-reporter';
	import { bindVisualViewport } from '$lib/utils/visual-viewport';
	import { startLiveSync } from '$lib/services/live-sync';
	import { initDesktop } from '$lib/services/desktop';
	import { installFileLinkGuard } from '$lib/utils/file-link-guard';
	let teardownLiveSync: (() => void) | undefined;
	let teardownDesktop: (() => void) | null = null;

	interface Props {
		children: Snippet;
		/** True in the mobile app's bundle (see mobile/). */
		mobileApp?: boolean;
	}
	let { children, mobileApp = false }: Props = $props();

	let initialized = $state(false);
	const currentPath = $derived<string>($page.url.pathname);
	let mounted = $state(false);
	let bootstrapPreparation: Promise<unknown> | undefined;
	let teardownErrorHandlers: (() => void) | null = null;
	let teardownVisualViewport: (() => void) | null = null;
	let teardownFileLinks: (() => void) | null = null;

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
	// An owner on an install without a password (typically after an upgrade)
	// must set one before anything else. Desktop installs never require it.
	const ownerPasswordBlocking = $derived(
		!mobileApp && currentPath !== '/pair' && $auth.status?.authenticated === true
			&& $auth.status?.is_admin === true && ownerAccess.status?.required === true
			&& ownerAccess.status?.password_set === false
	);

	const authDestination = $derived.by(() => {
		const { status, loading, error } = $auth;
		// Widened: SvelteKit types the pathname per app, and the mobile app has
		// only the root route.
		const path: string = $page.url.pathname;
		// The mobile app has no auth routes: a lost session is reported below.
		if (mobileApp) return null;
		if (!initialized || loading || error || !status || path === '/pair') return null;
		// A denied browser stays where it is and sees the local-network notice below.
		if (status.authenticated !== true) return null;
		if (status.setup_required || status.onboarding_complete === false) return path === '/setup' ? null : '/setup';
		return path === '/setup' ? '/' : null;
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
		// Owner password state first, so a blocking step never flashes the app.
		await ownerAccess.refresh();
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
		// File links in chat download in place; a refused one shows an error
		// instead of replacing the app with the API's JSON.
		teardownFileLinks = installFileLinkGuard();

		// PWA features are the web UI's. The phone UI (served at /mobile/ and
		// shown by the phone apps) gets no service worker and drops a stale one.
		if (!mobileApp) pwa.init();
		else void pwa.removeAppServiceWorkers();

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
		teardownFileLinks?.();
		teardownFileLinks = null;
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

{#if mobileApp && initialized && !$auth.loading && !$auth.error && $auth.status?.authenticated === false}
	<!-- The phone UI cannot sign in by itself: the app minted the session. -->
	<div class="min-h-screen flex items-center justify-center">
		<div class="text-center">
			<p role="status" class="text-muted-foreground mb-4">Your session on this worker has expired.</p>
			<p class="text-muted-foreground mb-4">Go back to Shuffle and use Refresh chat to reconnect.</p>
			<button type="button" class="btn" onclick={() => void refreshAuth()}>Try again</button>
		</div>
	</div>
{:else if !mobileApp && currentPath !== '/pair' && initialized && !$auth.loading && !$auth.error && $auth.status?.authenticated === false}
	<!-- Sign in with the owner password, create the first one, or a notice. -->
	<OwnerAccessGate status={ownerAccess.status} authenticated={false} firstRun={$auth.status?.onboarding_complete === false} onDone={refreshAuth} onRetry={() => void refreshAuth()} />
{:else if ownerPasswordBlocking && initialized && !$auth.loading}
	<!-- The blocking "set an owner password" step. -->
	<OwnerAccessGate status={ownerAccess.status} authenticated={true} firstRun={$auth.status?.onboarding_complete === false} onDone={refreshAuth} onRetry={() => void refreshAuth()} />
{:else if (mobileApp && !initialized) || (!mobileApp && currentPath !== '/pair' && (!initialized || authDestination || (currentPath !== '/setup' && !$isAuthenticated)))}
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
	{#if !mobileApp}<InstallPrompt />{/if}
	<BannerHost />
	{#if $isAuthenticated}<ComputerUseBanner /><SupportReportHost />{/if}

	{@render children?.()}
{/if}
