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
   await page.getByRole('button', { name: 'New automation', exact: true }).first().click();
   const collapse = page.getByRole('button', { name: 'Collapse trigger', exact: true });
   expect((await collapse.innerText()).trim() === '', 'trigger collapse is an icon-only control');
   await collapse.click();
   await page.getByRole('button', { name: 'Expand trigger', exact: true }).click();
   const trigger = page.getByRole('region', { name: 'Trigger 1', exact: true });
   const row = trigger.locator('.st-row').filter({ hasText: 'Trigger type' });
   expect(await row.getByRole('group', { name: 'Trigger type' }).count() === 1, 'trigger type is inside the shared row, not an expansion');
   const toggleSize = await collapse.boundingBox();
   expect(toggleSize.width === (page.viewportSize().width < 600 ? 44 : 28), 'chevron keeps desktop density and a phone touch target');
   if (page.viewportSize().width < 600) {
    const schedule = trigger.getByRole('group', { name: 'Schedule', exact: true });
    const scheduleRow = schedule.locator('xpath=ancestor::div[contains(@class,"st-row")]');
    expect(Math.abs((await schedule.boundingBox()).width - (await scheduleRow.locator('.st-rc').boundingBox()).width) < 1, 'Schedule spans the stacked phone control row');
   }
   await page.getByRole('textbox', { name: 'Cron expression', exact: true }).fill('*/15 * * * *');
   await page.getByText('Every 15 minutes', { exact: true }).last().waitFor();
   const actions = page.locator('details.detail-more');
   if (await actions.isVisible()) await actions.locator('summary').click();
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Name is required' }).waitFor();
   await page.getByRole('textbox', { name: 'Name', exact: true }).fill('Preview form validation');
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Either a prompt or at least one action step is required' }).waitFor();
   await page.getByRole('textbox', { name: 'Prompt', exact: true }).fill('No provider work is triggered.');
   await page.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('dev-agent');
   if (await actions.isVisible()) await actions.locator('summary').click();
   await mark('switch trigger configurations using the shared controls');
   const types = page.getByRole('group', { name: 'Trigger type', exact: true });
   for (const label of ['Interval', 'Event', 'Connector', 'Webhook', 'Cron']) {
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
