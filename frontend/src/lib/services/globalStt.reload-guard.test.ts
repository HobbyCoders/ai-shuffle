/**
 * #707 review: dictation keeps recording and transcribing in a background
 * tab, so an update must not reload the page out from under it.
 */
import { afterEach, expect, it, vi } from 'vitest';

vi.mock('$lib/api/voice', () => ({ loadVoiceSettings: vi.fn() }));
vi.mock('./keyboard', () => ({ registerShortcut: vi.fn() }));
vi.mock('$lib/stores/hotkeyPreferences.svelte', () => ({ hotkeyPreferences: {} }));
vi.mock('$lib/utils/earcon', () => ({ playEarcon: vi.fn(), primeEarcons: vi.fn(), setEarconsEnabled: vi.fn() }));
vi.mock('./sttStream', () => ({ fetchSttCapability: vi.fn(), startSttStream: vi.fn(), SttUnsupportedError: class extends Error {} }));

import { globalSttState } from './globalStt';
import { reloadBlockers } from './app-update';

afterEach(() => globalSttState.update((s) => ({ ...s, isRecording: false, isTranscribing: false, isPreparing: false })));

it.each([
	['recording', { isRecording: true }],
	['transcribing', { isTranscribing: true }],
	['starting', { isPreparing: true }]
])('dictation that is %s blocks an automatic reload', (_label, patch) => {
	expect(reloadBlockers()).not.toContain('dictation');
	globalSttState.update((s) => ({ ...s, ...patch }));
	expect(reloadBlockers()).toContain('dictation');
});
