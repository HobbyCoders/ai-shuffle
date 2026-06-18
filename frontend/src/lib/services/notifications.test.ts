/**
 * Tests for notifications.ts — verifies the audible chime plays for "session
 * complete" and "Claude has a question / needs permission" REGARDLESS of whether
 * the window is focused (the visual OS popup is still suppressed when focused).
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

const sound = vi.hoisted(() => ({
	play: vi.fn(),
	playComplete: vi.fn(),
	playQuestion: vi.fn(),
}));

vi.mock('./notificationSound', () => ({
	notificationSound: sound,
}));

import { notifyWaitingForResponse, notifySessionComplete } from './notifications';

beforeEach(() => {
	sound.play.mockReset();
	sound.playComplete.mockReset();
	sound.playQuestion.mockReset();
});

describe('notifyWaitingForResponse', () => {
	it('plays the question chime even when the window is focused', () => {
		vi.spyOn(document, 'hasFocus').mockReturnValue(true);
		const result = notifyWaitingForResponse('question', { questionCount: 1 });
		expect(sound.play).toHaveBeenCalledWith('question');
		// Focused → no redundant OS popup (in-app modal is already visible).
		expect(result).toBeNull();
	});

	it('plays the permission chime when focused', () => {
		vi.spyOn(document, 'hasFocus').mockReturnValue(true);
		notifyWaitingForResponse('permission', { toolName: 'Bash' });
		expect(sound.play).toHaveBeenCalledWith('permission');
	});

	it('plays the chime when the window is NOT focused', () => {
		vi.spyOn(document, 'hasFocus').mockReturnValue(false);
		notifyWaitingForResponse('question', { questionCount: 2 });
		expect(sound.play).toHaveBeenCalledWith('question');
	});
});

describe('notifySessionComplete', () => {
	it('plays the complete chime', () => {
		notifySessionComplete({ sessionId: 's1', sessionTitle: 'Test session' });
		expect(sound.playComplete).toHaveBeenCalled();
	});
});
