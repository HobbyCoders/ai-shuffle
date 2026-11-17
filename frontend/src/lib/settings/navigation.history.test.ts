import { beforeEach, expect, it, vi } from 'vitest';
import { shellHistory, type ShellState } from '$lib/services/app-history.svelte';
import { settingsNav } from './navigation.svelte';

const router = vi.hoisted(() => ({ entries: [] as ShellState[], index: 0 }));
vi.mock('$app/navigation', () => ({ goto: (_url: string, options: { replace: boolean; state: ShellState; shallow: boolean; persistState: boolean }) => {
 expect(options.shallow && options.persistState).toBe(true);
 if (options.replace) router.entries[router.index] = options.state;
 else router.entries.splice(++router.index, Infinity, options.state);
} }));
const here = () => router.entries[router.index];
beforeEach(() => {
 router.index = 0; router.entries = [{}]; shellHistory.stop(); settingsNav.reset();
 shellHistory.start({}, { type: 'session', sessionId: 'a' });
});
it.each([false, true])('reload mid-chain and branch after Back (stacked=%s)', async stacked => {
 settingsNav.stacked = stacked;
 shellHistory.destination({ type: 'settings' });
 shellHistory.detail('settings', { settings: settingsNav.position(stacked ? 'index' : 'page', 0) }, true);
 settingsNav.selectCategory('providers'); settingsNav.selectItem('one'); settingsNav.selectItem('two');
 await vi.waitFor(() => expect(router.entries.length).toBe(5));
 const laterId = here().shell!.id;
 router.index--; shellHistory.land(here());
 shellHistory.stop(); shellHistory.start(here(), { type: 'session', sessionId: 'b' });
 expect(shellHistory.place?.settings?.item).toBe('one');
 expect(router.entries.length).toBe(5);
 router.index++; shellHistory.land(here()); expect(shellHistory.place?.settings?.item).toBe('two');
 router.index--; shellHistory.land(here()); shellHistory.destination({ type: 'session', sessionId: 'c' });
 await vi.waitFor(() => expect(here().shell?.place.sessionId).toBe('c'));
 expect(here().shell!.id).not.toBe(laterId);
 shellHistory.detail('settings', { settings: settingsNav.position('detail') });
 expect(shellHistory.place?.type).toBe('session');
});
