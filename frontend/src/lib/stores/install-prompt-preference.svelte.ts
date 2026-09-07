/** Account presentation choices; browser installation capability stays in pwa. */
import { browser } from '$app/environment';
import { preferenceSync } from '$lib/services/preferences';

interface InstallPromptChoice { never: boolean; dismissedAt: number | null }
const NEVER_KEY = 'ai-shuffle-install-never';
const DISMISSED_KEY = 'ai-shuffle-install-dismissed';
const SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;
function normalize(value: unknown): InstallPromptChoice {
 const raw = value && typeof value === 'object' ? value as Partial<InstallPromptChoice> : {};
 return { never: raw.never === true,
  dismissedAt: typeof raw.dismissedAt === 'number' && Number.isFinite(raw.dismissedAt) && raw.dismissedAt > 0 ? raw.dismissedAt : null };
}
function load(): InstallPromptChoice {
 try {
  return browser ? normalize({ never: !!localStorage.getItem(NEVER_KEY),
   dismissedAt: Number(localStorage.getItem(DISMISSED_KEY)) }) : normalize(undefined);
 } catch { return normalize(undefined); }
}
let current = $state<InstallPromptChoice>(load());
function apply(value: unknown) {
 current = normalize(value);
 try {
  if (!browser) return;
  if (current.never) localStorage.setItem(NEVER_KEY, '1'); else localStorage.removeItem(NEVER_KEY);
  if (current.dismissedAt !== null) localStorage.setItem(DISMISSED_KEY, String(current.dismissedAt)); else localStorage.removeItem(DISMISSED_KEY);
 } catch { /* Cache only. */ }
}
function save(value: InstallPromptChoice) { apply(value); preferenceSync.save('install_prompt', { ...current }); }
export const installPromptPreference = {
 get current(): Readonly<InstallPromptChoice> { return current; },
 isSuppressed(now = Date.now()) { return current.never || (current.dismissedAt !== null && now - current.dismissedAt < SNOOZE_MS); },
 snooze() { save({ ...current, dismissedAt: Date.now() }); },
 neverShow() { save({ ...current, never: true }); },
};
preferenceSync.register('install_prompt', { read: () => ({ ...current }), apply });
