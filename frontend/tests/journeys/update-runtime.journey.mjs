// Connected-worker update details. Controlled responses below test rendering,
// not a real updater transition; the first case reads the real status producer.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { runJourney } from './runner.mjs';
import { expect, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

const UPDATE_PATH = '/api/v1/settings/application-updates';
const isResponse = (path, method) => (response) => new URL(response.url()).pathname === path && response.request().method() === method;

// A native Unknown response must never stand in for a controlled fixture.
export async function assertFixtureResponse(ctx, key, response, expected) {
  expect(ctx.fixtureHits[key] > 0, `fixture ${key} was not intercepted`);
  expect(response.ok(), `fixture ${key} response failed`);
  expect(!response.fromServiceWorker(), `fixture ${key} was served by a service worker`);
  expect(isDeepStrictEqual(await response.json(), expected), `fixture ${key} readback did not match`);
  ctx.log?.(`fixture ${key} hits=${ctx.fixtureHits[key]} readback=matched`);
}

async function assertNoServiceWorker(ctx) {
  const state = await ctx.page.evaluate(() => ({ secure: isSecureContext, controller: navigator.serviceWorker?.controller?.scriptURL ?? null }));
  expect(state.controller === null, 'HTTP-fixture journey must not have a service-worker controller');
  ctx.log(`fixture context secure=${state.secure} controller=${state.controller} serviceWorkers=block`);
}

async function openControlledSystem(ctx, payload, peer = null) {
  ctx.fixtureHits = { statusGet: 0, workersGet: 0, checkPost: 0 };
  ctx.peer = peer;
  await ctx.page.route(`**${UPDATE_PATH}`, async (route) => {
    if (route.request().method() === 'GET') {
      ctx.fixtureHits.statusGet++;
      await route.fulfill({ json: payload });
    } else {
      expect(!!peer && route.request().method() === 'POST' && isDeepStrictEqual(route.request().postDataJSON(), { action: 'check' }), 'controlled check only, never an updater refresh');
      ctx.fixtureHits.checkPost++;
      await route.fulfill({ json: peer.receipt });
    }
  });
  if (peer) await ctx.page.route(`**${UPDATE_PATH}/workers`, async (route) => {
    expect(route.request().method() === 'GET', 'worker fixture is read-only');
    ctx.fixtureHits.workersGet++;
    await route.fulfill({ json: peer.workers });
  });
  const firstRead = ctx.page.waitForResponse(isResponse(UPDATE_PATH, 'GET'));
  await openSystem(ctx.page, ctx.status);
  await assertFixtureResponse(ctx, 'statusGet', await firstRead, payload);
  await assertNoServiceWorker(ctx);
}

async function checkControlledPeer(ctx) {
  const check = ctx.page.waitForResponse(isResponse(UPDATE_PATH, 'POST'));
  const workers = ctx.page.waitForResponse(isResponse(`${UPDATE_PATH}/workers`, 'GET'));
  await ctx.page.getByRole('button', { name: 'Check now', exact: true }).click();
  await assertFixtureResponse(ctx, 'checkPost', await check, ctx.peer.receipt);
  expect(ctx.fixtureHits.checkPost === 1, 'ordinary Check POST is dispatched exactly once');
  await assertFixtureResponse(ctx, 'workersGet', await workers, ctx.peer.workers);
  await ctx.page.getByRole('button', { name: 'Update details for Other worker', exact: true }).click();
}

async function openSystem(page, status) {
  await page.goto(new URL(process.env.UPDATE_REAL_MOBILE ? '/mobile/' : '/', status.urls.ui).href);
  if (!process.env.UPDATE_REAL_MOBILE) {
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView?.type === 'session');
  }
  if (process.env.UPDATE_REAL_MOBILE) {
    // The real mobile shell owns a separate drawer, not the responsive desktop sidebar.
    await page.getByRole('button', { name: 'Open menu', exact: true }).click();
  } else {
    await page.getByRole('complementary', { name: 'Chats' })
      .or(page.getByRole('button', { name: /^(Show sidebar|Chats)\b/ })).filter({ visible: true }).first().waitFor();
    await showSidebar(page);
  }
  await page.getByRole('button', { name: 'Settings', exact: true }).click();
  await page.getByRole('navigation', { name: 'Settings', exact: true }).getByRole('button', { name: 'System', exact: true }).click();
}

await runJourney({
  name: 'update-runtime',
  viewports: ['desktop', 'phone'],
  throttles: ['slow'],
  allow: DEV_INSTANCE_NOISE,
  // page.route cannot intercept API fetches owned by the product service worker.
  browser: async () => ({ context: { serviceWorkers: 'block' } }),
  cases: [
    {
      name: 'real-local-status',
      async setup(ctx) {
        const response = await ctx.page.request.get(new URL('/api/v1/settings/application-updates', ctx.status.urls.ui).href);
        expect(response.ok(), 'real local update status is readable');
        ctx.produced = await response.json();
        expect(ctx.produced.updater?.refresh === 'unknown', 'unmanaged devloop reports unknown, never current');
        await openSystem(ctx.page, ctx.status);
        await assertNoServiceWorker(ctx);
      },
      async act({ page, mark }) {
        await mark('open connected-worker details from the real local status producer');
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page, variant }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByText('Unknown', { exact: true }).first().waitFor();
        await panel.getByText('No update timings reported yet.', { exact: true }).waitFor();
        expect(await panel.getByRole('button', { name: /Refresh updater/ }).count() === 0, 'unknown is not a refresh capability');
      }
    },
    {
      name: 'controlled-distinct-updater-and-timings',
      async setup(ctx) {
        await openControlledSystem(ctx, {
          allow_updates: true, update_all_workers: true, supported: true, phase: 'idle',
          running_version: 'a'.repeat(40), version: 'a'.repeat(40),
          updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' },
          diagnostics: { revision: 'a'.repeat(40), total_elapsed_ms: 1500, stages: [
            { stage: 'assembly', elapsed_ms: 1400, outcome: 'completed' },
            { stage: 'tools_cache', elapsed_ms: 30, outcome: 'hit' },
            { stage: 'prebuilt_ui', elapsed_ms: 20, outcome: 'fallback' }
          ] }
        });
      },
      async act({ page, mark }) {
        await mark('inspect controlled resident identity and nested timings');
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page, variant }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByText('aaaaaaaaaaaa', { exact: true }).first().waitFor();
        await panel.getByText('bbbbbbbbbbbb', { exact: true }).waitFor();
        await panel.getByText('1.5 s total', { exact: true }).waitFor();
        await panel.getByText(/separately operator-approved container restart/).waitFor();
        expect(await panel.getByRole('button').count() === 0, 'valid legacy available metadata stays read-only');
        expect(await panel.getByRole('group').count() === 0, 'no updater restart confirmation');
        await panel.getByText('Reused verified cache', { exact: true }).waitFor();
        await panel.getByText('Prebuilt UI not reused; using a source build instead.', { exact: true }).waitFor();
        const size = await panel.evaluate((el) => ({ width: el.clientWidth, scroll: el.scrollWidth, viewport: innerWidth, page: document.documentElement.scrollWidth }));
        expect(size.scroll <= size.width && size.page <= size.viewport, 'details fit without horizontal overflow');
        if (process.env.JOURNEY_SCREENSHOTS) {
          mkdirSync(process.env.JOURNEY_SCREENSHOTS, { recursive: true });
          await panel.scrollIntoViewIfNeeded();
          await page.screenshot({ path: join(process.env.JOURNEY_SCREENSHOTS, `controlled-update-${process.env.UPDATE_REAL_MOBILE ? 'mobile-shell' : 'desktop-shell'}-${variant.viewport}.png`) });
        }
      }
    },
    ...[
      ['paused', { allow_updates: false }],
      ['pending', { request_pending: true, install_pending: true }],
      ['failed', { phase: 'error' }]
    ].map(([name, extra]) => ({
      name: `controlled-${name}-report-is-read-only`,
      async setup(ctx) {
        await openControlledSystem(ctx, {
          allow_updates: true, update_all_workers: false, supported: true, phase: 'idle',
          running_version: 'a'.repeat(40),
          updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' },
          ...extra
        });
      },
      async act({ page, mark }) {
        await mark(`inspect controlled ${name} guard`);
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        expect(await panel.getByRole('button').count() === 0, `${name} has no updater action`);
        expect(await panel.getByRole('group').count() === 0, `${name} has no updater confirmation`);
      }
    })),
    ...['not-a-revision', null].map((revision) => ({
      name: `controlled-${revision === null ? 'unknown' : 'malformed'}-identity-is-not-refreshable`,
      async setup(ctx) {
        await openControlledSystem(ctx, {
          allow_updates: true, update_all_workers: true, supported: true, phase: 'idle', running_version: 'a'.repeat(40),
          updater: { revision, capabilities: ['refresh-updater-v1'], refresh: 'available' },
          diagnostics: { revision: 'bogus', total_elapsed_ms: -50, stages: 'malformed' }
        });
      },
      async act({ page, mark }) {
        await mark('inspect controlled malformed legacy metadata without dispatch');
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByText('aaaaaaaaaaaa', { exact: true }).waitFor();
        await panel.getByText('Unknown', { exact: true }).waitFor();
        expect(await panel.getByRole('button', { name: /Refresh updater/ }).count() === 0, 'unknown identity cannot enable refresh');
        expect(await panel.getByText(/A newer updater can be loaded/).count() === 0, 'unknown identity cannot prove a newer updater');
        expect(await panel.getByRole('list', { name: 'Preparation stages' }).count() === 0, 'malformed diagnostics cannot become stage rows');
        await panel.getByText('No update timings reported yet.', { exact: true }).waitFor();
      }
    })),
    ...[
      ['legacy', { revision: null, capabilities: [], refresh: 'unknown' }, null],
      ['unknown', { revision: null, capabilities: ['refresh-updater-v1'], refresh: 'available' }, null],
      ['malformed', { revision: 'not-a-revision', capabilities: ['refresh-updater-v1'], refresh: 'available' },
        { revision: 'bogus', total_elapsed_ms: -50, stages: 'malformed' }],
      ['available', { revision: 'c'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' }, null]
    ].map(([name, updater, diagnostics]) => ({
      name: `controlled-${name}-peer-is-read-only-and-never-borrows-local-identity`,
      async setup(ctx) {
        await openControlledSystem(ctx, {
          allow_updates: true, update_all_workers: true, supported: true, phase: 'idle', running_version: 'a'.repeat(40),
          updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' }
        }, {
          receipt: { results: [{ device_id: 'peer', device_name: 'Other worker', accepted: false, reason: 'Offline; not queued' }] },
          workers: { allow_updates: true, update_all_workers: true, workers: [{ device_id: 'peer', device_name: 'Other worker',
            online: false, supported: false, phase: 'unsupported', running_version: 'd'.repeat(40), updater, diagnostics }] }
        });
      },
      async act(ctx) {
        await ctx.mark(`read controlled ${name} per-worker check results without updater action`);
        await checkControlledPeer(ctx);
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Other worker update details' });
        await panel.getByText('dddddddddddd', { exact: true }).waitFor();
        await panel.getByText(name === 'available' ? 'cccccccccccc' : 'Unknown', { exact: true }).waitFor();
        expect(!(await panel.innerText()).includes('aaaaaaaaaaaa'), 'never borrow the connected app revision');
        expect(!(await panel.innerText()).includes('bbbbbbbbbbbb'), 'never borrow the connected resident revision');
        expect(await panel.getByRole('button').count() === 0, 'peer details have no updater action');
        expect(await panel.getByRole('group').count() === 0, 'peer details have no updater confirmation');
        expect(await panel.getByRole('list', { name: 'Preparation stages' }).count() === 0, 'legacy or malformed timings do not become rows');
        const size = await panel.evaluate((el) => ({ width: el.clientWidth, scroll: el.scrollWidth, viewport: innerWidth, page: document.documentElement.scrollWidth }));
        expect(size.scroll <= size.width && size.page <= size.viewport, 'peer details fit without horizontal overflow');
      }
    }))
  ]
});
