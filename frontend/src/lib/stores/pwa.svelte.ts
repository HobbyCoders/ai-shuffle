/**
 * PWA Store - Manages Progressive Web App features
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { browser } from '$app/environment';

interface PWAState {
	isInstalled: boolean;
	isInstallable: boolean;
	isOnline: boolean;
	hasUpdate: boolean;
	isUpdating: boolean;
	installPromptEvent: BeforeInstallPromptEvent | null;
	serviceWorkerRegistration: ServiceWorkerRegistration | null;
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
	hasUpdate: false,
	isUpdating: false,
	installPromptEvent: null,
	serviceWorkerRegistration: null
});

// ============================================================================
// Internal helpers
// ============================================================================

function _update(fn: (s: PWAState) => PWAState) {
	_state = fn(_state);
}

function checkInstallState() {
	if (!browser) return;

	const isStandalone =
		window.matchMedia('(display-mode: standalone)').matches ||
		(window.navigator as Navigator & { standalone?: boolean }).standalone === true ||
		document.referrer.includes('android-app://');

	_update((s) => ({ ...s, isInstalled: isStandalone }));
}

async function registerServiceWorker() {
	if (!browser || !('serviceWorker' in navigator)) {
		return null;
	}

	try {
		const registration = await navigator.serviceWorker.register('/sw.js', {
			scope: '/',
			updateViaCache: 'none'
		});

		_update((s) => ({ ...s, serviceWorkerRegistration: registration }));

		registration.addEventListener('updatefound', () => {
			const newWorker = registration.installing;
			if (newWorker) {
				newWorker.addEventListener('statechange', () => {
					if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
						_update((s) => ({ ...s, hasUpdate: true }));
					}
				});
			}
		});

		let refreshing = false;
		navigator.serviceWorker.addEventListener('controllerchange', () => {
			if (!refreshing) {
				refreshing = true;
				window.location.reload();
			}
		});

		navigator.serviceWorker.addEventListener('message', (event) => {
			if (event.data?.type === 'SW_UPDATED') {
				if (!refreshing) {
					refreshing = true;
					window.location.reload();
				}
			}
		});

		return registration;
	} catch (error) {
		console.error('[PWA] Service worker registration failed:', error);
		return null;
	}
}

function wasRecentlyDismissed(): boolean {
	if (!browser) return false;
	const dismissed = localStorage.getItem('ai-shuffle-install-dismissed');
	if (!dismissed) return false;

	const dismissedTime = parseInt(dismissed, 10);
	const daysSinceDismissed = (Date.now() - dismissedTime) / (1000 * 60 * 60 * 24);
	return daysSinceDismissed < 7;
}

// ============================================================================
// Subscribe shim for Svelte 4 compatibility
// ============================================================================

type Subscriber = (value: PWAState) => void;

const _subscribers = new Set<Subscriber>();

/**
 * Notify all subscribers of state changes.
 * Called via $effect in components that import the store.
 */
function _notifySubscribers() {
	for (const fn of _subscribers) {
		fn(_state);
	}
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
	subscribe(fn: Subscriber) {
		_subscribers.add(fn);
		fn(_state);
		return () => {
			_subscribers.delete(fn);
		};
	},

	init() {
		if (!browser) return;

		checkInstallState();

		window.addEventListener('online', () => {
			_update((s) => ({ ...s, isOnline: true }));
			_notifySubscribers();
			pwa.checkForUpdates();
		});

		window.addEventListener('offline', () => {
			_update((s) => ({ ...s, isOnline: false }));
			_notifySubscribers();
		});

		document.addEventListener('visibilitychange', () => {
			if (document.visibilityState === 'visible') {
				pwa.checkForUpdates();
			}
		});

		window.addEventListener('focus', () => {
			pwa.checkForUpdates();
		});

		window.addEventListener('beforeinstallprompt', (e) => {
			e.preventDefault();
			if (wasRecentlyDismissed()) return;

			_update((s) => ({
				...s,
				isInstallable: true,
				installPromptEvent: e as BeforeInstallPromptEvent
			}));
			_notifySubscribers();
		});

		window.addEventListener('appinstalled', () => {
			_update((s) => ({
				...s,
				isInstalled: true,
				isInstallable: false,
				installPromptEvent: null
			}));
			_notifySubscribers();
		});

		registerServiceWorker();
	},

	async promptInstall(): Promise<boolean> {
		if (!_state.installPromptEvent) return false;

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
				localStorage.setItem('ai-shuffle-install-dismissed', Date.now().toString());
				_update((s) => ({ ...s, installPromptEvent: null, isInstallable: false }));
			}
			_notifySubscribers();
			return outcome === 'accepted';
		} catch (error) {
			console.error('[PWA] Install prompt failed:', error);
			return false;
		}
	},

	async applyUpdate() {
		_update((s) => ({ ...s, isUpdating: true }));
		_notifySubscribers();

		if (_state.serviceWorkerRegistration?.waiting) {
			_state.serviceWorkerRegistration.waiting.postMessage({ type: 'SKIP_WAITING' });

			setTimeout(() => {
				window.location.reload();
			}, 500);
		}
	},

	dismissUpdate() {
		_update((s) => ({ ...s, hasUpdate: false }));
		_notifySubscribers();
	},

	async clearCache() {
		if (_state.serviceWorkerRegistration?.active) {
			_state.serviceWorkerRegistration.active.postMessage({ type: 'CLEAR_CACHE' });
		}
	},

	async registerServiceWorker() {
		return registerServiceWorker();
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
// Derived values (Svelte 5 $derived)
// ============================================================================

// These use getter-based objects with subscribe shims for backward compat.

function createDerivedBool(getter: () => boolean) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: (value: boolean) => void) {
			// For Svelte 4 $store auto-subscription
			// We piggyback on the parent store's subscriber mechanism
			const handler: Subscriber = () => fn(getter());
			_subscribers.add(handler);
			fn(getter());
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

export const isOnline = createDerivedBool(() => _state.isOnline);
export const isInstallable = createDerivedBool(() => _state.isInstallable && !_state.isInstalled);
export const hasUpdate = createDerivedBool(() => _state.hasUpdate);
export const isInstalled = createDerivedBool(() => _state.isInstalled);
