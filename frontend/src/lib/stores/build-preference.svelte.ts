/** Remembered Build tool, without sharing open editors or selected records. */
import { browser } from '$app/environment';
import { preferenceSync } from '$lib/services/preferences';

export type BuildSub = 'profile' | 'mcp' | 'plugins' | 'hooks';
const CACHE_KEY = 'ai-shuffle-build-subtab';
const normalize = (value: unknown): BuildSub =>
 value === 'mcp' || value === 'plugins' || value === 'hooks' ? value : 'profile';
function load(): BuildSub {
 try { return browser ? normalize(localStorage.getItem(CACHE_KEY)) : 'profile'; }
 catch { return 'profile'; }
}
let current = $state<BuildSub>(load());
function apply(value: unknown) {
 current = normalize(value);
 try { if (browser) localStorage.setItem(CACHE_KEY, current); } catch { /* Cache only. */ }
}
export const buildPreference = {
 get current() { return current; },
 set(value: BuildSub) { apply(value); preferenceSync.save('build_subtab', current); },
};
preferenceSync.register('build_subtab', { read: () => current, apply });
