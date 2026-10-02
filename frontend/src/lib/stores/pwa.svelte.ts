/**
 * PWA Store - Manages Progressive Web App features
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';
import { isBundledMobile } from '$lib/api/runtime';
import { untrack } from 'svelte';
import { createSubscribers } from './store-utils';
import { installPromptPreference } from './install-prompt-preference.svelte';
import { sessionOwnerEpoch } from './session-owner';
import { isDesktop } from '$lib/services/desktop';

interface PWAState {
	isInstalled: boolean;
	isInstallable: boolean;
	isOnline: boolean;
	installPromptEvent: BeforeInstallPromptEvent | null;
	serviceWorkerRegistration: ServiceWorkerRegistration | null;
	/**
	 * A newer build has activated but this tab is visible, so we deferred the
	 * reload to avoid wiping in-progress UI (e.g. a chat composer draft). The
	 * app surfaces a "refresh" toast; the reload also happens automatically the
	 * next time the tab is hidden. See `handleSwUpdate` / `applyUpdate`.
	 */
	updateReady: boolean;
}

interface BeforeInstallPromptEvent extends Event {
	prompt(): Promise<void>;
	userChoice: Promise<{ outcome: 'accepted' | 'dismissed'; platform: string }>;
}

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<PWAState>({
	isInstalled: false,
	isInstallable: false,
	isOnline: browser ? navigator.onLine : true,
	installPromptEvent: null,
	serviceWorkerRegistration: null,
	updateReady: false
});

// ============================================================================
// Internal helpers
// ============================================================================

function _update(fn: (s: PWAState) => PWAState) {
	_state = fn(untrack(() => _state));
}

// ── Service-worker update handling ──────────────────────────────────────────
//
// A new service worker calls skipWaiting() on install, so it activates
// immediately and fires `controllerchange` (some builds also postMessage
// `SW_UPDATED`). Reloading a VISIBLE tab there is destructive — it wipes the
// user's in-progress state (e.g. the chat composer draft, which is in-memory).
// So we only reload silently when the tab is hidden; when it's visible we mark
// `updateReady` (surfaced as a non-blocking "refresh" toast) and defer the
// reload until the tab next becomes hidden (or the user clicks the toast).

// Guard so at most one reload ever fires, from whichever path wins.
let _refreshing = false;
// Guard so we register the deferred-reload visibilitychange listener once.
let _deferredReloadArmed = false;

function _reloadNow() {
	if (_refreshing) return;
	_refreshing = true;
	window.location.reload();
}

function _onVisibleHiddenReload() {
	if (document.visibilityState === 'hidden') {
		document.removeEventListener('visibilitychange', _onVisibleHiddenReload);
		_reloadNow();
	}
}

function handleSwUpdate() {
	if (!browser) return;

	// User isn't looking — a silent reload is fine and least surprising.
	if (document.visibilityState === 'hidden') {
		_reloadNow();
		return;
	}

	// Tab is visible: do NOT reload out from under the user. Flag it (drives the
	// refresh toast) and arm a one-time reload for when the tab next hides.
	if (!_state.updateReady) {
		_update((s) => ({ ...s, updateReady: true }));
		_notifySubscribers();
	}
	if (!_deferredReloadArmed) {
		_deferredReloadArmed = true;
		document.addEventListener('visibilitychange', _onVisibleHiddenReload);
	}
}

function checkInstallState() {
	if (!browser) return;

	const isStandalone =
		window.matchMedia('(display-mode: standalone)').matches ||
		(window.navigator as Navigator & { standalone?: boolean }).standalone === true ||
		document.referrer.includes('android-app://');

	_update((s) => ({ ...s, isInstalled: isStandalone }));
}

/** Any page inside the desktop app, main window or pop-up: its partition's user agent carries this marker. */
function isDesktopUserAgent(): boolean {
	return typeof navigator !== 'undefined' && navigator.userAgent.includes('AIShuffleDesktop/');
}

async function registerServiceWorker() {
	if (!browser || !('serviceWorker' in navigator)) {
		return null;
	}
	// The desktop app has no install prompt and takes its updates from the
	// shell, so the worker would only add a cache layer that Chromium's
	// app partition does not always honour. Pop-ups the desktop app opens at
	// this origin (Plaid Link, an OAuth callback's "Return to AI Shuffle")
	// have no preload bridge, so `isDesktop` is false there; they share the
	// app's user agent, and a worker they registered would outlive them in
	// the app's partition.
	if (isDesktop || isDesktopUserAgent()) {
		return null;
	}
	// The phone apps load the worker's /mobile/ fresh on every open; a service
	// worker would only sit in front of it (see removeAppServiceWorkers).
	if (isBundledMobile()) {
		return null;
	}

	try {
		const registration = await navigator.serviceWorker.register('/sw.js', {
			scope: '/',
			updateViaCache: 'none'
		});

		_update((s) => ({ ...s, serviceWorkerRegistration: registration }));

		// Route both activation signals through the shared handler, which reloads
		// silently only when the tab is hidden and otherwise defers (see above).
		navigator.serviceWorker.addEventListener('controllerchange', handleSwUpdate);

		navigator.serviceWorker.addEventListener('message', (event) => {
			if (event.data?.type === 'SW_UPDATED') {
				handleSwUpdate();
			}
		});

		return registration;
	} catch (error) {
		console.error('[PWA] Service worker registration failed:', error);
		return null;
	}
}

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<PWAState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Event listener references (for cleanup)
// ============================================================================

function _handleOnline() {
	_update((s) => ({ ...s, isOnline: true }));
	_notifySubscribers();
	pwa.checkForUpdates();
}

function _handleOffline() {
	_update((s) => ({ ...s, isOnline: false }));
	_notifySubscribers();
}

function _handleVisibilityChange() {
	if (document.visibilityState === 'visible') {
		pwa.checkForUpdates();
	}
}

function _handleFocus() {
	pwa.checkForUpdates();
}

function _handleBeforeInstallPrompt(e: Event) {
	e.preventDefault();
	// Retain this device's capability even while the account suppresses its UI.
	// A later preference reset must not need a new browser install event.

	_update((s) => ({
		...s,
		isInstallable: true,
		installPromptEvent: e as BeforeInstallPromptEvent
	}));
	_notifySubscribers();
}

function _handleAppInstalled() {
	_update((s) => ({
		...s,
		isInstalled: true,
		isInstallable: false,
		installPromptEvent: null
	}));
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const pwa = {
	/** Get current state snapshot */
	get current() {
		return _state;
	},

	/**
	 * Svelte 4 compatibility: subscribe for $pwa auto-subscription.
	 */
	subscribe,

	init() {
		if (!browser) return;

		checkInstallState();

		// Guard against HMR duplicate listener registration
		if (!(window as any).__pwa_listeners) {
			(window as any).__pwa_listeners = true;

			window.addEventListener('online', _handleOnline);
			window.addEventListener('offline', _handleOffline);
			document.addEventListener('visibilitychange', _handleVisibilityChange);
			window.addEventListener('focus', _handleFocus);
			window.addEventListener('beforeinstallprompt', _handleBeforeInstallPrompt);
			window.addEventListener('appinstalled', _handleAppInstalled);
		}

		registerServiceWorker();
	},

	/**
	 * Remove all event listeners registered by init().
	 * Call from appropriate cleanup points (e.g., app teardown).
	 */
	destroy() {
		if (!browser) return;

		window.removeEventListener('online', _handleOnline);
		window.removeEventListener('offline', _handleOffline);
		document.removeEventListener('visibilitychange', _handleVisibilityChange);
		window.removeEventListener('focus', _handleFocus);
		window.removeEventListener('beforeinstallprompt', _handleBeforeInstallPrompt);
		window.removeEventListener('appinstalled', _handleAppInstalled);

		// Allow re-registration after destroy
		delete (window as any).__pwa_listeners;
	},

	async promptInstall(): Promise<boolean> {
		if (!_state.installPromptEvent) return false;
		const ownerEpoch = sessionOwnerEpoch();

		try {
			await _state.installPromptEvent.prompt();
			const { outcome } = await _state.installPromptEvent.userChoice;

			if (outcome === 'accepted') {
				_update((s) => ({
					...s,
					isInstalled: true,
					isInstallable: false,
					installPromptEvent: null
				}));
			} else {
				if (ownerEpoch === sessionOwnerEpoch()) installPromptPreference.snooze();
				_update((s) => ({ ...s, installPromptEvent: null, isInstallable: false }));
			}
			_notifySubscribers();
			return outcome === 'accepted';
		} catch (error) {
			console.error('[PWA] Install prompt failed:', error);
			return false;
		}
	},

	async registerServiceWorker() {
		return registerServiceWorker();
	},

	/**
	 * Inside the phone apps only: remove any service worker on this worker's
	 * origin. The phone UI is the worker's /mobile/, and an Android app from
	 * before the bundled UI loaded the worker's root page, whose /sw.js (scope
	 * `/`) can still be registered in the app's WebView. It would answer
	 * /mobile/ requests from its cache and outside the app's request checks.
	 * Never runs in a browser, where that worker is the user's installed web app.
	 */
	async removeAppServiceWorkers(): Promise<number> {
		if (!browser || !isBundledMobile() || !('serviceWorker' in navigator)) return 0;
		try {
			const registrations = await navigator.serviceWorker.getRegistrations();
			const removed = await Promise.all(registrations.map((registration) => registration.unregister()));
			return removed.filter(Boolean).length;
		} catch {
			return 0;
		}
	},

	/**
	 * Reload now to apply a pending update. Called from the "refresh" toast
	 * action when a new build is ready but the tab is visible. Guarded so it
	 * fires at most once even if the deferred visibility reload also races it.
	 */
	applyUpdate() {
		if (!browser) return;
		_reloadNow();
	},

	async checkForUpdates() {
		if (_state.serviceWorkerRegistration) {
			try {
				await _state.serviceWorkerRegistration.update();
			} catch {
				// Update check failed silently
			}
		}
	}
};

// ============================================================================
// Derived values (via store-utils createDerived)
// ============================================================================

export const isInstallable = createDerived(() => _state.isInstallable && !_state.isInstalled);
