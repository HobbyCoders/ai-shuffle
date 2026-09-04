/** Tests for the in-app notification sound hooks. */

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

let notificationConstructor: ReturnType<typeof vi.fn>;
let requestPermission: ReturnType<typeof vi.fn>;

beforeEach(() => {
	sound.play.mockReset();
	sound.playComplete.mockReset();
	sound.playQuestion.mockReset();
	notificationConstructor = vi.fn();
	requestPermission = vi.fn();
	Object.defineProperty(window, 'Notification', {
		configurable: true,
		value: Object.assign(notificationConstructor, {
			permission: 'granted',
			requestPermission,
		}),
	});
});

describe('notifyWaitingForResponse', () => {
	it.each(['question', 'permission'] as const)('plays the %s chime without using browser notifications', (type) => {
		expect(notifyWaitingForResponse(type)).toBeNull();
		expect(sound.play).toHaveBeenCalledWith(type);
		expect(notificationConstructor).not.toHaveBeenCalled();
		expect(requestPermission).not.toHaveBeenCalled();
	});
});

describe('notifySessionComplete', () => {
	it('plays the complete chime without using browser notifications', () => {
		expect(notifySessionComplete({ sessionId: 's1', sessionTitle: 'Test session' })).toBeNull();
		expect(sound.playComplete).toHaveBeenCalledOnce();
		expect(notificationConstructor).not.toHaveBeenCalled();
		expect(requestPermission).not.toHaveBeenCalled();
	});
});
