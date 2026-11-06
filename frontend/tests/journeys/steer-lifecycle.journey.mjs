// A stale/deleted agent selection must not strand an optimistic steer.
// Corrupt only the next outgoing selection, not the response: the real server
// executes its missing-profile preflight (no seeded agents need deleting).
import { runJourney } from './runner.mjs';
import { composer, expect, openNewChat, typeAndSend, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
  name: 'steer-lifecycle',
  recorder: { selectors: ['pending=.pending-messages', 'cancelling=[data-queue-status="cancelling"]'] },
  cases: [{
    name: 'refused-steer',
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
      // Ensure the next query really is a mid-turn send, not the initial one.
      await ctx.page.waitForFunction(() => document.body.innerText.includes('Here is a long'));
    },
    async act({ page, mark }) {
      const text = `recover my unsent words ${uniq()}`;
      await mark('send with a stale agent selection');
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'query') {
            data.profile = 'journey-missing-agent';
            WebSocket.prototype.send = send;
            return send.call(this, JSON.stringify(data));
          }
          return send.call(this, frame);
        };
      });
      await typeAndSend(page, text);
      await page.waitForFunction(() => document.body.innerText.includes('That agent no longer exists'));
      await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 5000 });
      expect((await composer(page).innerText()).includes(text), 'refused text returns to the composer');
    }
  }, {
    name: 'unadmitted-unsend',
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `delayed unsent words ${uniq()}`;
      await mark('delay only the outgoing steer before server admission');
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'query') {
            WebSocket.prototype.send = send;
            window.__lateSteer = () => send.call(this, frame);
            return;
          }
          return send.call(this, frame);
        };
      });
      await typeAndSend(page, text);
      await page.getByText('Send unconfirmed', { exact: true }).waitFor({ timeout: 15_000 });
      await mark('unsend a message the server has never admitted');
      await page.getByRole('button', { name: 'Unsend message and edit it' }).last().click();
      await page.locator('.pending-messages').waitFor({ state: 'hidden' });
      expect((await composer(page).innerText()).includes(text), 'unknown-id unsend recovers the words');
      await mark('the original delayed send arrives after cancellation');
      await page.evaluate(() => window.__lateSteer());
      await page.waitForFunction(() => document.body.innerText.toLowerCase().includes('you cancelled it before it was queued'));
      expect(await page.locator('.pending-messages').count() === 0, 'late send cannot resurrect the bubble');
      expect(await page.locator('[data-ui-row]').filter({ hasText: text }).count() === 0, 'the cancelled command never reaches the transcript');
    }
  }, {
    name: 'lost-verdict',
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `cancel with lost reply ${uniq()}`;
      await typeAndSend(page, text);
      await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'remove_queued_message') {
            const onmessage = this.onmessage;
            this.onmessage = function(event) {
              if (JSON.parse(event.data)?.type === 'queued_message_removal') {
                this.onmessage = onmessage; // Lose just one verdict, not queue snapshots.
                return;
              }
              return onmessage.call(this, event);
            };
            WebSocket.prototype.send = send;
          }
          return send.call(this, frame);
        };
      });
      await mark('cancel but lose the verdict on the way back');
      await page.getByRole('button', { name: 'Unsend message and edit it' }).last().click();
      await page.getByText('Cancelling…', { exact: true }).waitFor();
      await page.waitForFunction(() => document.body.innerText.includes('Cancellation was not confirmed'), null, { timeout: 15_000 });
      const unsend = page.getByRole('button', { name: 'Unsend message and edit it' }).last();
      expect(await unsend.isEnabled(), 'a lost verdict re-enables cancellation');
      await mark('retry the cancellation');
      await unsend.click();
      await page.locator('.pending-messages').waitFor({ state: 'hidden' });
      expect((await composer(page).innerText()).includes(text), 'a retry recovers the cancelled words');
    }
  }, {
    name: 'delivery-wins',
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=300 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `delivered before cancellation ${uniq()}`;
      await typeAndSend(page, text);
      await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'remove_queued_message') {
            WebSocket.prototype.send = send;
            const onmessage = this.onmessage;
            this.onmessage = function(event) {
              const reply = JSON.parse(event.data);
              if (reply.type === 'queued_message_removal') {
                window.__lateCancelVerdict = reply.verdict;
                this.onmessage = onmessage;
              }
              return onmessage.call(this, event);
            };
            window.__lateCancel = () => send.call(this, frame);
            return;
          }
          return send.call(this, frame);
        };
      });
      await mark('request cancellation but let real queue delivery win');
      await page.getByRole('button', { name: 'Unsend message and edit it' }).last().click();
      await page.getByText('Cancelling…', { exact: true }).waitFor();
      await page.getByRole('status').filter({ hasText: 'Message already sent' }).waitFor({ timeout: 15_000 });
      expect(await page.locator('.pending-messages').count() === 0, 'the delivery receipt retires the cancelling bubble');
      expect(!(await composer(page).innerText()).includes(text), 'delivered words are not recovered as unsent');
      await mark('release the cancellation after its delivery receipt');
      await page.evaluate(() => window.__lateCancel());
      await page.waitForFunction(() => window.__lateCancelVerdict === 'already_delivered');
      expect(await page.locator('.pending-messages').count() === 0, 'the late verdict cannot resurrect the bubble');
    }
  }],
  allow: DEV_INSTANCE_NOISE
});
