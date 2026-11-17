import { runJourney } from "./runner.mjs";
import {
  openNewChat,
  withChatSetup,
  pickAccess,
  expect,
  composer,
} from "./lib/app.mjs";

import { DEV_INSTANCE_NOISE } from "./lib/noise.mjs";

// Hold only delivery to the tab. For the admitted case the real POST (and
// fake-provider turn) completes on the disposable worker before the reload.
function reloadCase(outcome) {
  return {
    name: outcome,
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
          if (path.endsWith("/sessions/with-worktree")) {
            if (outcome === "ambiguous") {
              window.__reloadReady = true;
              return new Promise(() => {});
            }
            const response = await original(input, init),
              body = await response.clone().json();
            if (!response.ok || !body.query_started)
              throw Error("Expected admitted opening turn");
            window.__reloadReady = body.session_id;
            return new Promise(() => {});
          }
          return original(input, init);
        };
      }, outcome);
      ctx.words = "Keep this worktree message " + Date.now();
      await composer(page).fill(ctx.words);
      await page.getByRole("button", { name: "Send", exact: true }).click();
      await page.waitForFunction(() => window.__reloadReady);
      ctx.expected = await page.evaluate(() => window.__reloadReady);
    },
    async act({ page, words, expected, mark }) {
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
      await box.waitFor();
      if (typeof expected === "string") {
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
  cases: ["before-dispatch", "admitted", "ambiguous"].map(reloadCase),
  allow: DEV_INSTANCE_NOISE,
});
