/**
 * An API key's Usage tab. Ported from OpenRouterCredits.test.ts and
 * OpenAICredits.test.ts: OpenRouter reads credits with the saved key (no
 * separate key, legacy management keys kept); OpenAI has no readable balance
 * and points at Platform billing; neither asks for another key.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/svelte";

const mocks = vi.hoisted(() => ({ hydrate: vi.fn(), checkAuth: vi.fn() }));
vi.mock("$lib/constants/models.svelte", async (importOriginal) => ({
  ...(await importOriginal<typeof import("$lib/constants/models.svelte")>()),
  hydrateModelRegistry: mocks.hydrate,
}));
vi.mock("$lib/stores/auth.svelte", async (importOriginal) => ({
  ...(await importOriginal<typeof import("$lib/stores/auth.svelte")>()),
  auth: { checkAuth: mocks.checkAuth },
}));

import { providers } from "$lib/stores/providers.svelte";
import { settingsNav } from "$lib/settings/navigation.svelte";
import ProvidersCollection from "./ProvidersCollection.svelte";
import { providerDrafts } from "./drafts.svelte";
import { row } from "./fixtures";
import { fakeWorker } from "./fake-worker";

const router = row({
  id: "router",
  name: "OpenRouter API key",
  provider_type: "openrouter",
  base_url: "https://openrouter.ai/api",
});
const credits = (configured: boolean) => ({
  configured,
  credits: { total_credits: 25, total_usage: 4.25, balance_remaining: 20.75 },
  fetched_at: 1000,
  last_error_kind: null,
  source: "OpenRouter credits API",
  scope: "Account",
});

let worker: ReturnType<typeof fakeWorker>;

async function flush(times = 6) {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function usageOf(item: string) {
  await providers.refreshAll();
  settingsNav.go({ category: "providers", item, tab: "Usage" });
  render(ProvidersCollection);
  await flush();
  return screen.getByRole("tabpanel");
}

function start(configured = false) {
  worker = fakeWorker(
    { rows: [router, row()] },
    { "GET /model-providers/:id/openrouter-usage": () => credits(configured) },
  );
  vi.stubGlobal("fetch", worker.fetch);
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.hydrate.mockResolvedValue(true);
  mocks.checkAuth.mockResolvedValue(undefined);
  providers.clear();
  providerDrafts.list = [];
  providerDrafts.gateways = [];
  settingsNav.reset();
  sessionStorage.clear();
  start();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it.each([false, true])(
  "OpenRouter: reads credits with the saved key and keeps a legacy management key (legacy=%s)",
  async (configured) => {
    start(configured);
    const panel = await usageOf("mp:router");
    expect(within(panel).getByText("$20.75")).toBeTruthy();
    expect(within(panel).getByText("Read with this key")).toBeTruthy();
    expect(!!within(panel).queryByText("Legacy management key")).toBe(
      configured,
    );
    expect(
      within(panel)
        .getByRole("link", { name: "Open OpenRouter" })
        .getAttribute("href"),
    ).toBe("https://openrouter.ai/settings/credits");
    expect(panel.querySelector("input, form")).toBeNull();
    expect(
      worker.sent("GET", "/model-providers/router/openrouter-usage"),
    ).toHaveLength(1);
    expect(
      worker.calls.filter(
        (c) => c.path.includes("openrouter-usage") && c.method !== "GET",
      ),
    ).toEqual([]);
    // The balance also heads the detail.
    expect(screen.getByText("$20.75", { selector: "dd" })).toBeTruthy();
  },
);

it("OpenAI: explains the missing API balance without asking for another key", async () => {
  const panel = await usageOf("mp:p1");
  expect(within(panel).getByText("Not available for this key")).toBeTruthy();
  await fireEvent.click(
    within(panel).getByRole("button", { name: "About Balance" }),
  );
  const tip = screen.getByRole("tooltip").textContent ?? "";
  expect(tip).toMatch(
    /does not expose a prepaid balance through the model API key/,
  );
  expect(tip).toMatch(
    /Admin key reports organization spending, not credit left/,
  );
  expect(tip).toMatch(/ChatGPT credits are separate from API billing/);
  const billing = within(panel).getByRole("link", { name: "Open billing" });
  expect(billing.getAttribute("href")).toBe(
    "https://platform.openai.com/settings/organization/billing/overview",
  );
  expect(billing.getAttribute("target")).toBe("_blank");
  expect(billing.getAttribute("rel")).toContain("noopener");
  expect(panel.querySelector("input, form")).toBeNull();
  expect(
    worker.calls.filter((c) => /openai-usage|openrouter-usage/.test(c.path)),
  ).toEqual([]);
  expect(
    screen.getByText("Not available for this key", { selector: "dd" }),
  ).toBeTruthy();
});

it("a gateway has no Usage tab", async () => {
  worker = fakeWorker({
    rows: [
      row({
        id: "gw-1",
        name: "Homelab vLLM",
        provider_type: "custom",
        base_url: "http://10.0.0.200:8000/v1",
      }),
    ],
  });
  vi.stubGlobal("fetch", worker.fetch);
  await providers.refreshAll();
  settingsNav.go({ category: "providers", item: "mp:gw-1" });
  render(ProvidersCollection);
  await flush();
  expect(screen.getAllByRole("tab").map((t) => t.textContent?.trim())).toEqual([
    "Overview",
    "Models",
  ]);
});
