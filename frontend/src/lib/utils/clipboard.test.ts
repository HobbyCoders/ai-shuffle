import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyText } from './clipboard';

afterEach(() => {
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
	document.body.innerHTML = '';
	document.getSelection()?.removeAllRanges();
});

function fallback(result = true) {
	const exec = vi.fn(() => result);
	vi.stubGlobal('navigator', {});
	Object.defineProperty(document, 'execCommand', { value: exec, configurable: true });
	return exec;
}

describe('copyText', () => {
	it('writes exact text with the Clipboard API, without needing DOM selection', async () => {
		const exec = fallback();
		const writeText = vi.fn().mockResolvedValue(undefined);
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		const text = '  <div>hello & goodbye</div>\n\tconst x = "🌍";\n';
		await copyText(text);
		expect(writeText).toHaveBeenCalledWith(text);
		expect(exec).not.toHaveBeenCalled();
	});

	it('selects all multiline text synchronously when the API is absent (HTTP / WebView)', async () => {
		const exec = fallback();
		const text = 'first line\n  second line\n🌍';
		exec.mockImplementation(() => {
			const field = document.activeElement as HTMLTextAreaElement;
			expect(field.tagName).toBe('TEXTAREA');
			expect(field.value).toBe(text);
			expect(field.readOnly).toBe(true);
			expect(field.selectionStart).toBe(0);
			expect(field.selectionEnd).toBe(text.length);
			return true;
		});
		const pending = copyText(text);
		expect(exec).toHaveBeenCalledWith('copy');
		await pending;
		expect(document.querySelector('textarea')).toBeNull();
	});

	it.each(['rejection', 'throw'])('falls back after a clipboard %s', async (mode) => {
		const exec = fallback();
		const writeText = vi.fn(() => {
			if (mode === 'throw') throw new Error('blocked');
			return Promise.reject(new Error('blocked'));
		});
		vi.stubGlobal('navigator', { clipboard: { writeText } });
		await copyText('ABCD-1234');
		expect(exec).toHaveBeenCalledWith('copy');
	});

	it.each(['false', 'throw'])('rejects and restores focus/selection when fallback returns %s', async (mode) => {
		const exec = fallback(false);
		exec.mockImplementation(() => { if (mode === 'throw') throw new Error('blocked'); return false; });
		const editor = document.createElement('textarea');
		editor.value = 'keep my draft';
		document.body.append(editor);
		editor.focus();
		editor.setSelectionRange(2, 7, 'backward');
		await expect(copyText('code')).rejects.toThrow();
		expect(document.activeElement).toBe(editor);
		expect(editor.value).toBe('keep my draft');
		expect(editor.selectionStart).toBe(2);
		expect(editor.selectionEnd).toBe(7);
		expect(editor.selectionDirection).toBe('backward');
		expect(document.querySelectorAll('textarea')).toHaveLength(1);
	});

	it('keeps fallback inside the focused dialog and restores the selected code', async () => {
		const exec = fallback();
		document.body.innerHTML = '<div role="dialog"><code>ABCD-1234</code><button>Copy</button></div>';
		const button = document.querySelector('button')!;
		button.focus();
		const range = document.createRange();
		range.selectNodeContents(document.querySelector('code')!);
		document.getSelection()!.removeAllRanges();
		document.getSelection()!.addRange(range);
		exec.mockImplementation(() => {
			expect(document.querySelector('[role="dialog"] textarea')).toBe(document.activeElement);
			return true;
		});
		await copyText('ABCD-1234');
		expect(document.activeElement).toBe(button);
		expect(document.getSelection()!.toString()).toBe('ABCD-1234');
	});
});
