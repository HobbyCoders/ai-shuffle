/**
 * The work-log row has room for one short string, so these pin which argument
 * each tool contributes — the identifying one, not merely the first present.
 */
import { describe, it, expect } from 'vitest';
import { toolPreview } from './tool-preview';

describe('toolPreview', () => {
	it('shows the command for Bash', () => {
		expect(toolPreview({ name: 'Bash', input: { command: 'npm run build', description: 'Build' } }))
			.toBe('npm run build');
	});

	it('shows the path for file tools', () => {
		expect(toolPreview({ name: 'Read', input: { file_path: 'src/app.css' } })).toBe('src/app.css');
		expect(toolPreview({ name: 'Edit', input: { file_path: 'src/a.ts', old_string: 'x' } })).toBe('src/a.ts');
		expect(toolPreview({ name: 'Write', input: { file_path: 'notes.md', content: 'hi' } })).toBe('notes.md');
	});

	it('shows the pattern for search tools, with a match count when the result states one', () => {
		expect(toolPreview({ name: 'Grep', input: { pattern: 'TODO' }, result: 'Found 12 matches\n…' }))
			.toBe('TODO — 12 matches');
		expect(toolPreview({ name: 'Glob', input: { pattern: '**/*.ts' } })).toBe('**/*.ts');
	});

	it('does not append a count while the call is still streaming', () => {
		expect(toolPreview({ name: 'Grep', input: { pattern: 'x' }, result: 'Found 3 matches', streaming: true }))
			.toBe('x');
	});

	it('shows the url, description and skill name for their tools', () => {
		expect(toolPreview({ name: 'WebFetch', input: { url: 'https://example.com', prompt: 'summarise' } }))
			.toBe('https://example.com');
		expect(toolPreview({ name: 'Task', input: { description: 'Audit rows', prompt: 'long…' } }))
			.toBe('Audit rows');
		expect(toolPreview({ name: 'Skill', input: { skill: 'code-review' } })).toBe('code-review');
	});

	it('falls back to the first stringy argument for an MCP tool', () => {
		expect(toolPreview({ name: 'mcp__chrome__click', input: { ref: 'ref_4' } })).toBe('ref_4');
	});

	it('reads a field out of a still-streaming input buffer', () => {
		expect(toolPreview({ name: 'Bash', partialInput: '{"command": "git status --por' }))
			.toBe('git status --por');
	});

	it('returns an empty string when there is nothing worth showing', () => {
		expect(toolPreview({ name: 'TodoWrite', input: { todos: [] } })).toBe('');
		expect(toolPreview({ name: 'Bash' })).toBe('');
	});

	it('collapses whitespace and truncates very long values', () => {
		const long = 'a'.repeat(400);
		const out = toolPreview({ name: 'Bash', input: { command: `echo   ${long}` } });
		expect(out.length).toBeLessThanOrEqual(160);
		expect(out.endsWith('…')).toBe(true);
	});
});
