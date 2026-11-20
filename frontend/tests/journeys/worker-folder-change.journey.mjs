// Real browser and API; opt into a real paired worker with PROJECT_FOLDER_PEER=1.
import { randomUUID } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { api, chatIdByToken, expect, gotoApp, settleServiceWorker, showSidebar, typeAndSend, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const peer = process.env.PROJECT_FOLDER_PEER === '1';
const fixtures = new WeakMap();
const workers = (page, id) => api(page, 'GET', `/projects/workers?project_v2_id=${encodeURIComponent(id)}`);

await runJourney({
	name: 'worker-folder-change',
	allow: DEV_INSTANCE_NOISE,
	cases: [{
		name: peer ? 'paired-worker' : 'local-worker',
		async setup({ page, status, variant }) {
			const target = peer ? status.workers?.[1] : status;
			expect(target, 'paired case needs a --workers 2 devloop');
			const id = `folder-${randomUUID().slice(0, 8)}`;
			const name = `Folder edit ${id.slice(-8)}`;
			const old = join(status.workspace_dir, id);
			const nextName = `${id}-destination`;
			const next = join(target.workspace_dir, nextName);
			mkdirSync(old);
			mkdirSync(next);
			writeFileSync(join(old, 'original.txt'), 'leave this file here');
			writeFileSync(join(next, 'destination.txt'), 'do not overwrite this file');
			await gotoApp(page, status);
			await settleServiceWorker(page, status);
			await api(page, 'POST', '/projects', { id, name, path: old });
			const detail = await api(page, 'GET', `/projects/workers?project_id=${id}`);
			const identity = detail.project_v2_id;
			const local = detail.workers.find(w => w.is_self);
			let worker = local;
			if (peer) {
				worker = detail.workers.find(w => !w.is_self && w.online);
				expect(worker, 'the paired worker must be online');
				await api(page, 'POST', '/projects/setup', { project_v2_id: identity, install_id: worker.install_id, folder: `${id}-peer` });
				const ready = await page.waitForFunction(async ({ identity, workerId }) => {
					const response = await fetch(`/api/v1/projects/workers?project_v2_id=${identity}`);
					return response.ok && (await response.json()).workers.find(w => w.install_id === workerId && w.state === 'ready' && w.folder && w.folder_revision);
				}, { identity, workerId: worker.install_id }, { polling: 500, timeout: 25_000 });
				worker = await ready.jsonValue();
				writeFileSync(join(worker.folder, 'peer-original.txt'), 'keep the peer file');
				expect(!existsSync(join(status.workspace_dir, nextName)), 'relative destination exists on B, not on A');
			}
			fixtures.set(page, { id, identity, name, local, worker, old, next, nextName });
			if (variant.viewport !== 'desktop') await showSidebar(page);
			await page.getByRole('navigation', { name: 'Workspace' }).getByRole('button', { name: 'Projects', exact: true }).click();
			await page.getByRole('button', { name: `Open ${name}`, exact: true }).click();
			await page.getByRole('tab', { name: /^Workers/ }).click();
			await page.locator(`[id="pw-card-${worker.install_id}"]`).getByRole('button', { name: 'Change folder' }).waitFor();
		},
		async act({ page, mark }) {
			const { worker, next, nextName, identity, local } = fixtures.get(page);
			const card = page.locator(`[id="pw-card-${worker.install_id}"]`);
			await mark(`open the editor on ${worker.device_name}`);
			await card.getByRole('button', { name: 'Change folder' }).click();
			await card.getByText(/No files are moved or copied/).waitFor();
			const field = card.getByRole('textbox', { name: /^New folder on / });
			expect((await field.inputValue()) === worker.folder, 'editor starts with current folder');
			await card.getByRole('button', { name: 'Browse' }).click();
			await card.locator('.directory-browser .tree-row').first().waitFor();
			await card.getByRole('button', { name: 'Browse' }).click();
			await field.fill(nextName);
			await mark('save the existing folder relative to the chosen worker');
			await card.getByRole('button', { name: 'Save folder' }).click();
			await card.getByText(next, { exact: true }).waitFor();
			const state = await workers(page, identity);
			expect(state.workers.find(w => w.install_id === worker.install_id).folder === next, 'the saved path persists in a fresh read');
			if (peer) expect(state.workers.find(w => w.is_self).folder === local.folder, 'caller project is unchanged');
		},
		async verify({ page, log }) {
			const { id, identity, worker, old, next } = fixtures.get(page);
			expect(readFileSync(join(old, 'original.txt'), 'utf8') === 'leave this file here', 'original files were untouched');
			expect(readFileSync(join(next, 'destination.txt'), 'utf8') === 'do not overwrite this file', 'destination files were untouched');
			expect(!existsSync(join(next, 'original.txt')), 'the old file was not copied');
			if (peer) expect(readFileSync(join(worker.folder, 'peer-original.txt'), 'utf8') === 'keep the peer file', 'peer source file stayed');
			await page.locator(`[id="pw-card-${worker.install_id}"]`).getByRole('button', { name: 'New chat here' }).click();
			const text = `Check ${id}`;
			await typeAndSend(page, text);
			await waitTurnSettled(page);
			const chatId = await chatIdByToken(page, id);
			const session = await api(page, 'GET', `/sessions/${chatId}`);
			expect(session.project_v2_id === identity, 'new chat binds the same logical project');
			const files = await api(page, 'GET', `/projects/${id}/files?session_id=${encodeURIComponent(chatId)}`);
			expect(files.files.some(file => file.name === 'destination.txt'), 'new session resolves files from the retargeted folder');
			log(`${peer ? 'Peer' : 'Local'} chat ${chatId} resolved the new folder after a real fake-provider turn.`);
		}
	}]
});
