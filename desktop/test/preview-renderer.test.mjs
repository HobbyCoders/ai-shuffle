import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readPreviewRenderer } from '../scripts/preview-renderer.mjs';

function fixture(evaluate) {
  const waits = [];
  return { waits, page: { isClosed: () => false, evaluate,
    async waitForFunction(predicate, arg, options) {
      assert.match(predicate.toString(), /shuffleDesktop/);
      assert.equal(arg, undefined);
      waits.push(options.timeout);
    } } };
}

test('renderer state read waits for preload and retries a destroyed startup context', async () => {
  let calls = 0;
  const { page, waits } = fixture(async read => {
    if (++calls === 1) throw new Error('page.evaluate: Execution context was destroyed, most likely because of a navigation');
    return read();
  });
  assert.deepEqual(await readPreviewRenderer(page, () => ({ phase: 'unsupported' })), { phase: 'unsupported' });
  assert.equal(calls, 2);
  assert.equal(waits.length, 2);
  assert(waits[1] < waits[0], 'Retries must share the original deadline');
});

for (const message of ['IPC channel unavailable', 'Target page, context or browser has been closed', 'TypeError: broken preload']) {
  test(`renderer probe propagates ${message} without retry`, async () => {
    let calls = 0;
    const { page } = fixture(async () => { calls++; throw new Error(message); });
    await assert.rejects(readPreviewRenderer(page, () => null), { message });
    assert.equal(calls, 1);
  });
}

test('closed page is terminal even when the last context was destroyed', async () => {
  const { page, waits } = fixture(async () => { throw new Error('Execution context was destroyed'); });
  page.isClosed = () => true;
  await assert.rejects(readPreviewRenderer(page, () => null), /context was destroyed/);
  assert.equal(waits.length, 1);
});

test('navigation retries have a bounded deadline', async () => {
  const { page } = fixture(async () => { throw new Error('Execution context was destroyed'); });
  await assert.rejects(readPreviewRenderer(page, () => null, { timeout: 10 }), /deadline|context was destroyed/);
});

test('missing preload fails through the readiness timeout without evaluating', async () => {
  const { page } = fixture(async () => assert.fail('must not read an unready renderer'));
  page.waitForFunction = async () => { throw new Error('Timeout waiting for preload'); };
  await assert.rejects(readPreviewRenderer(page, () => null), /Timeout waiting for preload/);
});
