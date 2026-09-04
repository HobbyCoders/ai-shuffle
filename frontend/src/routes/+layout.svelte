<script lang="ts">
	import '@fontsource-variable/inter';
	import '@fontsource-variable/jetbrains-mono';
	import '@fontsource/playfair-display/700.css';
	import '@fontsource/playfair-display/900.css';
	import '../app.css';
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import { auth, isAuthenticated, setupRequired, claudeAuthenticated, onboardingComplete } from '$lib/stores/auth.svelte';
	import { theme } from '$lib/stores/theme.svelte';
	import { pwa } from '$lib/stores/pwa.svelte';
	import { chatPreferences } from '$lib/stores/chatPreferences.svelte';
	import { hotkeyPreferences } from '$lib/stores/hotkeyPreferences.svelte';
	import { files } from '$lib/stores/files.svelte';
	import { gitActionPreferences } from '$lib/stores/gitActionPreferences.svelte';
	import { timezonePreference } from '$lib/stores/timezonePreference.svelte';
import { syncFavoriteModels } from '$lib/stores/modelFavorites.svelte';
	import { ensureModelRegistryHydrated } from '$lib/constants/models';
	import { onDestroy } from 'svelte';
	import type { Snippet } from 'svelte';
	import InstallPrompt from '$lib/components/InstallPrompt.svelte';
	import ToastViewport from '$lib/components/ToastViewport.svelte';
	import BannerHost from '$lib/components/BannerHost.svelte';
	import { notify } from '$lib/stores/notifications';
	import { installGlobalErrorHandlers } from '$lib/services/error-reporter';
	import { bindVisualViewport } from '$lib/utils/visual-viewport';

	let { children }: { children: Snippet } = $props();

	let initialized = $state(false);
	let teardownErrorHandlers: (() => void) | null = null;
	let teardownVisualViewport: (() => void) | null = null;

	// When a new build activates while THIS tab is visible, the PWA store defers
	// the reload (so it can't wipe an in-progress composer draft) and flips
	// `updateReady`. Surface that as a non-blocking, sticky toast whose action
	// applies the update on demand. Fires once per pending update.
	let updateToastShown = false;
	$effect(() => {
		if (pwa.current.updateReady && !updateToastShown) {
			updateToastShown = true;
			notify.info('A new version is ready — click to refresh', {
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

	// Bounce to /login when the auth poll observes a lost session, mirroring the
	// HTTP-401 interceptor. Guarded against loops: only fires once initialized,
	// only when confirmed-unauthenticated (not merely "still checking"), setup is
	// not the intended destination, and we're not already on /login or /setup.
	function redirectIfDeauthenticated() {
		if (!initialized) return;
		if ($setupRequired) return; // setup flow owns its own routing
		if ($isAuthenticated) return;
		const path = $page.url.pathname;
		if (path === '/login' || path === '/setup' || path === '/pair') return;
		goto('/login');
	}

	function startAuthPoll() {
		if (authPollTimer) return;
		authPollTimer = setInterval(async () => {
			try {
				await auth.checkAuth();
				redirectIfDeauthenticated();
			} catch (e) {
				console.debug('Auth poll failed:', e);
			}
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
			auth.checkAuth().then(redirectIfDeauthenticated).catch(() => {});
			startAuthPoll();
		}
	}

	onMount(async () => {
		// Size the app shell to the visible viewport so the composer stays
		// above the virtual keyboard (100dvh does not shrink with it).
		teardownVisualViewport = bindVisualViewport();

		// Initialize theme on mount (applies from localStorage immediately to prevent flash)
		theme.init();

		// Initialize PWA features
		pwa.init();

		// Funnel uncaught frontend errors into the durable error_log so agents
		// can read them via GetAppErrors. Gated on auth inside the reporter.
		teardownErrorHandlers = installGlobalErrorHandlers();

		try {
			await auth.checkAuth();

			// Load and sync user preferences after auth is verified
			if ($isAuthenticated) {
				// Sync all settings from server (database is source of truth)
				// These run in parallel for faster startup
				await Promise.all([
					chatPreferences.load(),
					hotkeyPreferences.syncFromServer(),
					theme.syncFromServer(),
					files.syncFromServer(),
					gitActionPreferences.syncFromServer(),
					timezonePreference.syncFromServer(),
					syncFavoriteModels(),
					ensureModelRegistryHydrated(),
					// Seed the notification bell from persisted history.
					notify.loadHistory()
				]);
			}

			initialized = true;

			// Handle redirects
			const path = $page.url.pathname;
			const isPairingPage = path === '/pair';

			if ($setupRequired && path !== '/setup' && !isPairingPage) {
				goto('/setup');
			} else if ($isAuthenticated && !$onboardingComplete && path !== '/setup' && !isPairingPage) {
				goto('/setup');
			} else if (!$isAuthenticated && !$setupRequired && path !== '/login' && !isPairingPage) {
				goto('/login');
			} else if ($isAuthenticated && path === '/login') {
				goto('/');
			} else if ($isAuthenticated && path === '/setup' && $onboardingComplete) {
				goto('/');
			}
		} catch (e) {
			console.error('Auth check failed:', e);
			initialized = true;
		}

		// Start visibility-aware auth polling
		startAuthPoll();
		document.addEventListener('visibilitychange', handleAuthVisibility);
	});

	onDestroy(() => {
		stopAuthPoll();
		document.removeEventListener('visibilitychange', handleAuthVisibility);
		teardownErrorHandlers?.();
		teardownErrorHandlers = null;
		teardownVisualViewport?.();
		teardownVisualViewport = null;
	});
</script>

<svelte:head>
	<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover, interactive-widget=resizes-content" />
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

{#if !initialized}
	<div class="min-h-screen flex items-center justify-center">
		<div class="text-center">
			<div class="animate-spin w-8 h-8 border-2 border-primary border-t-transparent rounded-full mx-auto mb-4"></div>
			<p class="text-gray-400">Loading...</p>
		</div>
	</div>
{:else}
	<!-- PWA Components -->
	<InstallPrompt />
	<ToastViewport />
	<BannerHost />

	{@render children?.()}
{/if}
