// Private preview validation: owner-only saves stay refused by preview auth.
// No provider work is triggered; populated history is covered by rendered tests.
import { runJourney } from './runner.mjs';
import { gotoApp } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
 name: 'automations',
 allow: DEV_INSTANCE_NOISE,
 cases: [{
  name: 'form-validation-and-back',
  async setup({ page, status }) {
   await gotoApp(page, status);
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
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Name is required' }).waitFor();
   await page.getByRole('textbox', { name: 'Name', exact: true }).fill('Preview form validation');
   await page.getByRole('button', { name: 'Create', exact: true }).click();
   await page.getByRole('alert').filter({ hasText: 'Either a prompt or at least one action step is required' }).waitFor();
   await page.getByRole('textbox', { name: 'Prompt', exact: true }).fill('No provider work is triggered.');
   await page.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('dev-agent');
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
 }]
});
