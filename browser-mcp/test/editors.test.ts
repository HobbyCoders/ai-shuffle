import { describe, it, expect, vi } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync, symlinkSync, unlinkSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { chromium } from 'playwright-core';
import { readEditors, readChoices, editField, uploadField, rememberFileChooser, copySelection, COPY_LIMIT, MAX_CHOICES, CHOICE_TEXT_BUDGET } from '../src/editors.js';

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

  it('leaves combobox-like fields to the page but keeps plain inputs', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<input role="combobox"><input aria-haspopup="listbox"><input aria-haspopup="true">
        <input aria-autocomplete="list"><input aria-autocomplete="inline"><input aria-autocomplete="both">
        <input list="choices"><datalist id="choices"></datalist>
        <input aria-label="Expanded only" aria-expanded="false"><input aria-label="Autocomplete none" aria-autocomplete="none">
        <input aria-label="Dangling list" list="missing"><div id="not-list"></div><input aria-label="Not a datalist" list="not-list">
        <input aria-label="Plain"><input aria-label="No popup" aria-haspopup="false"><textarea aria-label="Notes"></textarea>`);
      expect((await readEditors(page)).map(f => f.label)).toEqual(['Expanded only', 'Autocomplete none', 'Dangling list', 'Not a datalist', 'Plain', 'No popup', 'Notes']);
    } finally { await browser.close(); }
  });

  it('reports negotiated unscaled typography without password contents', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<input placeholder="Example" style="font:18px/24px monospace;padding:5px 7px;border-left:3px solid;border-top:4px solid">
        <input type="password" value="never broadcast" placeholder="Secret" style="line-height:normal">`);
      expect(await readEditors(page, true)).toMatchObject([
        { style: { fontSize: 18, lineHeight: 24, paddingLeft: 7, paddingTop: 5, fontFamily: 'monospace', borderLeft: 3, borderTop: 4 } },
        { type: 'password', value: '', style: { lineHeight: null } },
      ]);
      expect((await readEditors(page, false))[0]).not.toHaveProperty('style');
    } finally { await browser.close(); }
  });

  it('omits covered fields, including inside nested shadow roots, but accepts labels', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent('<div id="host"></div>');
      await page.evaluate(() => {
        const root = document.querySelector('#host')!.attachShadow({ mode: 'open' });
        root.innerHTML = '<div id="inner"></div>';
        root.querySelector('#inner')!.attachShadow({ mode: 'open' }).innerHTML = `
          <style>input{display:block;width:200px;height:30px}label{position:absolute;left:0;top:0;width:200px;height:30px}
          .row{position:relative}#options{position:absolute;left:0;top:0;width:200px;height:30px;background:white;z-index:2}</style>
          <input aria-label="Visible"><div class="row"><input aria-label="Covered"><div id="options" role="listbox">Option</div></div>
          <div class="row"><input id="labelled"><label for="labelled"><span>Label hit</span></label></div>`;
      });
      expect((await readEditors(page)).map(f => f.label)).toEqual(['Visible', 'Label hit']);
      await page.locator('#options').evaluate(el => el.remove());
      expect((await readEditors(page)).map(f => f.label)).toEqual(['Visible', 'Covered', 'Label hit']);
    } finally { await browser.close(); }
  });

  it('flags split one-time-code boxes for keystrokes so the page can advance focus itself', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<input aria-label="Email"><div id="otp">${Array.from({ length: 6 }, (_, i) => `<input inputmode="numeric" maxlength="1" aria-label="Digit ${i + 1}">`).join('')}</div>
        <script>const boxes = [...document.querySelectorAll('#otp input')];
        boxes.forEach((box, i) => box.addEventListener('input', () => { box.value = box.value.replace(/\\D/g, '').slice(-1); if (box.value) boxes[i + 1]?.focus(); }));</script>`);
      const editors = await readEditors(page);
      expect(editors[0]).not.toHaveProperty('keys');
      expect(editors.slice(1).map(f => f.keys)).toEqual([true, true, true, true, true, true]);
      await editField(page, { field: editors[1].id, action: 'focus' });
      // The panel sends committed text through Input.insertText, as a keyboard would.
      for (const digit of '482915') await page.keyboard.insertText(digit);
      expect(await page.locator('#otp input').evaluateAll(els => els.map(el => (el as HTMLInputElement).value).join(''))).toBe('482915');
    } finally { await browser.close(); }
  });

  it('copies the page selection or a selected field range, and refuses passwords and empty selections', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<p id="key" style="width:80px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">sk_test_0123456789abcdef</p>
        <input id="name" value="Ada Lovelace"><input id="secret" type="password" value="hunter2">
        <div id="host"></div><script>document.querySelector('#host').attachShadow({ mode: 'open' }).innerHTML = '<input id="inner" value="shadow value">';</script>`);
      await expect(copySelection(page)).rejects.toThrow('Select some text');
      // Text clipped by CSS is still copied in full.
      await page.evaluate(() => { const r = document.createRange(); r.selectNodeContents(document.querySelector('#key')!); getSelection()!.removeAllRanges(); getSelection()!.addRange(r); });
      expect(await copySelection(page)).toBe('sk_test_0123456789abcdef');
      await page.evaluate(() => { const el = document.querySelector('#name') as HTMLInputElement; el.focus(); el.setSelectionRange(4, 12); });
      expect(await copySelection(page)).toBe('Lovelace');
      await page.evaluate(() => { const el = document.querySelector('#host')!.shadowRoot!.querySelector('input')!; el.focus(); el.setSelectionRange(0, 6); });
      expect(await copySelection(page)).toBe('shadow');
      await page.evaluate(() => { const el = document.querySelector('#secret') as HTMLInputElement; el.focus(); el.select(); });
      await expect(copySelection(page)).rejects.toThrow('Password');
      await page.evaluate((n) => { document.body.textContent = 'x'.repeat(n); const r = document.createRange(); r.selectNodeContents(document.body); getSelection()!.removeAllRanges(); getSelection()!.addRange(r); }, COPY_LIMIT + 1);
      await expect(copySelection(page)).rejects.toThrow('too long');
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
  it('discovers nested shadow auth fields and delivers composed edits without exposing passwords', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, headless: true, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent('<input aria-label="Light DOM"><auth-dialog></auth-dialog>');
      await page.evaluate(() => {
        const dialog = document.querySelector('auth-dialog')!.attachShadow({ mode: 'open' });
        dialog.innerHTML = '<faceplate-text-input><span slot="label">Email or username</span></faceplate-text-input><faceplate-text-input id="password"><span slot="label">Password</span></faceplate-text-input><div hidden><input aria-label="Hidden"></div><input disabled><input readonly><textarea aria-label="Notes">before</textarea><input type="file" aria-label="Choose files" accept=".txt" multiple>';
        for (const host of dialog.querySelectorAll('faceplate-text-input')) {
          const password = host.id === 'password';
          host.attachShadow({ mode: 'open' }).innerHTML = `<label><span id="label"><slot name="label"></slot><span aria-hidden="true">*</span></span><input type="${password ? 'password' : 'text'}" aria-labelledby="label" value="${password ? 'synthetic secret' : 'existing'}"></label>`;
          host.addEventListener('input', e => {
            // A listener outside the inner root only sees composed events.
            host.setAttribute('data-edited', String(e.composed));
          });
        }
      });
      const fields = await readEditors(page);
      expect(fields.map(f => f.label)).toEqual(['Light DOM', 'Email or username', 'Password', 'Notes', 'Choose files']);
      expect(fields[4]).toMatchObject({ type: 'file', accept: '.txt', multiple: true, value: '' });
      await editField(page, { field: fields[3].id, action: 'replace', value: 'multiline\ntext', start: 0, end: 0 });
      expect(await page.getByRole('textbox', { name: 'Notes', exact: true }).inputValue()).toBe('multiline\ntext');
      const email = fields[1], password = fields[2];
      expect(email.value).toBe('existing');
      expect(password.value).toBe('');
      const box = await page.getByRole('textbox', { name: 'Email or username' }).boundingBox();
      expect(email).toMatchObject(box!);
      await editField(page, { field: email.id, action: 'focus' });
      await expect(page.getByRole('textbox', { name: 'Email or username' }).evaluate(el => el.matches(':focus'))).resolves.toBe(true);
      await editField(page, { field: email.id, action: 'replace', value: 'synthetic@example.test', start: 4, end: 4 });
      expect(await page.getByRole('textbox', { name: 'Email or username' }).inputValue()).toBe('synthetic@example.test');
      expect(await page.locator('faceplate-text-input').first().getAttribute('data-edited')).toBe('true');
      expect((await readEditors(page))[1].id).toBe(email.id);
      expect((await readEditors(page))[2].value).toBe('');
      await editField(page, { field: email.id, action: 'blur' });
      expect(await page.getByRole('textbox', { name: 'Email or username' }).evaluate(el => el.matches(':focus'))).toBe(false);
      await page.locator('faceplate-text-input').first().evaluate(el => el.remove());
      await expect(editField(page, { field: email.id, action: 'replace', value: 'stale', start: 0, end: 0 })).rejects.toThrow('Text field changed');
    } finally { await browser.close(); }
  });

  it('reports popup controls (select, date and time types) with their limits and focus', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage({ timezoneId: 'Asia/Tokyo' });
      await page.setContent(`<label>Country <select id="c"><option value="">Choose</option><optgroup label="Europe"><option value="fr">France</option><option value="de" selected>Germany</option></optgroup></select></label>
        <select aria-label="Many" multiple><option>a</option></select><select aria-label="Box" size="3"><option>a</option></select><select aria-label="Off" disabled><option>a</option></select>
        <input type="date" aria-label="Day" value="2026-10-21" min="2026-01-01" max="2026-12-31" step="7">
        <input type="time" aria-label="At" step="900"><input type="datetime-local" aria-label="When"><input type="month" aria-label="Month"><input type="week" aria-label="Week">
        <input type="date" aria-label="Fixed" readonly><input type="color" aria-label="Colour"><input type="number" aria-label="Count">
        <fieldset disabled><select aria-label="Fenced"><option>a</option></select><input type="date" aria-label="Fenced day"><input aria-label="Fenced text"></fieldset>`);
      await page.focus('#c');
      const fields = await readEditors(page);
      expect(fields.map(f => [f.label, f.type])).toEqual([
        ['Country', 'select'], ['Day', 'date'], ['At', 'time'], ['When', 'datetime-local'], ['Month', 'month'], ['Week', 'week'],
      ]);
      expect(fields[0]).toMatchObject({ value: 'Germany', focused: true });
      expect(fields[0]).not.toHaveProperty('style');
      // The value attribute travels as `base`: without min, steps count from it.
      expect(fields[1]).toMatchObject({ value: '2026-10-21', min: '2026-01-01', max: '2026-12-31', step: '7', base: '2026-10-21', zone: 'Asia/Tokyo' });
      expect(fields[0]).not.toHaveProperty('zone'); // a list has no Today
      expect(fields[1]).not.toHaveProperty('focused');
      expect(fields[2]).toMatchObject({ step: '900' });
      expect(fields[2]).not.toHaveProperty('min');
      expect(fields[2]).not.toHaveProperty('base');
    } finally { await browser.close(); }
  });

  it('omits the zone when the page overrides Intl with a non-string time zone', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent('<input type="date" aria-label="Day" value="2026-10-21">');
      await page.evaluate(() => {
        Intl.DateTimeFormat = function () { return { resolvedOptions: () => ({ timeZone: { length: 3 } }) }; } as any;
      });
      const [day] = await readEditors(page);
      expect(day).toMatchObject({ label: 'Day', type: 'date', value: '2026-10-21' });
      expect(day).not.toHaveProperty('zone');
    } finally { await browser.close(); }
  });

  it('lists select options with groups and disabled state, bounded, without option values', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<select aria-label="Pick"><option value="secret-a">  Plain
        label </option><option disabled>Off</option><optgroup label="Group A"><option selected>A1</option></optgroup>
        <optgroup label="Closed" disabled><option>C1</option></optgroup><option label="By attribute">ignored</option><option hidden>Concealed</option></select>
        <select aria-label="Huge">${Array.from({ length: 1500 }, (_, i) => `<option>Option ${i}</option>`).join('')}</select>`);
      const [pick, huge] = await readEditors(page);
      expect(await readChoices(page, { field: pick.id })).toEqual({
        selected: 2, groups: ['Group A', 'Closed'],
        options: [
          { label: 'Plain label', disabled: false }, { label: 'Off', disabled: true },
          { label: 'A1', disabled: false, group: 0 }, { label: 'C1', disabled: true, group: 1 }, { label: 'By attribute', disabled: false },
          { label: 'Concealed', disabled: true }, // the page's own popup would not offer it
        ],
      });
      const many = await readChoices(page, { field: huge.id });
      expect(many.options).toHaveLength(MAX_CHOICES);
      expect(many.truncated).toBe(true);
      expect(JSON.stringify(many)).not.toContain('secret-a');
      await page.locator('select').first().evaluate(el => el.remove());
      await expect(readChoices(page, { field: pick.id })).rejects.toThrow('changed');
    } finally { await browser.close(); }
  });

  it('counts group labels and code points against the list budget, as the server does', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      // 120 options of 200 characters fill the budget exactly: the group's own
      // label tips it over, so the last option must be left off.
      const long = (i: number) => String(i).padStart(3, '0') + 'x'.repeat(197);
      await page.setContent(`<select aria-label="Grouped"><optgroup label="G">${Array.from({ length: 120 }, (_, i) => `<option>${long(i)}</option>`).join('')}</optgroup></select>
        <select aria-label="Exact"><optgroup label="G">${Array.from({ length: 119 }, (_, i) => `<option>${long(i)}</option>`).join('')}<option>${'y'.repeat(199)}</option></optgroup></select>
        <select aria-label="Wide"><option>${'😀'.repeat(150)}</option><option>${'😀'.repeat(150)}</option></select>
        <select aria-label="Odd"><option>a\u0001b\u0085c</option></select>`);
      const fields = await readEditors(page);
      const choices = (label: string) => readChoices(page, { field: fields.find(f => f.label === label)!.id });
      const size = (c: Awaited<ReturnType<typeof readChoices>>) => [...c.groups, ...c.options.map(o => o.label)].reduce((n, t) => n + Array.from(t).length, 0);
      const grouped = await choices('Grouped');
      expect(grouped).toMatchObject({ groups: ['G'], truncated: true });
      expect(grouped.options).toHaveLength(119);
      expect(size(grouped)).toBeLessThanOrEqual(CHOICE_TEXT_BUDGET);
      const exact = await choices('Exact');
      expect(exact.truncated).toBeUndefined();
      expect(size(exact)).toBe(CHOICE_TEXT_BUDGET);
      // Emoji are one code point each (the server's len()), two UTF-16 units.
      const wide = await choices('Wide');
      expect(wide.options.map(o => Array.from(o.label).length)).toEqual([150, 150]);
      expect(wide.truncated).toBeUndefined();
      // Control characters are not kept: each would cost 6 bytes of JSON.
      expect((await choices('Odd')).options[0].label).toBe('a b c');
    } finally { await browser.close(); }
  });

  it('sets a choice in the page and fires input then change like the native popup', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<select aria-label="Pick"><option>One</option><option disabled>Two</option><option>Three</option></select>
        <input type="date" aria-label="Day" value="2026-10-21"><input type="time" aria-label="At"><input type="datetime-local" aria-label="When">
        <input type="month" aria-label="Month"><input type="week" aria-label="Week">
        <script>window.log = []; for (const el of document.querySelectorAll('select, input'))
          for (const type of ['input', 'change']) el.addEventListener(type, e => log.push([el.getAttribute('aria-label'), type, el.value, e.bubbles, document.activeElement === el]));</script>`);
      const fields = await readEditors(page);
      const byLabel = (label: string) => fields.find(f => f.label === label)!.id;
      await editField(page, { field: byLabel('Pick'), action: 'set', index: 2, label: 'Three' });
      await editField(page, { field: byLabel('Day'), action: 'set', value: '2027-01-05' });
      await editField(page, { field: byLabel('At'), action: 'set', value: '13:45' });
      await editField(page, { field: byLabel('When'), action: 'set', value: '2026-10-21T08:30' });
      await editField(page, { field: byLabel('Month'), action: 'set', value: '2026-11' });
      await editField(page, { field: byLabel('Week'), action: 'set', value: '2026-W43' });
      await editField(page, { field: byLabel('Day'), action: 'set', value: '' });
      expect(await page.evaluate(() => (window as unknown as { log: unknown[] }).log)).toEqual([
        ['Pick', 'input', 'Three', true, true], ['Pick', 'change', 'Three', true, true],
        ['Day', 'input', '2027-01-05', true, true], ['Day', 'change', '2027-01-05', true, true],
        ['At', 'input', '13:45', true, true], ['At', 'change', '13:45', true, true],
        ['When', 'input', '2026-10-21T08:30', true, true], ['When', 'change', '2026-10-21T08:30', true, true],
        ['Month', 'input', '2026-11', true, true], ['Month', 'change', '2026-11', true, true],
        ['Week', 'input', '2026-W43', true, true], ['Week', 'change', '2026-W43', true, true],
        ['Day', 'input', '', true, true], ['Day', 'change', '', true, true],
      ]);
      // The same value again is not a change; a disabled option, a malformed value
      // and a text field are refused without touching the page.
      await editField(page, { field: byLabel('Pick'), action: 'set', index: 2, label: 'Three' });
      await expect(editField(page, { field: byLabel('Pick'), action: 'set', index: 1, label: 'Two' })).rejects.toThrow('unavailable');
      await expect(editField(page, { field: byLabel('Pick'), action: 'set', index: 9, label: 'Nine' })).rejects.toThrow('This list changed');
      await expect(editField(page, { field: byLabel('At'), action: 'set', value: 'noon' })).rejects.toThrow("didn't accept");
      expect(await page.locator('input[type=time]').inputValue()).toBe('13:45');
      await expect(editField(page, { field: byLabel('Pick'), action: 'replace', value: 'x', start: 0, end: 0 })).rejects.toThrow();
      expect((await page.evaluate(() => (window as unknown as { log: unknown[] }).log)).length).toBe(14);
    } finally { await browser.close(); }
  });

  it('refuses a choice when the list was refilled, or the option or control became unusable', async () => {
    const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH, args: ['--no-sandbox'] });
    try {
      const page = await browser.newPage();
      await page.setContent(`<select aria-label="City"><option>Paris</option><option>  Lyon\n </option><option hidden>Secret</option></select>
        <fieldset><select aria-label="Later"><option>a</option><option>b</option></select><input type="date" aria-label="Day"></fieldset>
        <script>window.log = []; for (const el of document.querySelectorAll('select, input')) el.addEventListener('change', () => log.push(el.getAttribute('aria-label')));</script>`);
      const fields = await readEditors(page);
      const byLabel = (label: string) => fields.find(f => f.label === label)!.id;
      const city = byLabel('City');
      const shown = await readChoices(page, { field: city });
      // A dependent dropdown refills between opening the list and choosing.
      await page.locator('select').first().evaluate(el => { el.innerHTML = '<option>Berlin</option><option>Munich</option>'; });
      await expect(editField(page, { field: city, action: 'set', index: 1, label: shown.options[1].label })).rejects.toThrow('This list changed');
      expect(await page.locator('select').first().inputValue()).toBe('Berlin');
      await page.locator('select').first().evaluate(el => { el.innerHTML = '<option>Paris</option><option>  Lyon\n </option><option hidden>Secret</option>'; });
      await editField(page, { field: city, action: 'set', index: 1, label: shown.options[1].label }); // the cleaned label still matches
      await expect(editField(page, { field: city, action: 'set', index: 2, label: 'Secret' })).rejects.toThrow('unavailable');
      // A fieldset disabled after the scan: the page would not let the user change these.
      await page.locator('fieldset').evaluate(el => { (el as HTMLFieldSetElement).disabled = true; });
      await expect(editField(page, { field: byLabel('Later'), action: 'set', index: 1, label: 'b' })).rejects.toThrow();
      await expect(editField(page, { field: byLabel('Day'), action: 'set', value: '2026-10-21' })).rejects.toThrow();
      await expect(readChoices(page, { field: byLabel('Later') })).rejects.toThrow('changed');
      expect(await page.evaluate(() => (window as unknown as { log: unknown[] }).log)).toEqual(['City']);
    } finally { await browser.close(); }
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
