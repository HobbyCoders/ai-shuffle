import { describe, expect, it } from 'vitest';
import { detectProjectMismatch, projectAliases } from './project-mismatch';

const projects = [
	{ id: 'shuffle', name: 'ai-shuffle' },
	{ id: 'evrima', name: 'evrima-server-toolkit' },
	{ id: 'web', name: 'web' }
];

describe('wrong-project nudge', () => {
	it('derives prose aliases but skips generic words', () => {
		expect(projectAliases('ai-shuffle')).toEqual(['ai shuffle', 'aishuffle']);
		expect(projectAliases('evrima-server-toolkit')).toContain('evrima');
		expect(projectAliases('web')).toEqual([]);
	});

	it('suggests the named project when another one is selected', () => {
		expect(detectProjectMismatch('Add a setting to AI Shuffle for mid-session permissions', projects, 'evrima'))
			?.toMatchObject({ id: 'shuffle' });
		expect(detectProjectMismatch('Why does the Evrima map not load after deploy?', projects, 'shuffle'))
			?.toMatchObject({ id: 'evrima' });
	});

	it('stays quiet when the selected project is named, nothing matches, or it is ambiguous', () => {
		expect(detectProjectMismatch('Port the AI Shuffle usage card to evrima', projects, 'shuffle')).toBeNull();
		expect(detectProjectMismatch('Fix the autoscroll in the chat', projects, 'shuffle')).toBeNull();
		expect(detectProjectMismatch('compare ai-shuffle and evrima releases', projects, null)).toBeNull();
		expect(detectProjectMismatch('shuffled the web list', projects, 'evrima')).toBeNull();
	});
});
