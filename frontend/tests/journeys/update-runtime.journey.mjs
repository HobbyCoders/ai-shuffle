// Connected-worker update details. Controlled responses below test rendering,
// not a real updater transition; the first case reads the real status producer.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { runJourney } from './runner.mjs';
import { expect, showSidebar } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

async function openSystem(page, status) {
  await page.goto(new URL(process.env.UPDATE_REAL_MOBILE ? '/mobile/' : '/', status.urls.ui).href);
  if (!process.env.UPDATE_REAL_MOBILE) {
    await page.waitForFunction(() => JSON.parse(localStorage.getItem('ai-shuffle-layout') || 'null')?.activeView?.type === 'session');
  }
  if (process.env.UPDATE_REAL_MOBILE) {
    // The real mobile shell owns a separate drawer, not the responsive desktop sidebar.
    await page.getByRole('button', { name: 'Open menu', exact: true }).click();
  } else {
    await page.getByRole('complementary', { name: 'Sessions' })
      .or(page.getByRole('button', { name: 'Show sidebar' })).filter({ visible: true }).first().waitFor();
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
  cases: [
    {
      name: 'real-local-status',
      async setup(ctx) {
        const response = await ctx.page.request.get(new URL('/api/v1/settings/application-updates', ctx.status.urls.ui).href);
        expect(response.ok(), 'real local update status is readable');
        ctx.produced = await response.json();
        expect(ctx.produced.updater?.refresh === 'unknown', 'unmanaged devloop reports unknown, never current');
        await openSystem(ctx.page, ctx.status);
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
      async setup({ page, status }) {
        await page.route('**/api/v1/settings/application-updates', async (route) => {
          expect(route.request().method() === 'GET', 'this controlled case never dispatches an update');
          await route.fulfill({ json: {
            allow_updates: true, update_all_workers: true, supported: true, phase: 'idle',
            running_version: 'a'.repeat(40), version: 'a'.repeat(40),
            updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' },
            diagnostics: { revision: 'a'.repeat(40), total_elapsed_ms: 1500, stages: [
              { stage: 'assembly', elapsed_ms: 1400, outcome: 'completed' },
              { stage: 'tools_cache', elapsed_ms: 30, outcome: 'hit' },
              { stage: 'prebuilt_ui', elapsed_ms: 20, outcome: 'fallback' }
            ] }
          } });
        });
        await openSystem(page, status);
      },
      async act({ page, mark }) {
        await mark('inspect controlled resident identity and nested timings');
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByRole('button', { name: 'Refresh updater…', exact: true }).click();
        await panel.getByText('Restart the connected worker to refresh its updater?', { exact: true }).waitFor();
        await mark('cancel the current-worker-only confirmation without any update request');
        await panel.getByRole('button', { name: 'Cancel', exact: true }).click();
      },
      async verify({ page, variant }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByText('aaaaaaaaaaaa', { exact: true }).first().waitFor();
        await panel.getByText('bbbbbbbbbbbb', { exact: true }).waitFor();
        await panel.getByText('1.5 s total', { exact: true }).waitFor();
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
      name: `controlled-${name}-blocks-refresh`,
      async setup({ page, status }) {
        await page.route('**/api/v1/settings/application-updates', async (route) => {
          expect(route.request().method() === 'GET', 'blocked states must not dispatch an update');
          await route.fulfill({ json: {
            allow_updates: true, update_all_workers: false, supported: true, phase: 'idle',
            running_version: 'a'.repeat(40),
            updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' },
            ...extra
          } });
        });
        await openSystem(page, status);
      },
      async act({ page, mark }) {
        await mark(`inspect controlled ${name} guard`);
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        expect(await panel.getByRole('button', { name: 'Refresh updater…', exact: true }).isDisabled(), `${name} prevents refresh`);
      }
    })),
    ...['not-a-revision', null].map((revision) => ({
      name: `controlled-${revision === null ? 'unknown' : 'malformed'}-identity-is-not-refreshable`,
      async setup({ page, status }) {
        await page.route('**/api/v1/settings/application-updates', async (route) => {
          expect(route.request().method() === 'GET', 'malformed metadata must never dispatch an update');
          await route.fulfill({ json: {
            allow_updates: true, update_all_workers: true, supported: true, phase: 'idle', running_version: 'a'.repeat(40),
            updater: { revision, capabilities: ['refresh-updater-v1'], refresh: 'available' },
            diagnostics: { revision: 'bogus', total_elapsed_ms: -50, stages: 'malformed' }
          } });
        });
        await openSystem(page, status);
      },
      async act({ page, mark }) {
        await mark('inspect controlled malformed legacy metadata without dispatch');
        await page.getByRole('button', { name: 'Update details', exact: true }).click();
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Connected worker update details' });
        await panel.getByText('Unknown', { exact: true }).waitFor();
        expect(await panel.getByRole('button', { name: /Refresh updater/ }).count() === 0, 'unknown identity cannot enable refresh');
        expect(await panel.getByText(/A newer updater can be loaded/).count() === 0, 'unknown identity cannot prove a newer updater');
        expect(await panel.getByRole('list', { name: 'Preparation stages' }).count() === 0, 'malformed diagnostics cannot become stage rows');
        await panel.getByText('No update timings reported yet.', { exact: true }).waitFor();
      }
    })),
    {
      name: 'controlled-legacy-peer-never-borrows-local-identity',
      async setup({ page, status }) {
        await page.route('**/api/v1/settings/application-updates', async (route) => {
          const post = route.request().method() === 'POST';
          if (post) expect(route.request().postDataJSON().action === 'check', 'controlled check only, never a refresh fanout');
          await route.fulfill({ json: post
            ? { results: [{ device_id: 'peer', device_name: 'Other worker', accepted: false, reason: 'Offline; not queued' }] }
            : { allow_updates: true, update_all_workers: true, supported: true, phase: 'idle', running_version: 'a'.repeat(40),
                updater: { revision: 'b'.repeat(40), capabilities: ['refresh-updater-v1'], refresh: 'available' } }
          });
        });
        await page.route('**/api/v1/settings/application-updates/workers', async (route) => route.fulfill({ json: {
          allow_updates: true, update_all_workers: true, workers: [{ device_id: 'peer', device_name: 'Other worker',
            online: false, supported: false, phase: 'unsupported', updater: { revision: null, capabilities: [], refresh: 'unknown' }, diagnostics: null }]
        } }));
        await openSystem(page, status);
      },
      async act({ page, mark }) {
        await mark('read controlled per-worker check results');
        await page.getByRole('button', { name: 'Check now', exact: true }).click();
        await page.getByRole('button', { name: 'Update details for Other worker', exact: true }).click();
      },
      async verify({ page }) {
        const panel = page.getByRole('region', { name: 'Other worker update details' });
        expect((await panel.innerText()).includes('Unknown'), 'legacy peer identity remains unknown');
        expect(!(await panel.innerText()).includes('aaaaaaaaaaaa'), 'never borrow the app revision of the connected worker');
        expect(!(await panel.innerText()).includes('bbbbbbbbbbbb'), 'never borrow the resident revision of the connected worker');
        expect(await panel.getByRole('button', { name: /Refresh updater/ }).count() === 0, 'peer details have no refresh action');
      }
    }
  ]
});
