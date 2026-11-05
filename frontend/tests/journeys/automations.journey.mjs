// Real automation authoring and responsive Back, with no provider work triggered.
import { runJourney } from './runner.mjs';
import { expect, gotoApp, uniq } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
 name: 'automations',
 allow: DEV_INSTANCE_NOISE,
 cases: [{
  name: 'create-search-edit-and-back',
  async setup({ page, status }) {
   await gotoApp(page, status);
   const drawer = page.getByRole('button', { name: 'Open menu', exact: true });
   if (await drawer.isVisible()) await drawer.click();
   const nav = page.getByRole('navigation', { name: 'Workspace', exact: true });
   await nav.getByRole('button', { name: 'More', exact: true }).click();
   await page.getByRole('menuitem', { name: 'Automations', exact: true }).click();
   await page.getByPlaceholder('Search automations').waitFor();
  },
  async act({ page, variant, mark }) {
   let id;
   const name = `Automation journey ${uniq()}`;
   try {
    await mark('open a new automation and validate the required fields');
    await page.getByRole('button', { name: 'New automation', exact: true }).first().click();
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    await page.getByRole('alert').filter({ hasText: 'Name is required' }).waitFor();
    await page.getByRole('textbox', { name: 'Name', exact: true }).fill(name);
    await page.getByRole('textbox', { name: 'Prompt', exact: true }).fill('No work is triggered by this journey.');
    await page.getByRole('combobox', { name: 'Agent', exact: true }).selectOption('dev-agent');
    // A webhook cannot fire accidentally on a timer during the journey.
    await page.getByRole('group', { name: 'Trigger type', exact: true }).getByRole('button', { name: 'Webhook', exact: true }).click();
    await mark('save through the real automation API');
    const saved = page.waitForResponse(r => r.url().endsWith('/api/v1/automations') && r.request().method() === 'POST');
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    const response = await saved;
    expect(response.ok(), `creation succeeds (${response.status()})`);
    id = (await response.json()).id;
    await page.getByRole('heading', { name, exact: true }).waitFor();
    await page.getByRole('tab', { name: /^Run history/ }).click();
    await page.getByText('No runs yet', { exact: true }).waitFor();
    await mark('return to the collection, search, and reopen');
    if (variant.viewport === 'phone') {
     await page.getByRole('button', { name: 'Back to Automations', exact: true }).click();
    }
    await page.getByPlaceholder('Search automations').fill(name);
    await page.locator(`[data-settings-item="${id}"]`).click();
    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    await page.getByRole('tab', { name: 'Budget & limits', exact: true }).click();
    await page.getByRole('textbox', { name: 'Max cost / run', exact: true }).fill('0.25');
    await page.getByRole('tab', { name: 'Media generation', exact: true }).click();
    await page.getByTestId('automation-generation-group').waitFor();
    await mark('Back exits the form without hidden collection controls');
    await page.getByRole('button', { name: 'Back to Automations', exact: true }).click();
    await page.getByPlaceholder('Search automations').waitFor();
    await page.getByPlaceholder('Search automations').fill('no such automation');
    await page.getByText('No matching automations', { exact: true }).waitFor();
   } finally {
    if (id) {
     const deleted = await page.request.delete(new URL(`/api/v1/automations/${id}`, page.url()).href);
     expect(deleted.ok(), 'the journey removes only its own automation');
    }
   }
  }
 }]
});
