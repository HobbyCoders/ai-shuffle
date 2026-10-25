/**
 * The dock's waveform is the caller's meter between the agent's turns.
 * Phone report: speech was transcribed while the bars never moved, because
 * the bars only animated for the agent and never read `level`.
 */
import { cleanup, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
const api = vi.hoisted(() => ({ getVoiceCallState: vi.fn() }));
vi.mock('$lib/api/voice', async (original) => ({ ...await original<typeof import('$lib/api/voice')>(), ...api }));
const IDLE = vi.hoisted(() => ({ sessionId: 'call', status: 'live', model: 'grok-voice-supergrok', startedAt: null,
	micMuted: false, agentMuted: false, agentSpeaking: false, userSpeaking: false, level: 0 }));
vi.mock('$lib/services/voiceCall', () => ({
	voiceCallState: writable({ ...IDLE }),
	hangup: vi.fn(), toggleMic: vi.fn(), toggleAgent: vi.fn(), MIC_PROBLEM_LABEL: 'Microphone not reaching the call',
}));
import { voiceCallState } from '$lib/services/voiceCall';
import VoiceCallPanel from './VoiceCallPanel.svelte';

const meter = () => screen.getByTestId('voice-waveform');
const level = () => meter().style.getPropertyValue('--level');

beforeEach(() => { voiceCallState.set({ ...IDLE } as never); });
afterEach(cleanup);

it('follows the caller level and marks speech that reaches the call', async () => {
	render(VoiceCallPanel, { sessionId: 'call', compact: true });
	expect(level()).toBe('0.000');
	voiceCallState.update(s => ({ ...s, level: 0.6, userSpeaking: true }));
	await tick();
	expect(level()).toBe('0.600');
	expect(meter().classList.contains('metering')).toBe(true);
	expect(screen.getByTestId('voice-ring').classList.contains('hearing')).toBe(true);
	expect(screen.getByRole('status').textContent).toBe('Hearing you…');
});

it('shows a level below the gate too, so a working but quiet mic is visible', async () => {
	render(VoiceCallPanel, { sessionId: 'call' });
	voiceCallState.update(s => ({ ...s, level: 0.25, userSpeaking: false }));
	await tick();
	expect(level()).toBe('0.250');
	expect(screen.getByTestId('voice-ring').classList.contains('hearing')).toBe(false);
});

it('rests while muted, while the agent speaks, and for another session', async () => {
	render(VoiceCallPanel, { sessionId: 'call' });
	voiceCallState.update(s => ({ ...s, level: 0.7, userSpeaking: true, micMuted: true }));
	await tick();
	expect(level()).toBe('0.000');
	expect(screen.getByTestId('voice-ring').classList.contains('hearing')).toBe(false);
	voiceCallState.update(s => ({ ...s, micMuted: false, agentSpeaking: true }));
	await tick();
	expect(level()).toBe('0.000');
	voiceCallState.update(s => ({ ...s, agentSpeaking: false, sessionId: 'other' }));
	await tick();
	expect(level()).toBe('0.000');
});
