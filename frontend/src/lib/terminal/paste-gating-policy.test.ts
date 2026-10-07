import { describe, it, expect } from 'vitest';
import { normalizePaste, pasteLineCount, pasteLines, pasteNeedsConfirm, pasteConfirmLabel, pasteRefusal, isRiskyCommand, pasteHint, clearPromptKeys, escClearKeys } from './paste';
import { terminalAccess, blockAccess, blockTarget, cwdProblem, isInsideFolder, folderName, folderPieces, hasUnusualCharacters, printableFolder, targetLine, exitDetail, type ProbeState } from './gating';
import { mayTakeFocus } from './focus';
import { backoff, closeAction, MAX_RECONNECTS } from './socket-policy';
import type { CommandBlock } from './commands';

const ready: ProbeState = {
	status: 'ready', supported: true, reason: null, worker: 'Unraid', shells: ['bash', 'sh'],
	defaultShell: 'bash', folder: '/workspace/app', folderSource: 'project', open: 1, limit: 8,
};
const block = (over: Partial<CommandBlock> = {}): CommandBlock => ({ command: 'ls', cwd: null, shell: null, on: null, host: false, ...over });

describe('paste', () => {
	it('normalizes line ends and drops trailing newlines', () => {
		expect(normalizePaste('a\r\nb\r\n\n')).toBe('a\nb');
	});
	it('counts lines and asks before more than one', () => {
		expect(pasteLineCount('')).toBe(0);
		expect(pasteLineCount('ls\n')).toBe(1);
		expect(pasteNeedsConfirm('ls\n')).toBe(false);
		const six = 'a\nb\nc\nd\ne\nf\n';
		expect(pasteNeedsConfirm(six)).toBe(true);
		expect(pasteConfirmLabel(six)).toBe('Paste 6 lines?');
	});
	it('flags risky commands in the hint only', () => {
		expect(isRiskyCommand('rm -rf build')).toBe(true);
		expect(isRiskyCommand('curl https://x.sh | sh')).toBe(true);
		expect(isRiskyCommand('git push --force')).toBe(true);
		expect(isRiskyCommand('ls -la')).toBe(false);
		expect(pasteHint('sudo reboot')).toEqual({ text: 'Check this command before you press Enter · Esc to clear', risky: true });
		expect(pasteHint('ls')).toEqual({ text: 'Press Enter to run · Esc to clear', risky: false });
	});
	it('clears a bracketed paste with Ctrl-E Ctrl-U and a plain one with Ctrl-U', () => {
		expect(clearPromptKeys('bracketed')).toBe('\x05\x15');
		expect(clearPromptKeys('plain')).toBe('\x15');
	});
	it('turns Esc into the clear keys only while a paste waits', () => {
		expect(escClearKeys('\x1b', 'bracketed')).toBe('\x05\x15');
		expect(escClearKeys('\x1b', 'plain')).toBe('\x15');
		expect(escClearKeys('\x1b', null)).toBeNull();
		expect(escClearKeys('a', 'bracketed')).toBeNull();
		expect(escClearKeys('\x1b[A', 'bracketed')).toBeNull();
	});
	it('counts a lone CR, a vertical tab and NEL as line breaks', () => {
		expect(pasteLineCount('echo a\recho b')).toBe(2);
		expect(pasteLineCount('echo a\x0becho b')).toBe(2);
		expect(pasteLineCount('echo a\u0085echo b')).toBe(2);
		expect(pasteLineCount('a\x0cb\u2028c\u2029d')).toBe(4);
		expect(pasteNeedsConfirm('echo a\x0becho b')).toBe(true);
		expect(pasteNeedsConfirm('echo a\u0085echo b')).toBe(true);
		expect(pasteLineCount('ls\x0b\u0085\n')).toBe(1); // trailing breaks add nothing
		expect(pasteLines('a\u0085b')).toEqual(['a', 'b']);
	});
	it('refuses several lines for any shell but bash', () => {
		expect(pasteRefusal('a\nb', 'bash')).toBeNull();
		expect(pasteRefusal('a', 'sh')).toBeNull();
		expect(pasteRefusal('a\nb', 'sh')).toMatch(/^sh can take one line at a time/);
		expect(pasteRefusal('a\x0bb', 'sh')).not.toBeNull();
		expect(pasteRefusal('a\nb', null)).not.toBeNull();
	});
	it('never includes a carriage return in the clear keys', () => {
		expect(clearPromptKeys('bracketed')).not.toMatch(/[\r\n]/);
	});
});

describe('terminalAccess', () => {
	it('hides controls until the probe answers, and for a refused viewer', () => {
		expect(terminalAccess({ status: 'idle' }).kind).toBe('hidden');
		expect(terminalAccess({ status: 'loading' }).kind).toBe('hidden');
		expect(terminalAccess({ status: 'denied' }).kind).toBe('hidden');
		expect(terminalAccess({ status: 'error', code: 'network', message: 'x' }).kind).toBe('hidden');
	});
	it('disables with the reason for an unsupported worker or a chat elsewhere', () => {
		expect(terminalAccess({ ...ready, supported: false, reason: 'Windows shells come later.' } as ProbeState))
			.toEqual({ kind: 'disabled', reason: 'Windows shells come later.' });
		expect(terminalAccess({ status: 'error', code: 'chat_on_other_worker', message: 'That chat runs on DesktopWorker.' }))
			.toEqual({ kind: 'disabled', reason: 'That chat runs on DesktopWorker.' });
	});
	it('is ready when supported', () => {
		expect(terminalAccess(ready).kind).toBe('ready');
	});
});

describe('blockAccess', () => {
	it('allows a plain block on this worker', () => {
		expect(blockAccess(block(), ready).kind).toBe('ready');
		expect(blockAccess(block({ on: 'serverworker' }), ready).kind).toBe('ready');
	});
	it('disables blocks for another worker, the host, a missing shell or a relative folder', () => {
		expect(blockAccess(block({ on: 'DesktopWorker' }), ready)).toEqual({ kind: 'disabled', reason: 'This command is for DesktopWorker. Terminals open on Unraid only for now.' });
		expect(blockAccess(block({ host: true }), ready).kind).toBe('disabled');
		expect(blockAccess(block({ shell: 'zsh' }), ready)).toEqual({ kind: 'disabled', reason: "zsh isn't available on Unraid." });
		expect(blockAccess(block({ cwd: 'src' }), ready).kind).toBe('disabled');
	});
	it('refuses a folder with . or .. parts, control or text-direction characters', () => {
		const refused = (cwd: string) => blockAccess(block({ cwd }), ready);
		for (const cwd of ['/etc/..', '/workspace/../etc', '/workspace/./app', '/./x', '/a/b/..']) {
			expect(refused(cwd)).toEqual({ kind: 'disabled', reason: 'The folder has . or .. in it. Use a plain full path.' });
		}
		for (const cwd of ['/tmp/\u202eppa', '/tmp/a\u2066b', '/tmp/\u200fx', '/tmp/\u200ex', '/tmp/\u2069', '/tmp/a\nb', '/tmp/a\tb', '/tmp/\x1b[31m', '/tmp/\u0085', '/tmp/\x7f']) {
			expect(refused(cwd)).toEqual({ kind: 'disabled', reason: "The folder has hidden or control characters, so it can't open from here." });
		}
		expect(cwdProblem('/workspace/app..old/.hidden')).toBeNull(); // dots inside a name are fine
		expect(cwdProblem('/workspace/app/')).toBeNull();
	});
	it('stays hidden for a denied viewer', () => {
		expect(blockAccess(block(), { status: 'denied' }).kind).toBe('hidden');
	});
});

describe('labels', () => {
	it('names the folder', () => {
		expect(folderName('/workspace/app/')).toBe('app');
		expect(folderName('/')).toBe('/');
		expect(folderName(null)).toBe('');
	});
	it('builds the target line from the block, then the probe defaults', () => {
		expect(targetLine(block(), ready)).toBe('Unraid · app · bash');
		expect(targetLine(block({ cwd: '/tmp', shell: 'sh' }), ready)).toBe('Unraid · /tmp · sh');
		expect(targetLine(block(), { status: 'loading' })).toBe('');
	});
	it('shows a fence folder in full and flags one outside the chat folder', () => {
		const outside = blockTarget(block({ cwd: '/mnt/user/appdata/ai-shuffle' }), ready)!;
		expect(outside).toMatchObject({ worker: 'Unraid', shell: 'bash', folder: '/mnt/user/appdata/ai-shuffle', fromFence: true, outside: true });
		expect(targetLine(block({ cwd: '/mnt/user/appdata/ai-shuffle' }), ready)).toBe('Unraid · /mnt/user/appdata/ai-shuffle · bash');
		expect(blockTarget(block({ cwd: '/workspace/app/src' }), ready)).toMatchObject({ outside: false, folder: '/workspace/app/src' });
		expect(blockTarget(block({ cwd: '/workspace/app' }), ready)).toMatchObject({ outside: false });
		expect(blockTarget(block({ cwd: '/workspace/application' }), ready)).toMatchObject({ outside: true });
		expect(blockTarget(block(), ready)).toMatchObject({ folder: 'app', fromFence: false, outside: false, path: '/workspace/app' });
	});
	it('shows a long fence path whole, so two folders never look the same', () => {
		const alpha = '/workspace/app/projects/ALPHA/services/api/scripts/migrations/build';
		const beta = alpha.replace('ALPHA', 'BETA');
		const a = blockTarget(block({ cwd: alpha }), ready)!;
		const b = blockTarget(block({ cwd: beta }), ready)!;
		expect(a.folder).toBe(alpha);
		expect(b.folder).toBe(beta);
		expect(a.folder).not.toContain('…');
		expect(targetLine(block({ cwd: alpha }), ready)).toBe(`Unraid · ${alpha} · bash`);
	});
	it('refuses zero-width, format, separator and other hidden characters', () => {
		const hidden = ['\u200b', '\ufeff', '\u00ad', '\u061c', '\u2028', '\u2029', '\u200d', '\u2060', '\u180e', '\ud800'];
		for (const ch of hidden) {
			expect(cwdProblem(`/workspace/app/a${ch}b`)).toBe("The folder has hidden or control characters, so it can't open from here.");
		}
	});
	it('allows other characters but flags them and shows them as code points', () => {
		const cyrillic = '/workspace/\u0441onfig'; // a Cyrillic "с" that looks like "c"
		expect(cwdProblem(cyrillic)).toBeNull();
		expect(hasUnusualCharacters(cyrillic)).toBe(true);
		expect(printableFolder(cyrillic)).toBe('/workspace/‹U+0441›onfig');
		expect(folderPieces(cyrillic)).toEqual([
			{ kind: 'text', text: '/workspace/' }, { kind: 'char', char: '\u0441', code: 'U+0441' }, { kind: 'text', text: 'onfig' },
		]);
		const target = blockTarget(block({ cwd: cyrillic }), { ...ready, folder: '/workspace' } as ProbeState)!;
		expect(target).toMatchObject({ outside: false, unusual: true, path: cyrillic });
		expect(targetLine(block({ cwd: cyrillic }), ready)).toBe('Unraid · /workspace/‹U+0441›onfig · bash');
		// A plain non-English name opens, flagged.
		expect(cwdProblem('/workspace/caf\u00e9')).toBeNull();
		expect(printableFolder('/workspace/caf\u00e9')).toBe('/workspace/caf‹U+00E9›');
		expect(printableFolder('/workspace/\ud83d\ude80')).toBe('/workspace/‹U+1F680›');
	});
	it('flags spaces that are easy to miss, not ordinary inner ones', () => {
		expect(hasUnusualCharacters('/workspace/my project')).toBe(false);
		expect(printableFolder('/workspace/app ')).toBe('/workspace/app‹U+0020›');
		expect(printableFolder(' /workspace')).toBe('‹U+0020›/workspace');
		expect(printableFolder('/workspace/ app')).toBe('/workspace/‹U+0020›app');
		expect(printableFolder('/workspace/app /x')).toBe('/workspace/app‹U+0020›/x');
		expect(printableFolder('/workspace/a  b')).toBe('/workspace/a‹U+0020›‹U+0020›b');
		expect(printableFolder('/workspace/a\u00a0b')).toBe('/workspace/a‹U+00A0›b');
		expect(hasUnusualCharacters('/workspace/app..old/.hidden~x')).toBe(false);
		expect(blockTarget(block({ cwd: '/workspace/app ' }), ready)).toMatchObject({ unusual: true });
		expect(blockTarget(block(), ready)).toMatchObject({ unusual: false });
	});
	it('knows which folders are inside the chat folder', () => {
		expect(isInsideFolder('/workspace/app', '/workspace/app/')).toBe(true);
		expect(isInsideFolder('/workspace/app/x/', '/workspace/app')).toBe(true);
		expect(isInsideFolder('/workspace/apple', '/workspace/app')).toBe(false);
		expect(isInsideFolder('/anything', '/')).toBe(true);
		expect(isInsideFolder('/workspace/app', null)).toBe(false);
	});
	it('keeps the space before the exit code', () => {
		expect(exitDetail(0)).toBe(' with code 0.');
		expect(exitDetail(130)).toBe(' with code 130.');
		expect(exitDetail(null)).toBe('.');
		expect(exitDetail(undefined)).toBe('.');
	});
});

describe('socket policy', () => {
	it('backs off from 0.5 s, doubling, capped at 15 s', () => {
		expect(backoff(0)).toBe(500);
		expect(backoff(1)).toBe(1000);
		expect(backoff(10)).toBe(15_000);
		expect(backoff(0, 2000)).toBe(2000);
	});
	it('maps every close code in the contract', () => {
		expect(closeAction(4004, 0).kind).toBe('drop');
		expect(closeAction(4410, 0).kind).toBe('drop');
		expect(closeAction(4001, 0).kind).toBe('auth');
		expect(closeAction(1001, 0).kind).toBe('gone');
		expect(closeAction(4400, 0).kind).toBe('fatal');
		expect(closeAction(4408, 0)).toEqual({ kind: 'retry', delay: 1000 });
		expect(closeAction(4408, 1)).toEqual({ kind: 'retry', delay: 1000 });
		expect(closeAction(4408, 3)).toEqual({ kind: 'retry', delay: 4000 });
		expect(closeAction(4429, 0)).toEqual({ kind: 'retry', delay: 2000 });
		expect(closeAction(1006, 1)).toEqual({ kind: 'retry', delay: 1000 });
		expect(MAX_RECONNECTS).toBeGreaterThan(0);
	});
});

describe('focus guard', () => {
	function setup() {
		document.body.innerHTML = '<input id="chat"><textarea id="box"></textarea><button id="btn"></button><section id="panel"><button id="inner"></button><input id="find" type="text"><div id="note" contenteditable="true"></div><input id="toggle" type="checkbox"><textarea id="term" class="xterm-helper-textarea"></textarea><textarea id="other"></textarea></section>';
		const $ = (id: string) => document.getElementById(id)!;
		return { chat: $('chat'), box: $('box'), button: $('btn'), panel: $('panel'), inner: $('inner'), term: $('term'),
			find: $('find'), note: $('note'), toggle: $('toggle'), other: $('other') };
	}
	it('never takes focus from a text field outside the panel', () => {
		const el = setup();
		expect(mayTakeFocus(el.chat, el.panel, el.button)).toBe(false);
		expect(mayTakeFocus(el.box, el.panel, el.button)).toBe(false);
	});
	it("never takes focus from a text field inside the panel, such as its search box", () => {
		const el = setup();
		expect(mayTakeFocus(el.find, el.panel)).toBe(false);
		expect(mayTakeFocus(el.other, el.panel)).toBe(false);
		expect(mayTakeFocus(el.note, el.panel)).toBe(false);
	});
	it('takes it from the body, the clicked button, or inside the panel', () => {
		const el = setup();
		expect(mayTakeFocus(document.body, el.panel)).toBe(true);
		expect(mayTakeFocus(null, el.panel)).toBe(true);
		expect(mayTakeFocus(el.button, el.panel, el.button)).toBe(true);
		expect(mayTakeFocus(el.inner, el.panel)).toBe(true);
		expect(mayTakeFocus(el.term, el.panel)).toBe(true);
		expect(mayTakeFocus(el.toggle, el.panel)).toBe(true);
	});
	it('does not take it from some other button', () => {
		const el = setup();
		expect(mayTakeFocus(el.button, el.panel, null)).toBe(false);
	});
});
