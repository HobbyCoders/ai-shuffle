import { runJourney } from "./runner.mjs";
import {
  openNewChat,
  withChatSetup,
  pickAccess,
  expect,
  composer,
  waitTurnSettled,
} from "./lib/app.mjs";

import { DEV_INSTANCE_NOISE } from "./lib/noise.mjs";

// Hold only delivery to the tab. For the admitted case the real POST (and
// fake-provider turn) completes on the disposable worker before the reload.
function reloadCase(outcome) {
  return {
    name: outcome,
    allow: outcome === "live-loss" ? [{ kind: "failed_requests", url: /^\/api\/v1\/sessions\/with-worktree$/, reason: "the case resets this one POST after verifying origin admission" }] : [],
    async setup(ctx) {
      const { page } = ctx;
      const hero = await openNewChat(ctx, { project: "Dev sandbox" });
      await pickAccess(page, "Bypass");
      await withChatSetup(page, async () => {
        await hero
          .getByRole("button", { name: /Switch to a worktree$/ })
          .or(hero.locator(".wt-chip"))
          .click();
        await hero.getByRole("button", { name: /^Worktree branch:/ }).waitFor();
      });
      const evidence = { creates: 0, queries: 0, receipts: 0, id: null };
      ctx.evidence = evidence;
      page.on("websocket", (ws) =>
        ws.on("framesent", ({ payload }) => {
          try {
            const frame = JSON.parse(String(payload));
            if (frame.type === "query") evidence.queries++;
          } catch {
            /* unrelated */
          }
        }),
      );
      page.on("request", (req) => {
        const path = new URL(req.url()).pathname;
        if (path.endsWith("/sessions/with-worktree")) evidence.creates++;
        if (path.endsWith("/sessions/with-worktree/receipt"))
          evidence.receipts++;
      });
      if (outcome === "late-admission") {
        await page.route("**/sessions/with-worktree", async (route) => {
          const req = route.request();
          // Drop the client connection; complete its one POST from the test
          // transport only after the reloaded tab has exhausted the first batch.
          await route.abort("failed");
          await page.waitForEvent(
            "request",
            (r) => r.url().endsWith("/receipt") && evidence.receipts > 8,
          );
          const response = await page.request.fetch(req);
          const body = await response.json();
          expect(
            response.ok() && body.query_started,
            "late create really admitted its turn",
          );
          evidence.id = body.session_id;
        });
      }
      await page.evaluate((outcome) => {
        const original = window.fetch.bind(window);
        window.fetch = async (input, init) => {
          const path = new URL(
            typeof input === "string" ? input : input.url,
            location.href,
          ).pathname;
          if (
            outcome === "before-dispatch" &&
            path.endsWith("/git/generate-branch-name")
          ) {
            window.__reloadReady = true;
            return new Promise(() => {});
          }
          // Let the real transport error reach sendMessage before reloading;
          // receipt delivery resumes normally in the next document.
          if (
            outcome === "delivery-lost" &&
            path.endsWith("/with-worktree/receipt")
          )
            return new Promise(() => {});
          if (path.endsWith("/sessions/with-worktree")) {
            if (outcome === "ambiguous") {
              window.__reloadReady = true;
              return new Promise(() => {});
            }
            if (outcome === "late-admission") {
              try {
                return await original(input, init);
              } finally {
                window.__reloadReady = true;
              }
            }
            if (outcome === "refused") {
              const invalid = JSON.parse(init.body);
              invalid.project_id = "";
              delete invalid.project_v2_id; // real origin validation rejects before creation
              const reply = await original(input, {
                ...init,
                body: JSON.stringify(invalid),
              });
              if (reply.ok || reply.status >= 500)
                throw Error("expected definitive create refusal");
              window.__reloadReady = true;
              return reply;
            }
            const response = await original(input, init),
              body = await response.clone().json();
            if (!response.ok || !body.query_started)
              throw Error("Expected admitted opening turn");
            window.__reloadReady = body.session_id;
            if (outcome === "delivery-lost")
              throw new TypeError("Response body lost after admission");
            return new Promise(() => {});
          }
          return original(input, init);
        };
      }, outcome);
      ctx.words = "Keep this worktree message " + Date.now();
      await composer(page).fill(ctx.words);
      if (outcome === "live-loss") {
        await page.route("**/sessions/with-worktree", async route => {
          const response = await route.fetch();
          const body = await response.json();
          expect(response.ok() && body.query_started, "origin admitted opening turn");
          evidence.id = body.session_id;
          await route.abort("connectionreset");
        });
        return; // Send belongs inside the recorded window for this case.
      }
      await page.getByRole("button", { name: "Send", exact: true }).click();
      await page.waitForFunction(() => window.__reloadReady);
      ctx.expected = await page.evaluate(() => window.__reloadReady);
      if (outcome === "delivery-lost") {
        await page.getByRole("region", { name: "Opening message" }).waitFor();
        expect(
          await page.evaluate(
            () => !!sessionStorage.getItem("__aihub_draft_worktree-opening"),
          ),
          "delivery failure kept exact receipt handle",
        );
      }
    },
    async act({ page, words, expected, mark, evidence }) {
      if (outcome === "live-loss") {
        await page.getByRole("button", { name: "Send", exact: true }).click();
        await waitTurnSettled(page);
        const id = await page.evaluate(() => JSON.parse(sessionStorage.getItem("ai-shuffle-active-session") || "{}").sessionId);
        expect(id && id === evidence.id && evidence.receipts > 0, "exact receipt chat without reload");
        const rows = await (await page.request.get(new URL(`/api/v1/sessions/${id}`, page.url()).href)).json();
        expect(rows.messages.filter(m => m.role === "user" && m.content === words).length === 1 && evidence.creates === 1 && !evidence.queries, "one create, one durable user turn, no fallback query");
        expect(!(await composer(page).innerText()).trim(), "admitted words stay out of Send");
        return;
      }
      // Record the old document before the deliberate replacement.
      await page.evaluate(
        () =>
          new Promise((r) =>
            requestAnimationFrame(() => requestAnimationFrame(r)),
          ),
      );
      await mark("reload the tab");
      await page.reload({ waitUntil: "domcontentloaded" });
      await page.evaluate(
        () =>
          new Promise((r) =>
            requestAnimationFrame(() => requestAnimationFrame(r)),
          ),
      );
      await mark("new document");
      const box = composer(page);
      if (outcome === "ambiguous") {
        const recovery = page.getByRole("region", { name: "Opening message" });
        await recovery.getByText(words, { exact: true }).waitFor();
        expect(
          !(await box.count()) && evidence.queries === 0,
          "unknown words are readable, not sendable",
        );
        expect(
          await page.evaluate(
            () => !!sessionStorage.getItem("__aihub_draft_worktree-opening"),
          ),
          "retain correlation after timeout",
        );
        return;
      }
      if (outcome === "late-admission") {
        await page
          .getByText("Delivery not confirmed", { exact: true })
          .waitFor();
        expect(
          !(await box.count()),
          "no unsent draft while admission is unknown",
        );
      }
      await box.waitFor();
      if (typeof expected === "string" || outcome === "late-admission") {
        expected = evidence.id ?? expected;
        await page
          .getByText(words, { exact: true })
          .first()
          .waitFor({ timeout: 20000 });
        await page.waitForFunction(
          (id) =>
            location.search.includes(id) ||
            JSON.parse(
              sessionStorage.getItem("ai-shuffle-active-session") || "{}",
            ).sessionId === id,
          expected,
          { timeout: 10000 },
        );
        expect(
          !(await box.innerText()).trim(),
          "no admitted prompt in composer",
        );
        const rows = await page.evaluate(
          async (id) =>
            (await (await fetch(`/api/v1/sessions/${id}`)).json()).messages,
          expected,
        );
        expect(
          rows.filter((m) => m.role === "user" && m.content === words)
            .length === 1 &&
            evidence.creates === 1 &&
            evidence.queries === 0,
          "one create, one durable turn and no fallback query",
        );
      } else {
        await page.waitForFunction(
          (text) =>
            document
              .querySelector("[contenteditable=true]")
              ?.textContent.includes(text),
          words,
          { timeout: 15000 },
        );
        expect(
          (await box.innerText()).trim() === words,
          "only one copy of the draft",
        );
      }
    },
  };
}
await runJourney({
  name: "auto-worktree-reload",
  browser: async () => ({ context: { serviceWorkers: "block" } }),
  cases: [
    "live-loss",
    "before-dispatch",
    "admitted",
    "ambiguous",
    "delivery-lost",
    "late-admission",
    "refused",
  ].map(reloadCase),
  allow: DEV_INSTANCE_NOISE,
});
