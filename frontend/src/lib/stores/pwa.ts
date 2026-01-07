import { writable, derived } from 'svelte/store';
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

function createPWAStore() {
  const initialState: PWAState = {
    isInstalled: false,
    isInstallable: false,
    isOnline: browser ? navigator.onLine : true,
    hasUpdate: false,
    isUpdating: false,
    installPromptEvent: null,
    serviceWorkerRegistration: null
  };

  const { subscribe, set, update } = writable<PWAState>(initialState);

  // Check if app is already installed
  function checkInstallState() {
    if (!browser) return;

    // Check if running in standalone mode (installed)
    const isStandalone = window.matchMedia('(display-mode: standalone)').matches ||
      (window.navigator as any).standalone === true ||
      document.referrer.includes('android-app://');

    update(state => ({ ...state, isInstalled: isStandalone }));
  }

  // Register service worker
  async function registerServiceWorker() {
    if (!browser || !('serviceWorker' in navigator)) {
      console.log('[PWA] Service workers not supported');
      return null;
    }

    try {
      const registration = await navigator.serviceWorker.register('/sw.js', {
        scope: '/'
      });

      console.log('[PWA] Service worker registered:', registration.scope);

      update(state => ({ ...state, serviceWorkerRegistration: registration }));

      // Check for updates
      registration.addEventListener('updatefound', () => {
        const newWorker = registration.installing;
        if (newWorker) {
          newWorker.addEventListener('statechange', () => {
            if (newWorker.state === 'installed' && navigator.serviceWorker.controller) {
              console.log('[PWA] New version available');
              update(state => ({ ...state, hasUpdate: true }));
            }
          });
        }
      });

      // Handle controller change (new service worker activated)
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        console.log('[PWA] New service worker activated');
      });

      return registration;
    } catch (error) {
      console.error('[PWA] Service worker registration failed:', error);
      return null;
    }
  }

  // Apply update
  async function applyUpdate() {
    update(state => ({ ...state, isUpdating: true }));

    const state = await new Promise<PWAState>(resolve => {
      subscribe(s => resolve(s))();
    });

    if (state.serviceWorkerRegistration?.waiting) {
      // Tell the waiting service worker to activate
      state.serviceWorkerRegistration.waiting.postMessage({ type: 'SKIP_WAITING' });

      // Reload the page after a short delay
      setTimeout(() => {
        window.location.reload();
      }, 500);
    }
  }

  // Dismiss update notification
  function dismissUpdate() {
    update(state => ({ ...state, hasUpdate: false }));
  }

  // Show install prompt
  async function promptInstall(): Promise<boolean> {
    const state = await new Promise<PWAState>(resolve => {
      subscribe(s => resolve(s))();
    });

    if (!state.installPromptEvent) {
      console.log('[PWA] No install prompt available');
      return false;
    }

    try {
      await state.installPromptEvent.prompt();
      const { outcome } = await state.installPromptEvent.userChoice;

      if (outcome === 'accepted') {
        console.log('[PWA] App installed');
        update(s => ({
          ...s,
          isInstalled: true,
          isInstallable: false,
          installPromptEvent: null
        }));
        return true;
      } else {
        console.log('[PWA] Install dismissed');
        // Store dismissal in localStorage to not prompt again for a while
        localStorage.setItem('ai-shuffle-install-dismissed', Date.now().toString());
        update(s => ({ ...s, installPromptEvent: null, isInstallable: false }));
        return false;
      }
    } catch (error) {
      console.error('[PWA] Install prompt failed:', error);
      return false;
    }
  }

  // Check if install prompt was recently dismissed
  function wasRecentlyDismissed(): boolean {
    if (!browser) return false;
    const dismissed = localStorage.getItem('ai-shuffle-install-dismissed');
    if (!dismissed) return false;

    const dismissedTime = parseInt(dismissed, 10);
    const daysSinceDismissed = (Date.now() - dismissedTime) / (1000 * 60 * 60 * 24);

    // Don't show prompt again for 7 days after dismissal
    return daysSinceDismissed < 7;
  }

  // Initialize PWA features
  function init() {
    if (!browser) return;

    checkInstallState();

    // Listen for online/offline events
    window.addEventListener('online', () => {
      update(state => ({ ...state, isOnline: true }));
    });

    window.addEventListener('offline', () => {
      update(state => ({ ...state, isOnline: false }));
    });

    // Listen for install prompt
    window.addEventListener('beforeinstallprompt', (e) => {
      e.preventDefault();

      // Don't show if recently dismissed
      if (wasRecentlyDismissed()) {
        console.log('[PWA] Install prompt recently dismissed, skipping');
        return;
      }

      console.log('[PWA] Install prompt available');
      update(state => ({
        ...state,
        isInstallable: true,
        installPromptEvent: e as BeforeInstallPromptEvent
      }));
    });

    // Listen for app installed event
    window.addEventListener('appinstalled', () => {
      console.log('[PWA] App was installed');
      update(state => ({
        ...state,
        isInstalled: true,
        isInstallable: false,
        installPromptEvent: null
      }));
    });

    // Register service worker
    registerServiceWorker();
  }

  // Clear cache
  async function clearCache() {
    const state = await new Promise<PWAState>(resolve => {
      subscribe(s => resolve(s))();
    });

    if (state.serviceWorkerRegistration?.active) {
      state.serviceWorkerRegistration.active.postMessage({ type: 'CLEAR_CACHE' });
      console.log('[PWA] Cache cleared');
    }
  }

  return {
    subscribe,
    init,
    promptInstall,
    applyUpdate,
    dismissUpdate,
    clearCache,
    registerServiceWorker
  };
}

export const pwa = createPWAStore();

// Derived stores for convenience
export const isOnline = derived(pwa, $pwa => $pwa.isOnline);
export const isInstallable = derived(pwa, $pwa => $pwa.isInstallable && !$pwa.isInstalled);
export const hasUpdate = derived(pwa, $pwa => $pwa.hasUpdate);
export const isInstalled = derived(pwa, $pwa => $pwa.isInstalled);
