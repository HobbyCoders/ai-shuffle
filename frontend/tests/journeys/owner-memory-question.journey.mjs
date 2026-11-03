// Producing-path check: synthetic owner facts -> Remember -> session compiler ->
// real native provider request; an owner keep-both answer removes the next prompt's question.
// No live credentials/data, no claim about what a real model would answer.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, pickAgent, pickProject, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const FIXTURE = String.raw`
import asyncio, json, os, re, sys
from itertools import combinations
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert home.name == 'data' and home.parent.name.startswith('ai-shuffle-preview-')
os.environ['HOME'] = str(home)
from fluid.codemode.skills import SkillStore
from fluid.codemode.preferences import coexistence_key
from fluid.tools.builtins.remember import handler
from fluid.tools.registry import ToolCtx
store = SkillStore(home / 'fluid' / 'skills')
namespace = 'journey.owner_question.' + sys.argv[2] + '.'
ctx = ToolCtx(extra={'memory_store': store})
async def save(text, key):
    result = await handler({'text': text, 'key': key, 'user_requested': True,
                            'when': 'When applying the synthetic journey owner rules'}, ctx)
    assert not result.is_error, result.text
stage = sys.argv[3]
if stage == 'seed':
    for job in ('code', 'review'):
        for model in (('Opus', 'Sol', 'Sonnet', 'Haiku') if job == 'code' else ('Opus', 'Sol')):
            qualifier = 'Since last week ' if job == 'code' and model == 'Sol' else ''
            asyncio.run(save(f'Synthetic journey owner rule {sys.argv[2]}: {qualifier}{job} work goes to {model}.',
                             namespace + job + model))
    # Forget an unrelated historical answer. Identical prose must remain usable
    # for these new pair keys, without asking Remember to bypass deletion fences.
    historical_key = 'memory.coexist.journey.' + sys.argv[2]
    asyncio.run(save('Both stand.', historical_key))
    historical = next(e for e in store.memories() if e.key == historical_key)
    assert store.delete_memory(historical.id, expected_revision=historical.revision, explicit=True)
rows = [e for e in store._facts.snapshot()['entries'] if str(e.get('key') or '').startswith(namespace)]
assert len(rows) == 6, 'only the six synthetic facts may be touched'
from fluid.codemode.preferences import owner_conflict_lines
keys = re.findall(r"key='([^']+)'", '\n'.join(owner_conflict_lines(rows)))
pair_keys = [coexistence_key(a, b) for job in ('code', 'review')
             for a, b in combinations([e for e in rows if e['key'].startswith(namespace + job)], 2)]
if stage == 'resolve':
    assert len(keys) == 2, 'one owner answer per job, never six code questions'
    for key in keys:
        asyncio.run(save('Both stand.', key))
    actual = {e.get('key') for e in store._facts.snapshot()['entries']}
    assert set(pair_keys) <= actual, 'two Remember calls must commit all seven pair answers'
if stage == 'cleanup':
    for row in store._facts.snapshot()['entries']:
        if row.get('key') in pair_keys or str(row.get('key') or '').startswith(namespace):
            store.delete_memory(row['id'], expected_revision=row['revision'], explicit=True)
print(json.dumps({'resolution_keys': keys}))
`;

function fixture(status, token, stage) {
    const local = join(status.checkout, '.venv', 'bin', 'python');
    const python = existsSync(local) ? local : (process.env.PYTHON || 'python3');
    return JSON.parse(execFileSync(python, ['-c', FIXTURE, status.data_dir, token, stage], {
        cwd: status.checkout,
        env: { ...process.env, PYTHONPATH: join(status.checkout, 'fluid'), PYTHONDONTWRITEBYTECODE: '1' },
        encoding: 'utf8'
    }));
}

async function requestFor(status, token) {
    const rows = (await readFile(status.fake_ai.request_log, 'utf8')).split('\n')
        .filter(Boolean).map((line) => JSON.parse(line));
    const found = rows.filter((row) => row.method === 'POST' && row.body?.tools?.length
        && JSON.stringify(row.body).includes(token));
    expect(found.length > 0, `no actual model request contains ${token}`);
    return JSON.stringify(found.at(-1).body);
}

await runJourney({
    name: 'owner-memory-question',
    viewports: ['desktop'],
    throttles: ['fast'],
    allow: [...DEV_INSTANCE_NOISE],
    cases: [{
        name: 'ask-then-keep-both',
        async setup(ctx) {
            ctx.token = `owner-question-${uniq()}`;
            fixture(ctx.status, ctx.token.replaceAll('-', '_'), 'seed');
            await openNewChat(ctx, { project: PROJECT });
            await pickAccess(ctx.page, 'Bypass');
        },
        async act(ctx) {
            const { page, status, token, mark, log } = ctx;
            const fixtureToken = token.replaceAll('-', '_');
            try {
                await mark('first prompt carries owner question');
                await typeAndSend(page, `${token}-before`);
                await waitTurnSettled(page, { messagesAtLeast: 2 });
                const before = await requestFor(status, `${token}-before`);
                expect((before.match(/\[owner question; unresolved conflict\]/g) || []).length === 2,
                    'four code rules and two review rules must produce two job questions');
                expect(before.includes('review'), 'code questions must not crowd out the review job');
                expect(before.includes('and model-routing authority'), 'both facts must remain usable');
                expect(before.includes('do not silently choose a model for the contested job'), 'keep the ask rule');
                const { resolution_keys: keys } = fixture(status, fixtureToken, 'keys');
                for (const key of keys) expect(before.includes(key), 'the prompt must offer both executable answer keys');
                fixture(status, fixtureToken, 'resolve');
                await mark('fresh session after owner answer');
                // Reloading / restores the active chat. Use the real New session
                // action instead of calling the fresh-context setup helper twice.
                await page.getByRole('button', { name: 'New session', exact: true }).click();
                await waitChat(page, { surface: 'new' });
                const hero = page.getByRole('region', { name: 'New session' });
                await pickAgent(page, hero);
                await pickProject(page, hero, PROJECT);
                await pickAccess(page, 'Bypass');
                await mark('send in the new session');
                await typeAndSend(page, `${token}-after`);
                await waitTurnSettled(page, { messagesAtLeast: 2 });
                const after = await requestFor(status, `${token}-after`);
                expect(!after.includes('[owner question; unresolved conflict]'), 'a saved keep-both answer must stop the question');
                log('Actual native model requests: owner question before; no question after existing Remember records both stand. Synthetic facts only.');
            } finally {
                fixture(status, fixtureToken, 'cleanup');
            }
        }
    }]
});
