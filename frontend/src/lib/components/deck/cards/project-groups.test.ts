import { describe, expect, it } from 'vitest';
import { groupDeleteMessage, groupMemberCount } from './project-groups';

describe('project group deletion copy', () => {
	it('counts the listed projects in the group only', () => {
		const assignments = { a: 'Work', b: 'Work', c: 'Home', gone: 'Work' };
		expect(groupMemberCount(assignments, 'Work', ['a', 'b', 'c'])).toBe(2);
		expect(groupMemberCount(undefined, 'Work', ['a'])).toBe(0);
	});

	it('names the group and how many projects become ungrouped', () => {
		expect(groupDeleteMessage('Work', 2)).toBe('Delete the group "Work"? 2 projects become ungrouped. No project is removed.');
		expect(groupDeleteMessage('Solo', 1)).toBe('Delete the group "Solo"? 1 project becomes ungrouped. No project is removed.');
	});
});
