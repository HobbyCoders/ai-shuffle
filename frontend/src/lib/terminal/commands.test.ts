import { describe, it, expect, beforeEach } from 'vitest';
import { commandBlock, fenceKeys, stripPrompts, registerCommandBlock, lookupCommandBlock, clearCommandBlocks } from './commands';
import { renderCodeBlock } from '$lib/utils/chat-markdown';
import { sanitizeHtml } from '$lib/utils/sanitize';

beforeEach(() => clearCommandBlocks());

describe('commandBlock', () => {
	it('treats POSIX shell languages as runnable', () => {
		for (const lang of ['sh', 'bash', 'zsh', 'shell']) {
			expect(commandBlock('ls -la\n', lang, lang)?.command).toBe('ls -la');
		}
	});

	it('leaves other languages alone unless the fence says run', () => {
		expect(commandBlock('print(1)', 'python', 'python')).toBeNull();
		expect(commandBlock('make test', 'text run', 'text')?.command).toBe('make test');
		// `run` must be a bare word after the language, not part of a value.
		expect(commandBlock('x', 'text cwd="/run"', 'text')).toBeNull();
	});

	it('does not make Windows shells runnable', () => {
		expect(commandBlock('dir', 'powershell', 'powershell')).toBeNull();
		expect(commandBlock('dir', 'cmd', 'cmd')).toBeNull();
	});

	it('reads cwd, shell, on and target from the fence', () => {
		const block = commandBlock('npm test', 'bash cwd="/workspace/app" shell=zsh on=DesktopWorker target=host', 'bash');
		expect(block).toEqual({ command: 'npm test', cwd: '/workspace/app', shell: 'zsh', on: 'DesktopWorker', host: true });
	});

	it('returns null for an empty block', () => {
		expect(commandBlock('\n\n', 'bash', 'bash')).toBeNull();
	});

	it('keeps only the prompt lines of a transcript', () => {
		const code = '$ git status\nOn branch main\n$ ls\nREADME.md';
		expect(commandBlock(code, 'console', 'console')?.command).toBe('git status\nls');
		expect(commandBlock('no prompt here', 'console', 'console')).toBeNull();
	});
});

describe('stripPrompts', () => {
	it('strips a prompt on every line of a plain block', () => {
		expect(stripPrompts('$ a\n$ b', false)).toBe('a\nb');
	});
	it('keeps a plain block with mixed lines unchanged', () => {
		expect(stripPrompts('$ a\necho $HOME', false)).toBe('$ a\necho $HOME');
	});
});

describe('fenceKeys', () => {
	it('parses quoted and bare values', () => {
		expect(fenceKeys(`bash cwd='/a b' on="Unraid" shell=bash`)).toEqual({ cwd: '/a b', on: 'Unraid', shell: 'bash' });
	});
});

describe('registry', () => {
	it('returns a random id that resolves to the block', () => {
		const block = commandBlock('ls', 'bash', 'bash')!;
		const id = registerCommandBlock(block, 'bash');
		expect(id).toMatch(/^cb[0-9a-f]{24}$/);
		expect(lookupCommandBlock(id)).toEqual(block);
	});

	it('reuses the id for the same block', () => {
		const block = commandBlock('ls', 'bash', 'bash')!;
		expect(registerCommandBlock(block, 'bash')).toBe(registerCommandBlock({ ...block }, 'bash'));
	});

	it('does not resolve forged or missing ids', () => {
		expect(lookupCommandBlock('cb000000000000000000000000')).toBeNull();
		expect(lookupCommandBlock(null)).toBeNull();
	});
});

describe('renderCodeBlock', () => {
	it('tags a settled shell block with an id that survives the sanitizer', () => {
		const html = sanitizeHtml(renderCodeBlock('echo hi', 'bash', true));
		const id = /data-cb-id="(cb[0-9a-f]+)"/.exec(html)?.[1];
		expect(id).toBeTruthy();
		expect(lookupCommandBlock(id)?.command).toBe('echo hi');
	});

	it('does not tag a streaming block or a non-shell block', () => {
		expect(renderCodeBlock('echo hi', 'bash', false)).not.toContain('data-cb-id');
		expect(renderCodeBlock('x = 1', 'python', true)).not.toContain('data-cb-id');
	});

	it('runs the registry command, not what the HTML shows', () => {
		// An agent can write HTML with a data-cb-id, but it cannot know a real id.
		const forged = sanitizeHtml('<div class="codeblock" data-cb-id="cbdeadbeefdeadbeefdeadbeef"><pre>rm -rf /</pre></div>');
		const id = /data-cb-id="([^"]+)"/.exec(forged)?.[1];
		expect(lookupCommandBlock(id)).toBeNull();
	});
});
