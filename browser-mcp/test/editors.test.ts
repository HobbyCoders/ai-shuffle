import { describe, it, expect, vi } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync, symlinkSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright-core';
import { readEditors, editField, uploadField, rememberFileChooser } from '../src/editors.js';

// Browser-dependent lane is opt-in; plain npm test must also work without Chrome.
describe.skipIf(!process.env.CHROMIUM_PATH)('native browser editor bridge', () => {
  it('skips geometry for large hidden forms without losing visible editors or hidden choosers', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, chromiumSandbox: true });
    try {
      const page = await browser.newPage();
      await page.setContent('<div hidden>' + '<input>'.repeat(50000) + '</div><input aria-label="Visible" value="kept"><input type="password" value="synthetic"><input type="file" id="chooser" hidden>');
      await page.evaluate(() => {
        const original = Element.prototype.getBoundingClientRect;
        Element.prototype.getBoundingClientRect = function() {
          if (this.closest('[hidden]')) throw new Error('hidden field geometry read');
          return original.call(this);
        };
      });
      expect(await readEditors(page)).toMatchObject([
        { label: 'Visible', value: 'kept' }, { type: 'password', value: '' },
      ]);
      const input = await page.locator('#chooser').elementHandle();
      await rememberFileChooser(page, input!);
      expect((await readEditors(page))[0]).toMatchObject({ type: 'file', chooser: true });
    } finally { await browser.close(); }
  });

  it('uploads real bytes, supports hidden chooser targets, and rejects escaped or stale paths', async () => {
    const folder = mkdtempSync(join(tmpdir(), 'browser-picker-'));
    const outside = mkdtempSync(join(tmpdir(), 'browser-picker-outside-'));
    const outsideFile = join(outside, 'outside.txt');
    const escape = join(folder, 'escape');
    writeFileSync(outsideFile, 'outside upload bytes');
    const allowed = join(folder, 'allowed.txt');
    writeFileSync(allowed, 'original upload bytes');
    vi.stubEnv('AIS_BROWSER_PANEL_UPLOAD_DIR', folder);
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'], headless: true });
    try {
      const page = await browser.newPage();
      await page.setContent(`<label for="doc">Documents</label><input type="file" id="doc" accept=".txt" multiple><input id="hidden" type="file" hidden><button onclick="document.querySelector('#hidden').click()">Custom upload</button>`);
      const file = (await readEditors(page)).find(f => f.type === 'file')!;
      expect(file).toMatchObject({ label: 'Documents', multiple: true, accept: '.txt' });
      await uploadField(page, {field: file.id, paths: [allowed]});
      expect(await page.locator('#doc').evaluate(async (el: HTMLInputElement) => [el.files![0].name, await el.files![0].text()])).toEqual(['allowed.txt', 'original upload bytes']);
      await expect(uploadField(page, {field: file.id, paths: [outsideFile]})).rejects.toThrow('outside');
      // Directory junctions work without Windows symlink privileges.
      symlinkSync(outside, escape, process.platform === 'win32' ? 'junction' : 'dir');
      try {
        await expect(uploadField(page, {field: file.id, paths: [join(escape, 'outside.txt')]})).rejects.toThrow('outside');
      } finally { unlinkSync(escape); }
      const chooserEvent = page.waitForEvent('filechooser');
      await page.getByText('Custom upload').click();
      await rememberFileChooser(page, (await chooserEvent).element());
      const pending = (await readEditors(page)).find(f => f.chooser)!;
      expect(pending.type).toBe('file');
      await uploadField(page, {field: pending.id, paths: [allowed]});
      expect(await page.locator('#hidden').evaluate((el: HTMLInputElement) => el.files![0].name)).toBe('allowed.txt');
      expect((await readEditors(page)).some(f => f.chooser)).toBe(false);
      await page.setContent('<iframe srcdoc="&lt;input type=file id=inner&gt;"></iframe>');
      const frame = page.frames()[1];
      const frameChooser = page.waitForEvent('filechooser');
      await frame.locator('#inner').click();
      await rememberFileChooser(page, (await frameChooser).element());
      const nested = (await readEditors(page)).find(f => f.chooser)!;
      await uploadField(page, {field: nested.id, paths: [allowed]});
      expect(await frame.locator('#inner').evaluate((el: HTMLInputElement) => el.files![0].name)).toBe('allowed.txt');
      expect((await readEditors(page)).some(f => f.chooser)).toBe(false);
      await expect(uploadField(page, {field: file.id, paths: [allowed]})).rejects.toThrow('changed');
    } finally { await browser.close(); rmSync(folder, {recursive:true, force:true}); rmSync(outside, {recursive:true, force:true}); vi.unstubAllEnvs(); }
  });
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
