// A stale/deleted agent selection must not strand an optimistic steer.
// Corrupt only the next outgoing selection, not the response: the real server
// executes its missing-profile preflight (no seeded agents need deleting).
import { runJourney } from './runner.mjs';
import { composer, expect, openNewChat, typeAndSend, uniq, waitChat } from './lib/app.mjs';
import { DEV_INSTANCE_NOISE } from './lib/noise.mjs';

await runJourney({
  name: 'steer-lifecycle',
  // Keep desktop/phone races at 4x CPU; duplicate fast variants cost hosted retry headroom.
  throttles: ['slow'],
  recorder: { selectors: ['pending=.pending-messages', 'cancelling=[data-queue-status="cancelling"]'] },
  cases: [{
    name: 'refused-idle-send',
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `idle first ${uniq()} [script:default tps=200 latency_ms=0]`);
      await waitChat(ctx.page, { streaming: 'false', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `recover idle words ${uniq()}`;
      await mark('idle send with a stale agent selection');
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'query') {
            data.profile = 'journey-missing-agent';
            WebSocket.prototype.send = send;
            const onmessage = this.onmessage;
            this.onmessage = function(event) {
              const reply = JSON.parse(event.data);
              if (reply.type === 'error' && reply.client_message_id === data.client_message_id) {
                this.onmessage = onmessage;
                window.__releaseIdleRefusal = () => onmessage.call(this, event);
                return;
              }
              return onmessage.call(this, event);
            };
            return send.call(this, JSON.stringify(data));
          }
          return send.call(this, frame);
        };
      });
      await typeAndSend(page, text);
      await page.waitForFunction(() => !!window.__releaseIdleRefusal);
      expect(await page.locator('[data-ui-row]').filter({ hasText: text }).count() === 1, 'in-flight idle send has its optimistic echo');
      // Send and the actual server rejection are separate causal steps. The
      // required optimistic-row retraction is not an unexplained UI reversal.
      await mark('deliver the real correlated server refusal');
      await page.evaluate(() => window.__releaseIdleRefusal());
      await page.waitForFunction(() => document.body.innerText.includes('That agent no longer exists'));
      await waitChat(page, { streaming: 'false' });
      expect((await composer(page).innerText()).includes(text), 'idle refusal returns text to the composer');
      expect(await page.locator('[data-ui-row]').filter({ hasText: text }).count() === 0, 'refused idle echo is not a saved message');
    }
  }, {
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
    // Nothing proves the withdrawal happened, so the page must say so and allow a retry.
    name: 'lost-cancel-request',
    // Viewport copies of this pair add no signal; one each keeps hosted shard time.
    viewports: ['phone'],
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `cancel that never arrived ${uniq()}`;
      await typeAndSend(page, text);
      await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
      await page.evaluate(() => {
        const send = WebSocket.prototype.send;
        WebSocket.prototype.send = function(frame) {
          const data = typeof frame === 'string' ? JSON.parse(frame) : null;
          if (data?.type === 'remove_queued_message') {
            WebSocket.prototype.send = send;
            return undefined; // Lost on the way out: the server never hears it.
          }
          return send.call(this, frame);
        };
      });
      await mark('cancel but lose the request on the way out');
      await page.getByRole('button', { name: 'Unsend message and edit it' }).last().click();
      await page.getByText('Cancelling…', { exact: true }).waitFor();
      await page.waitForFunction(() => document.body.innerText.includes('Cancellation was not confirmed'), null, { timeout: 15_000 });
      const unsend = page.getByRole('button', { name: 'Unsend message and edit it' }).last();
      expect(await unsend.isEnabled(), 'an unconfirmed cancellation can be retried');
      await mark('retry the cancellation');
      await unsend.click();
      await page.locator('.pending-messages').waitFor({ state: 'hidden' });
      expect((await composer(page).innerText()).includes(text), 'a retry recovers the cancelled words');
    }
  }, {
    // The verdict is lost but the durable receipt arrives (#1273): the receipt
    // proves the withdrawal, so the words come back with no error and no retry.
    name: 'lost-verdict',
    viewports: ['desktop'],
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const text = `cancel with lost reply ${uniq()}`;
      await typeAndSend(page, text);
      await page.locator('.pending-messages [data-queue-confirmed="true"]').waitFor();
      // A receipt answers only a row queued before it, compared by server
      // timestamp, so the two must not land on the same instant.
      await page.waitForTimeout(1_100);
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
      await page.locator('.pending-messages').waitFor({ state: 'hidden', timeout: 8_000 });
      await composer(page).getByText(text).waitFor({ timeout: 5_000 });
      expect(!(await page.locator('body').innerText()).includes('Cancellation was not confirmed'), 'the receipt leaves no false error');
    }
  }, {
    name: 'touch-unsend-targets',
    viewports: ['phone', 'fold-inner'],
    async setup(ctx) {
      await openNewChat(ctx);
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=2 latency_ms=1500]`);
      await waitChat(ctx.page, { streaming: 'true', messagesAtLeast: 2 });
    },
    async act({ page, mark }) {
      const first = `first touch words ${uniq()}`, second = `second touch words ${uniq()}`;
      await typeAndSend(page, first);
      await typeAndSend(page, second);
      await page.waitForFunction(() => document.querySelectorAll('.pending-messages [data-queue-confirmed="true"]').length === 2);
      const isolated = await page.evaluate(() => {
        const interrupt = document.querySelector('.pending-action');
        const r = interrupt.getBoundingClientRect();
        const interruptHits = [0.25, r.width / 2, r.width - 0.25].every(x =>
          [0.25, r.height / 2, r.height - 0.25].every(y =>
            document.elementFromPoint(r.x + x, r.y + y)?.closest('button') === interrupt));
        const textHits = [...document.querySelectorAll('.pending-messages .bubble-text')].every(el => {
          const t = el.getBoundingClientRect();
          return [0.25, t.width / 2, t.width - 0.25].every(x =>
            [0.25, t.height / 2, t.height - 0.25].every(y =>
              !document.elementFromPoint(t.x + x, t.y + y)?.closest('.hover-remove')));
        });
        return { interruptHits, textHits };
      });
      expect(isolated.interruptHits, 'the entire Interrupt box hits Interrupt, never Unsend');
      expect(isolated.textHits, 'the bubble text is not covered by the hovering Unsend target');
      for (const [selector, remaining] of [['.hover-remove', 1], ['.pending-remove', 0]]) {
        const target = await page.locator(selector).evaluate(el => {
          const r = el.getBoundingClientRect(), hit = getComputedStyle(el, '::before');
          const x = r.x + r.width / 2 - 11, y = r.y + r.height / 2;
          return { x, y, width: parseFloat(hit.width), height: parseFloat(hit.height),
            hits: document.elementFromPoint(x, y)?.closest('button') === el };
        });
        expect(target.width >= 24 && target.height >= 24 && target.hits, `${selector} has a real 24px touch target`);
        await mark(`unsend via ${selector} outside its visible glyph`);
        await page.mouse.click(target.x, target.y);
        await page.waitForFunction(n => document.querySelectorAll('.pending-messages .bubble').length === n, remaining);
      }
      const draft = await composer(page).innerText();
      expect(draft.includes(first) && draft.includes(second), 'both expanded touch targets recover their own message');
    }
  }, {
    name: 'delivery-wins',
    async setup(ctx) {
      await openNewChat(ctx);
      // Leave a real producer window for throttled Chromium actionability retries.
      await typeAndSend(ctx.page, `working ${uniq()} [script:long tps=100 latency_ms=1500]`);
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
