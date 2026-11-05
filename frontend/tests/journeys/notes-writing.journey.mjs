// Real note creation, autosave, Preview and moved controls in both app shells.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';
const mobileShell = process.argv.includes('--mobile-shell');

async function openNotes(page, status, native = false) {
 await gotoApp(page, status);
 if (native) {
  await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
  expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
 }
 const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
 if (!await nav.isVisible()) await page.getByRole('button', { name: /^(Open menu|Show sidebar|Chats)( \(|$)/ }).first().click();
 const notes = nav.getByRole('button', { name: 'Notes', exact: true });
 if (await notes.count()) await notes.click();
 else {
  await nav.getByRole('button', { name: 'More', exact: true }).click();
  await page.getByRole('menuitem', { name: 'Notes', exact: true }).click();
 }
 await page.getByPlaceholder('Search notes', { exact: true }).waitFor();
}
function writingCase(name, native = false, mid = false) {
 return {
  name, ...(native ? { viewports: ['phone'] } : mid ? { viewports: ['desktop'] } : {}),
  async setup({ page, status }) {
   if (mid) await page.setViewportSize({ width: 1000, height: 900 });
   await openNotes(page, status, native);
  },
  async act({ page, mark }) {
   const title = `Writing journey ${uniq()} — a quieter workspace for every release`;
   await mark('create a note through the editable heading and bounded writing field');
   await page.getByRole('button', { name: 'New note', exact: true }).first().click();
   expect(await page.getByRole('tab', { name: 'Edit', exact: true }).getAttribute('aria-selected') === 'true', 'new notes start in Edit');
   const heading = page.getByRole('textbox', { name: 'Note title', exact: true });
   expect(await heading.evaluate(el => !!el.closest('h1')), 'the only title input is the heading');
   expect(await heading.evaluate(el => parseFloat(getComputedStyle(el).fontSize)) >= 22, 'the editable title retains heading hierarchy, including the native shell');
   expect(await heading.evaluate(el => el.tagName) === 'TEXTAREA', 'the note heading wraps in an auto-growing textarea');
   await heading.fill(title);
   expect(await heading.evaluate(el => el.scrollHeight <= el.clientHeight + 1), 'every title line is visible');
   await heading.press('Enter');
   expect(await page.getByRole('textbox', { name: 'Note body', exact: true }).evaluate(el => el === document.activeElement), 'Enter moves from title to body');
   expect(await heading.inputValue() === title, 'Enter never inserts a title newline');
   await page.getByRole('textbox', { name: 'Note body', exact: true }).fill('# Release plan\n- [x] **Ready** for [review](https://example.com)');
   await page.locator('.note-save').filter({ hasText: /^Saved$/ }).waitFor({ state: 'attached' });
   await mark('switch to Preview and keep the phone body above 320px');
   await page.getByRole('tab', { name: 'Preview', exact: true }).click();
   const preview = page.locator('.note-preview');
   const task = preview.locator('li').first();
   expect(await task.locator('input[type=checkbox]:checked:disabled').count() === 1, 'Preview reuses read-only GFM task marks');
   expect(await task.evaluate(el => getComputedStyle(el).listStyleType) === 'none', 'task items have no bullet');
   expect(await preview.locator('ul').evaluate(el => parseFloat(getComputedStyle(el).paddingInlineStart)) >= 20, 'lists stay inside the preview column');
   expect(await task.locator('input').evaluate(el => getComputedStyle(el).appearance) === 'none', 'task marks use the theme outline and accent, not disabled browser grey');
   if (page.viewportSize().width < 600) {
    expect(await preview.evaluate(el => getComputedStyle(el).paddingTop) === '24px', 'phone Preview has a 24px document inset');
    const titleBox = await heading.boundingBox();
    const tabsBox = await page.getByRole('tablist').boundingBox();
    expect(tabsBox.y - (titleBox.y + titleBox.height) >= 16 && tabsBox.y - (titleBox.y + titleBox.height) <= 24, 'phone title-to-tabs spacing is 16–24px');
   }
   const body = page.getByRole('region', { name: 'Note content', exact: true });
   if (page.viewportSize().width < 600) expect((await body.boundingBox()).y < 320, 'the note body starts within 320px');
   const actions = page.locator('details.detail-more');
   if (await actions.isVisible()) await actions.locator('summary').click();
   expect(await page.getByRole('button', { name: 'Attach file', exact: true }).isVisible(), 'attach stays reachable through the phone title-row More menu');
   if (await actions.isVisible()) await actions.locator('summary').click();
   const summary = page.locator('details.note-fields > summary');
   if (!await page.getByRole('textbox', { name: 'Add tag', exact: true }).isVisible()) await summary.click();
   await page.getByRole('textbox', { name: 'Add tag', exact: true }).fill('Planning');
   await page.getByRole('textbox', { name: 'Add tag', exact: true }).press('Enter');
   await page.locator('.note-save').filter({ hasText: /^Saved$/ }).waitFor({ state: 'attached' });
   if (page.viewportSize().width < 600) await summary.click();
   await mark('More retains its keyboard path and the existing delete confirmation');
   if (await actions.isVisible()) await actions.locator('summary').click();
   await page.getByRole('button', { name: 'More note actions', exact: true }).click();
   await page.getByRole('menuitem', { name: 'Delete note', exact: true }).press('Escape');
   expect(await page.getByRole('button', { name: 'More note actions', exact: true }).evaluate(el => el === document.activeElement), 'Escape returns focus');
   await page.getByRole('button', { name: 'More note actions', exact: true }).click();
   await page.getByRole('menuitem', { name: 'Delete note', exact: true }).click();
   await page.getByRole('dialog').getByRole('button', { name: 'Cancel', exact: true }).last().click();
   await mark('close via phone Back or another note, then reopen the persisted content');
   const back = page.getByRole('button', { name: 'Back to Notes', exact: true });
   if (await back.isVisible()) await back.click();
   else await page.getByRole('button', { name: 'New note', exact: true }).first().click();
   const row = page.locator('[data-settings-item]').filter({ hasText: title });
   expect(!(await row.innerText()).match(/# Release|\[x\]|\*\*|https:/), 'the list excerpt is plain text');
   await row.click();
   expect(await page.getByRole('tab', { name: 'Preview', exact: true }).getAttribute('aria-selected') === 'true', 'persisted content reopens in Preview');
   expect(await heading.inputValue() === title, 'the title was persisted');
  }
 };
}
await runJourney({ name: 'notes-writing', allow: DEV_INSTANCE_NOISE, cases: [writingCase('responsive-note-writing'), writingCase('mid-note-writing', false, true), ...(mobileShell ? [writingCase('native-note-writing', true)] : [])] });
