// Private preview validation: owner-only saves stay refused by preview auth.
// No provider work is triggered; populated history is covered by rendered tests.
import { runJourney } from './runner.mjs';
import { expect, gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const mobileShell = process.argv.includes('--mobile-shell');
function formCase(native = false) {
 return {
  name: native ? 'native-form-validation-and-back' : 'form-validation-and-back',
  ...(native ? { viewports: ['phone'] } : {}),
  async setup({ page, status }) {
   if (native) {
    await page.goto(new URL('/mobile/', status.urls.preview_listener).href);
    expect(new URL(page.url()).pathname === '/mobile/', 'native evidence uses the real phone bundle');
   } else await gotoApp(page, status);
   const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
   if (!await nav.isVisible()) {
    await page.getByRole('button', { name: /^(Open menu|Show sidebar|Chats)( \(|$)/ }).first().click();
   }
   await nav.getByRole('button', { name: 'More', exact: true }).click();
   await page.getByRole('menuitem', { name: 'Automations', exact: true }).click();
   await page.getByPlaceholder('Search automations').waitFor();
  },
  async act({ page, mark }) {
   await mark('open a new automation and validate the required fields');
   const phone = page.viewportSize().width < 600;
   const start = page.getByRole('button', { name: 'New automation', exact: true }).first();
   const startBox = await start.boundingBox();
   await start.click();
   if (phone) {
    await page.touchscreen.tap(startBox.x + startBox.width / 2, startBox.y + startBox.height / 2);
    await page.waitForTimeout(450);
    expect(await page.evaluate(() => !document.activeElement?.matches('input, textarea, select')), 'the second tap of a double tap does not land in a form field');
    // From the list header that same spot is the form's More button, which is harmless.
    if (await page.locator('details.detail-more[open]').count()) await page.locator('details.detail-more summary').click();
   }
   const collapse = page.getByRole('button', { name: 'Collapse trigger', exact: true });
   expect((await collapse.innerText()).trim() === '', 'trigger collapse is an icon-only control');
   await collapse.click();
   await page.getByRole('button', { name: 'Expand trigger', exact: true }).click();
   const trigger = page.getByRole('region', { name: 'Trigger 1', exact: true });
   expect(await trigger.getByRole('heading', { name: 'Every day at 9 AM', exact: true }).count() === 1, 'the trigger heading is its useful schedule, not Cron');
   expect(await trigger.locator('.cron-summary-text').count() === 0, 'preset descriptions are not repeated as helpers');
   expect((await trigger.innerText()).match(/UTC/g)?.length === 1, 'the trigger says UTC once');
   expect(await trigger.locator(':scope > .st-row + .st-row').count() > 0, 'trigger controls use shared card row dividers');
   const row = trigger.locator('.st-row').filter({ hasText: 'Trigger type' });
   expect(await row.getByRole('group', { name: 'Trigger type' }).count() === 1, 'trigger type is inside the shared row, not an expansion');
   const toggleSize = await collapse.boundingBox();
   expect(toggleSize.width === (page.viewportSize().width < 600 ? 44 : 28), 'chevron keeps desktop density and a phone touch target');
   if (page.viewportSize().width < 600) {
    const schedule = trigger.getByRole('group', { name: 'Timing', exact: true });
    const scheduleRow = schedule.locator('xpath=ancestor::div[contains(@class,"st-row")]');
    expect(Math.abs((await schedule.boundingBox()).width - (await scheduleRow.locator('.st-rc').boundingBox()).width) < 1, 'Timing spans the stacked phone control row');
   }
   expect(await trigger.getByRole('textbox', { name: 'Cron expression', exact: true }).count() === 0, 'a preset hides its raw expression behind Advanced');
   await trigger.getByRole('button', { name: 'Show expression', exact: true }).click();
   await page.getByRole('textbox', { name: 'Cron expression', exact: true }).fill('*/15 * * * *');
   await trigger.getByRole('heading', { name: 'Every 15 minutes', exact: true }).waitFor();
   if (phone) {
    // Drawn at 32px, but reachable across the full 44px.
    for (const name of ['Add trigger', 'Add action step', 'Show expression', 'Hide expression']) {
     const button = page.getByRole('button', { name, exact: true }).first();
     if (!await button.count()) continue;
     const misses = await button.evaluate(async el => {
      el.scrollIntoView({ block: 'center', behavior: 'instant' });
      await new Promise(requestAnimationFrame);
      const box = el.getBoundingClientRect();
      return [box.top - 5.5, box.bottom + 5.5].map(y => document.elementFromPoint(box.left + box.width / 2, y))
       .filter(hit => !el.contains(hit)).map(hit => hit ? `${hit.tagName}.${hit.className}` : 'nothing');
     });
     expect(!misses.length, `${name} reaches a full finger's height (missed: ${misses})`);
    }
    const agent = page.getByRole('combobox', { name: 'Agent', exact: true });
    const agentRow = agent.locator('xpath=ancestor::div[contains(@class,"st-row")][1]');
    expect(Math.abs((await agent.boundingBox()).width - (await agentRow.locator('.st-rc').boundingBox()).width) < 1, 'the Agent select fills its phone row');
    const tops = await page.getByRole('group', { name: 'Trigger type', exact: true }).getByRole('button').evaluateAll(all => all.map(el => Math.round(el.getBoundingClientRect().top)));
    expect(Object.values(Object.groupBy(tops, top => top)).every(row => row.length > 1), `no trigger type sits alone on a row (${tops})`);
   }
   const actions = page.locator('details.detail-more');
   if (await actions.isVisible()) await actions.locator('summary').click();
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Name is required' }).waitFor();
   const nameField = page.getByRole('textbox', { name: 'Name', exact: true });
   const border = locator => locator.evaluate(el => getComputedStyle(el).borderTopColor);
   expect(await nameField.getAttribute('aria-invalid') === 'true', 'the refused Name field says so');
   expect(await border(nameField) !== await border(page.getByRole('textbox', { name: 'Prompt', exact: true })), 'the refused Name field looks refused');
   await nameField.fill('Preview form validation');
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Either a prompt or at least one action step is required' }).waitFor();
   await page.getByRole('textbox', { name: 'Prompt', exact: true }).fill('No provider work is triggered.');
   await page.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('dev-agent');
   if (await actions.isVisible()) await actions.locator('summary').click();
   await mark('switch trigger configurations using the shared controls');
   const types = page.getByRole('group', { name: 'Trigger type', exact: true });
   for (const label of ['Interval', 'Event', 'Connector', 'Webhook', 'Schedule']) {
    await types.getByRole('button', { name: label, exact: true }).click();
   }
   await mark('visit limits and generation tabs and preserve the draft');
   await page.getByRole('tab', { name: 'Budget & limits', exact: true }).click();
   await page.getByRole('textbox', { name: 'Max cost / run', exact: true }).fill('0.25');
   await page.getByRole('tab', { name: 'Media generation', exact: true }).click();
   await page.getByTestId('automation-generation-group').waitFor();
   await page.getByRole('tab', { name: 'Setup', exact: true }).click();
   await page.getByRole('textbox', { name: 'Name', exact: true }).waitFor();
   await mark('the collection remains searchable; phone Back restores its pane');
   const back = page.getByRole('button', { name: 'Back to Automations', exact: true });
   if (await back.isVisible()) await back.click();
   await page.getByPlaceholder('Search automations').waitFor();
   await page.getByPlaceholder('Search automations').fill('no such automation');
   await page.getByText('No matching automations', { exact: true }).waitFor();
  }
 };
}
await runJourney({ name: 'automations', allow: DEV_INSTANCE_NOISE, cases: [formCase(), ...(mobileShell ? [formCase(true)] : [])] });
