import { describe, it, expect } from 'vitest';
import { chromium } from 'playwright-core';
import { readEditors, editField } from '../src/editors.js';

// Browser-dependent lane is opt-in; plain npm test must also work without Chrome.
describe.skipIf(!process.env.CHROMIUM_PATH)('native browser editor bridge', () => {
  it('discovers real fields, applies edits and rejects replaced nodes without leaking passwords', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.route('http://example.test/**', route => route.fulfill({ body: '<html></html>', contentType: 'text/html' }));
      await page.goto('http://example.test/');
      await page.setContent(`<input aria-label="Message" value="existing" oninput="document.title=this.value"><textarea aria-label="Notes">hello</textarea><input type="password" value="secret"><input disabled><input readonly>`);
      const fields = await readEditors(page);
      expect(fields).toHaveLength(3);
      const field = fields.find(f => f.label === 'Message')!;
      expect(field.value).toBe('existing');
      expect(field.width).toBeGreaterThan(0);
      expect(fields.find(f => f.type === 'password')!.value).toBe('');
      await editField(page, { field: field.id, action: 'focus' });
      await editField(page, { field: field.id, action: 'replace', value: 'pasted 😀', start: 7, end: 9 });
      expect(await page.title()).toBe('pasted 😀');
      expect(await page.locator('input').first().evaluate(el => [(el as HTMLInputElement).selectionStart, (el as HTMLInputElement).selectionEnd])).toEqual([7, 9]);
      expect((await readEditors(page))[0].id).toBe(field.id);
      await page.locator('input').first().evaluate(el => el.replaceWith(el.cloneNode()));
      await expect(editField(page, { field: field.id, action: 'replace', value: 'wrong', start: 0, end: 0 })).rejects.toThrow('Text field changed');
    } finally { await browser.close(); }
  });
});
