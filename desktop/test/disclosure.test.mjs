import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { test } from 'node:test';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { AGENT_ACCESS, AGENT_ACCESS_VERSION, DISCLOSURE_ACCEPTED_ENV, DisclosureStore, agentAccessDetail, ensureAgentAccessAcknowledged } = require('../dist/main/disclosure.js');

async function profile(t) {
	const dir = await mkdtemp(path.join(tmpdir(), 'shuffle-disclosure-'));
	t.after(() => rm(dir, { recursive: true, force: true }));
	return dir;
}

test('the disclosure says agents run as the user, with their own login and full file access', () => {
	const text = [AGENT_ACCESS.message, agentAccessDetail()].join('\n');
	assert.match(text, /run as you/);
	assert.match(text, /your own `claude` login/);
	assert.match(text, /any file your user account can/);
	assert.match(text, /Help > Agent Access/);
});

test('first launch asks once; quitting asks again next time', async (t) => {
	const dir = await profile(t);
	const store = DisclosureStore.forProfile(dir, {});
	let asked = 0;
	assert.equal(await ensureAgentAccessAcknowledged(store, async () => { asked++; return 'quit'; }), false);
	assert.equal(await store.acknowledged(), false);
	assert.equal(await ensureAgentAccessAcknowledged(store, async () => { asked++; return 'continue'; }), true);
	assert.equal(await ensureAgentAccessAcknowledged(store, async () => { asked++; return 'quit'; }), true);
	assert.equal(asked, 2);
	const saved = JSON.parse(await readFile(path.join(dir, 'agent-access.json'), 'utf8'));
	assert.equal(saved.version, AGENT_ACCESS_VERSION);
});

test('an older or unreadable acknowledgement asks again; acceptance runs can skip it', async (t) => {
	const dir = await profile(t);
	const file = path.join(dir, 'agent-access.json');
	await writeFile(file, JSON.stringify({ version: AGENT_ACCESS_VERSION - 1 }));
	assert.equal(await DisclosureStore.forProfile(dir, {}).acknowledged(), false);
	await writeFile(file, 'not json');
	assert.equal(await DisclosureStore.forProfile(dir, {}).acknowledged(), false);
	assert.equal(await DisclosureStore.forProfile(dir, { [DISCLOSURE_ACCEPTED_ENV]: '1' }).acknowledged(), true);
	assert.equal(await DisclosureStore.forProfile(dir, { [DISCLOSURE_ACCEPTED_ENV]: 'true' }).acknowledged(), false);
});
