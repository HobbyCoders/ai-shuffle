// Model tuning writes the same per-model speed preference that new chats read.
// An OpenAI-shaped provider points only at the devloop's fake server. No chats
// are sent, and neither settings nor capabilities are stubbed in the browser.
import { runJourney } from './runner.mjs';
import { api, expect, gotoApp, showSidebar, waitChat } from './lib/app.mjs';

const model = 'gpt-6.1-sol';
const otherModel = 'gpt-6-astra';
const fastOnly = 'gpt-5.4';
const providerName = 'Speed tiers (journey)';
const button = (page, name) => page.getByRole('button', { name, exact: true }).filter({ visible: true });
const item = (page, id) => page.locator(`[data-settings-item="model:${id}"]`).filter({ visible: true });
const speed = (page) => page.getByRole('combobox', { name: 'Speed', exact: true }).filter({ visible: true });
const keys = ['modelFast', 'modelSpeed'].map((part) => `composer.${part}.${model}`);

// Make each write observable on the worker, not merely in this tab's cache.
async function save(page, changed, action, values) {
  const responses = changed.map((i) => page.waitForResponse((res) =>
    res.request().method() === 'PUT' && new URL(res.url()).pathname === `/api/v1/preferences/${keys[i]}`,
    { timeout: 5_000 }
  ));
  const [, ...writes] = await Promise.all([action(), ...responses]);
  expect(writes.every((res) => res.ok()), 'the worker accepted the speed preference');
  await checkSaved(page, values);
}

async function checkSaved(page, values) {
  const { preferences } = await api(page, 'GET', '/preferences/all');
  for (let i = 0; i < keys.length; i++) expect((preferences[keys[i]] ?? null) === values[i], `stored ${keys[i]} = ${values[i]}`);
}

async function modelList(page) {
  const back = button(page, 'Back to Model tuning');
  if (await back.isVisible()) await back.click();
  await item(page, model).waitFor();
}

async function newChatSpeed(page, tier) {
  // The global New chat shortcut also works while narrow Settings owns the screen.
  await page.keyboard.press('Control+n');
  await waitChat(page, { surface: 'new' });
  await page.getByRole('button', { name: /^Model: / }).filter({ visible: true }).click();
  const picker = page.getByRole('dialog', { name: 'Choose model' });
  await picker.getByRole('option', { name: /^GPT-6\.1 Sol/ }).click();
  await page.getByRole('button', { name: /^(Effort: |All chat controls)/ }).filter({ visible: true }).click({ timeout: 8_000 });
  // Desktop: the effort card's corner bolt names the tier. Phone: the sheet's Speed segments.
  const bolt = page.getByRole('button', { name: `Speed: ${tier}`, exact: true }).filter({ visible: true });
  const segment = page.getByRole('radiogroup', { name: 'Speed', exact: true }).getByRole('radio', { name: tier, exact: true, checked: true });
  await bolt.or(segment).first().waitFor();
  await page.keyboard.press('Escape');
}

async function openTuning(page) {
  await showSidebar(page);
  await page.getByRole('navigation', { name: 'Workspace', exact: true }).getByRole('button', { name: 'Settings', exact: true }).click();
  const category = button(page.getByRole('navigation', { name: 'Settings', exact: true }), 'Model tuning').first();
  const tuning = item(page, model).or(button(page, 'Back to Model tuning'));
  // Narrow Settings can reopen directly on the previous page or detail.
  await category.or(tuning).first().waitFor({ timeout: 8_000 });
  if (!(await tuning.count())) await category.click();
}

runJourney({ name: 'model-tuning-speed', cases: [{
  name: 'new-chat-defaults',
  async setup({ page, status }) {
    // Preview grants are read-only for account prefs. Use the isolated
    // devloop's loopback owner listener and stamped desktop build.
    const port = status.app.local_port;
    expect(Number.isInteger(port) && port > 1024 && port !== 8000 && port !== 8001, 'requires a private devloop owner port');
    const local = { ...status, urls: { ui: `http://localhost:${port}/` } };
    await gotoApp(page, local);
    const { providers: rows } = await api(page, 'GET', '/model-providers');
    const models = [
      { id: model, label: 'GPT-6.1 Sol' }, { id: otherModel, label: 'GPT-6 Astra' }, { id: fastOnly, label: 'GPT-5.4' }
    ];
    const row = rows.find((row) => row.name === providerName && row.base_url === status.fake_ai.url);
    if (!row) {
      await api(page, 'POST', '/model-providers', {
        name: providerName, provider_type: 'openai', base_url: status.fake_ai.url,
        api_dialect: 'openai', auth_token: 'dev-fake-key-not-secret',
        models, default_model: model
      });
    } else if (!row.models.some((entry) => entry.id === fastOnly)) {
      await api(page, 'PATCH', `/model-providers/${row.id}`, { models });
    }
    // A retry after a failed case starts with the same unset records.
    const { preferences } = await api(page, 'GET', '/preferences/all');
    for (const key of keys) if (preferences[key] != null) await api(page, 'PUT', `/preferences/${key}`, { key, value: null });
    await gotoApp(page, local);
    await openTuning(page);
    await item(page, model).click();
  },
  async act({ page, mark }) {
    await mark('choose Ultrafast for new chats');
    expect(await speed(page).getByRole('option', { name: 'Ultrafast', exact: true }).count() === 1,
      'Model tuning offers Ultrafast for an Ultrafast-capable model');
    expect(await speed(page).inputValue() === '', 'no override by default');
    await save(page, [0, 1], () => speed(page).selectOption('ultrafast'), [true, 'ultrafast']);
    const bounds = await speed(page).boundingBox();
    expect(bounds.x >= 0 && bounds.x + bounds.width <= page.viewportSize().width, 'tier selector fits the viewport');

    await mark('check list summary, isolation and an unsupported model');
    await modelList(page);
    await item(page, model).getByText(/Ultrafast/).waitFor();
    await item(page, otherModel).click();
    expect(await speed(page).inputValue() === '', 'the other model has no speed override');
    await modelList(page);
    await item(page, fastOnly).click();
    await page.getByRole('switch', { name: 'Fast mode', exact: true }).waitFor();
    expect(await speed(page).count() === 0, 'a model without Ultrafast keeps the Fast switch');
    await modelList(page);
    await item(page, model).click();
    expect(await speed(page).inputValue() === 'ultrafast', 'reopening the model shows Ultrafast');
  },
  async verify({ page, log }) {
    // A reload replaces the recorded document; check the persisted selection in
    // the new document rather than treating that transition as a layout shift.
    await page.reload();
    await speed(page).waitFor();
    expect(await speed(page).inputValue() === 'ultrafast', 'reload restores Ultrafast');
    await newChatSpeed(page, 'Ultrafast');
    for (const [tier, changed, values] of [['Fast', [0, 1], [true, null]], ['Standard', [0], [false, null]]]) {
      await openTuning(page);
      await modelList(page);
      await item(page, model).click();
      await save(page, changed, () => speed(page).selectOption(tier.toLowerCase()), values);
      if (tier === 'Standard') {
        await modelList(page);
        await item(page, model).getByText(/\bStandard\b/).waitFor();
      }
      await newChatSpeed(page, tier);
    }
    await openTuning(page);
    await modelList(page);
    await item(page, model).click();
    await save(page, [0, 1], () => speed(page).selectOption('ultrafast'), [true, 'ultrafast']);
    // The shared row reveals its reset on hover or keyboard focus.
    await speed(page).focus();
    await save(page, [0, 1], () => button(page, 'Reset Speed to default').click(), [null, null]);
    expect(await speed(page).inputValue() === '', 'reset restores Not picked');
    log('Worker prefs accepted and reloaded; new chats selected Ultrafast, Fast and Standard; reset cleared both records.');
  }
}] });
