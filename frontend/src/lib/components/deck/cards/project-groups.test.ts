import { describe, expect, it } from 'vitest';
import { groupDeleteMessage, groupMemberCount } from './project-groups';

describe('project group deletion copy', () => {
	it('counts every assignment the delete clears, including projects listed on another worker', () => {
		const assignments = { a: 'Work', b: 'Work', c: 'Home', 'on-peer': 'Work' };
		expect(groupMemberCount(assignments, 'Work')).toBe(3);
		expect(groupMemberCount(undefined, 'Work')).toBe(0);
	});

	it('names the group and how many projects become ungrouped', () => {
		expect(groupDeleteMessage('Work', 2)).toBe('Delete the group "Work"? 2 projects become ungrouped. No project is removed.');
		expect(groupDeleteMessage('Solo', 1)).toBe('Delete the group "Solo"? 1 project becomes ungrouped. No project is removed.');
	});
});
