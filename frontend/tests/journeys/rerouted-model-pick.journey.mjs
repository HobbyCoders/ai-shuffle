// A second pick of the original model is still a new choice (#1635).
import { readFile } from 'node:fs/promises';
import { runJourney } from './runner.mjs';
import { expect, gotoApp, openFromSidebar, waitChat } from './lib/app.mjs';

async function pick(page, label) {
    await page.getByRole('button', { name: /^Model: / }).click();
    const dialog = page.getByRole('dialog', { name: 'Choose model' });
    await dialog.getByText(label, { exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
}
async function send(page, prompt) {
    await page.getByRole('textbox', { name: 'Message', exact: true }).fill(prompt);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
}
let marker;
await runJourney({
    name: 'rerouted-model-pick', viewports: ['desktop'], throttles: ['fast'],
    cases: [{ name: 'pick-back',
        async setup({ page, status }) {
            await gotoApp(page, status);
            await openFromSidebar(page, 'Quick question (seeded)');
            await pick(page, 'Fake Ai');
        },
        async act({ page }) {
            await send(page, '[script:guardrail-reroute] Report the model');
            await page.getByRole('button', { name: /^Model: Fake Ai Safe$/i }).waitFor();
            await pick(page, 'Fake Ai Safe');
            await pick(page, 'Fake Ai');
            await page.getByRole('button', { name: /^Model: Fake Ai$/i }).waitFor();
            marker = `next explicit pick ${Date.now()}`;
            await send(page, marker);
        },
        async verify({ page, status }) {
            await page.getByText(marker, { exact: true }).last().waitFor();
            await waitChat(page, { streaming: 'false' });
            const rows = (await readFile(status.fake_ai.request_log, 'utf8')).trim().split('\n').map(s => JSON.parse(s));
            expect(rows.some(r => String(r.prompt).includes(marker) && r.model === 'fake-ai'), 'the provider received the next explicit choice');
        }
    }]
});
