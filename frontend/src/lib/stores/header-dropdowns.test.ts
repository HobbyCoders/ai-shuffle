/**
 * Header dropdown coordinator — exactly one header popover open at a time.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { get } from 'svelte/store';
import { openHeaderDropdown, openDropdown, closeDropdown, toggleDropdown } from './header-dropdowns';

beforeEach(() => openHeaderDropdown.set(null));

describe('header dropdowns', () => {
	it('opening one closes any other', () => {
		openDropdown('profile');
		openDropdown('branch');
		expect(get(openHeaderDropdown)).toBe('branch');
	});

	it('close only affects the dropdown that is open', () => {
		openDropdown('git-push');
		closeDropdown('git-pull');
		expect(get(openHeaderDropdown)).toBe('git-push');
		closeDropdown('git-push');
		expect(get(openHeaderDropdown)).toBeNull();
	});

	it('toggle reports the new state and swaps between dropdowns', () => {
		expect(toggleDropdown('notifications')).toBe(true);
		expect(toggleDropdown('session-title')).toBe(true);
		expect(get(openHeaderDropdown)).toBe('session-title');
		expect(toggleDropdown('session-title')).toBe(false);
		expect(get(openHeaderDropdown)).toBeNull();
	});
});
