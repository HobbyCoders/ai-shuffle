// Producing-path check: synthetic owner rules -> Remember -> a stored conflict verdict ->
// session compiler -> real native provider request; an owner keep-both answer removes the
// next prompt's note. The verdict's answer text is scripted here (the shipped parser checks
// its quotes and stores it): no live credentials/data, no claim about what a real model
// would answer. scripts/measure_memory_questions.py scores a real model on the same prompts.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { PROJECT, expect, openNewChat, pickAccess, pickAgent, pickProject, showSidebar, typeAndSend, uniq, waitChat, waitTurnSettled } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const FIXTURE = String.raw`
import asyncio, json, os, sys
from pathlib import Path
home = Path(sys.argv[1]).resolve()
assert home.name == 'data' and home.parent.name.startswith('ai-shuffle-preview-')
os.environ['HOME'] = str(home)
from fluid.codemode import conflict_review as review
from fluid.codemode.skills import SkillStore
from fluid.tools.builtins.recall import handler as recall
from fluid.tools.builtins.remember import handler
from fluid.tools.registry import ToolCtx
store = SkillStore(home / 'fluid' / 'skills')
token, stage = sys.argv[2], sys.argv[3]
namespace = 'journey.owner_question.' + token + '.'
ctx = ToolCtx(extra={'memory_store': store, 'recall_store': store})
RULES = {'opus': 'code reviews are done by Opus.',
         'sol': 'from now on code reviews are done by Sol, never by Opus.',
         'findings': 'code review findings are fixed on the branch before a pull request.'}
async def save(text, key, when):
    result = await handler({'text': text, 'key': key, 'user_requested': True, 'when': when}, ctx)
    assert not result.is_error, result.text
def rows():
    found = {e['key'][len(namespace):]: e for e in store._facts.snapshot()['entries']
             if str(e.get('key') or '').startswith(namespace)}
    assert set(found) == set(RULES), 'only the three synthetic rules may be touched'
    return found
def waiting():
    mine = {e['id'] for e in rows().values()}
    return [c for c in review.open_conflicts(store._facts.snapshot()['entries'], store.root)
            if {c['a']['id'], c['b']['id']} & mine]
if stage == 'seed':
    for name, text in RULES.items():
        asyncio.run(save(f'Synthetic journey owner rule {token}: {text}', namespace + name,
                         f'When applying synthetic journey rule {token} {name}'))
    # The learner's review, with the model's answer scripted: the shipped parser
    # accepts the claim only because both passages are in their own rule.
    batch = review.plan(list(rows().values()), {})
    alias = {e['key'][len(namespace):]: a for a, e in batch.shown.items()}
    answer = json.dumps({'conflicts': [
        {'a': alias['opus'], 'b': alias['sol'], 'a_quote': 'code reviews are done by Opus',
         'b_quote': 'code reviews are done by Sol, never by Opus', 'why': 'Both name the reviewer.'},
        {'a': alias['findings'], 'b': alias['sol'], 'a_quote': 'code reviews are done by Opus',
         'b_quote': 'code reviews are done by Sol', 'why': 'A passage the rule does not contain.'}]})
    claims, rejected = review.parse(answer, batch)
    assert (len(claims), rejected) == (1, 1), 'a claim with a quote its rule lacks is dropped'
    review.record(store.root, store._facts.snapshot()['entries'], batch.subjects, claims)
found = rows()
open_now = waiting()
out = {'ids': {name: e['id'] for name, e in found.items()}, 'open': len(open_now)}
if stage == 'recall':
    assert len(open_now) == 1
    result = asyncio.run(recall({'ids': [found['opus']['id']]}, ctx))
    out.update(recall=result.text, key=open_now[0]['key'])
if stage == 'resolve':
    assert len(open_now) == 1
    asyncio.run(save('Both stand.', open_now[0]['key'], 'When applying these owner rules'))
    assert waiting() == [], 'the owner answer ends the question'
    out['open'] = 0
if stage == 'cleanup':
    keys = {c['key'] for c in open_now} | {review.coexistence_key(found['opus'], found['sol'])}
    for row in store._facts.snapshot()['entries']:
        if row.get('key') in keys or str(row.get('key') or '').startswith(namespace):
            store.delete_memory(row['id'], expected_revision=row['revision'], explicit=True)
print(json.dumps(out))
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

// One line of the # Memory index, from the JSON-encoded request body.
const indexLine = (body, id) => body.split('\\n').find((line) => line.includes(`- [${id} r`)) || '';

await runJourney({
    name: 'owner-memory-question',
    viewports: ['desktop', 'phone'],
    throttles: ['fast', 'slow'],
    allow: [...DEV_INSTANCE_NOISE],
    cases: [{
        name: 'ask-then-keep-both',
        async setup(ctx) {
            ctx.token = `owner-question-${uniq()}`;
            ctx.ids = fixture(ctx.status, ctx.token.replaceAll('-', '_'), 'seed').ids;
            await openNewChat(ctx, { project: PROJECT });
            await pickAccess(ctx.page, 'Bypass');
        },
        async act(ctx) {
            const { page, status, token, ids, mark, log } = ctx;
            const fixtureToken = token.replaceAll('-', '_');
            try {
                await mark('first prompt marks the two rules in conflict');
                await typeAndSend(page, `${token}-before`);
                await waitTurnSettled(page, { messagesAtLeast: 2 });
                const before = await requestFor(status, `${token}-before`);
                expect(indexLine(before, ids.opus).includes(`owner question: conflicts with [${ids.sol}]`),
                    'the older rule must point at the newer one');
                expect(indexLine(before, ids.sol).includes(`owner question: conflicts with [${ids.opus}]`),
                    'the newer rule must point at the older one');
                expect(indexLine(before, ids.findings).length > 0 && !indexLine(before, ids.findings).includes('owner question'),
                    'a rule that only shares words must carry no question');
                expect(!before.includes('unresolved conflict'), 'no question paragraph is added to the prompt');
                const asked = fixture(status, fixtureToken, 'recall');
                expect(asked.recall.includes('[owner question]') && asked.recall.includes(asked.key),
                    'Recall of a rule in conflict must print the question and its answer key');
                expect(asked.recall.includes('"code reviews are done by Opus"')
                    && asked.recall.includes('"code reviews are done by Sol, never by Opus"'),
                    'the question must quote both rules');
                expect(asked.recall.includes(`follow the newer rule [${ids.sol}]`), 'the newer rule is followed meanwhile');
                fixture(status, fixtureToken, 'resolve');
                await mark('fresh session after owner answer');
                // Reloading / restores the active chat. Use the real New session
                // action instead of calling the fresh-context setup helper twice.
                await showSidebar(page);
                await page.getByRole('button', { name: 'New chat', exact: true }).click();
                await waitChat(page, { surface: 'new' });
                const hero = page.getByRole('region', { name: 'New chat' });
                await pickAgent(page, hero);
                await pickProject(page, hero, PROJECT);
                await pickAccess(page, 'Bypass');
                await mark('send in the new session');
                await typeAndSend(page, `${token}-after`);
                await waitTurnSettled(page, { messagesAtLeast: 2 });
                const after = await requestFor(status, `${token}-after`);
                expect(indexLine(after, ids.opus).length > 0 && !indexLine(after, ids.opus).includes('owner question')
                    && !indexLine(after, ids.sol).includes('owner question'),
                    'a saved keep-both answer must stop the question');
                log('Actual native model requests: the two rules in conflict are marked on their index lines before; neither is after the keep-both answer. The verdict answer text was scripted; synthetic facts only.');
            } finally {
                fixture(status, fixtureToken, 'cleanup');
            }
        }
    }]
});
