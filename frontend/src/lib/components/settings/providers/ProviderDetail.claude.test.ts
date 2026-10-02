/**
 * Settings > Providers > Claude: the Claude Code mode and the credentials it
 * picks between. Ported from ClaudeAuthModePanel.test.ts: the active mode,
 * the CLI account and the key's last four, the warning when the active mode
 * has no credential, a mode change only on an explicit pick, and read-only.
 */
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
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
import { SETTINGS_ACCESS_CONTEXT } from "$lib/settings/access.svelte";
import ProvidersCollection from "./ProvidersCollection.svelte";
import { providerDrafts } from "./drafts.svelte";
import { claudeSummary } from "./fixtures";
import { fakeWorker } from "./fake-worker";

let worker: ReturnType<typeof fakeWorker>;

async function flush(times = 6) {
  for (let i = 0; i < times; i++) await new Promise((r) => setTimeout(r, 0));
}

async function show(options: { readOnly?: boolean } = {}) {
  await providers.refreshAll();
  settingsNav.go({ category: "providers", item: "provider:claude" });
  const context = options.readOnly
    ? new Map([[SETTINGS_ACCESS_CONTEXT, { readOnly: true }]])
    : undefined;
  render(ProvidersCollection, context ? { context } : {});
  await flush();
}

const modes = () => screen.getByRole("group", { name: "Runs on" });
const credentials = () => screen.getByRole("region", { name: /^Credentials/ });

beforeEach(() => {
  vi.clearAllMocks();
  mocks.hydrate.mockResolvedValue(true);
  mocks.checkAuth.mockResolvedValue(undefined);
  providers.clear();
  providerDrafts.list = [];
  providerDrafts.gateways = [];
  settingsNav.reset();
  sessionStorage.clear();
  worker = fakeWorker({
    claude: claudeSummary({ api_key: { set: true, last4: "WXYZ" } }),
    integrations: {
      anthropic_api_key_set: true,
      anthropic_api_key_masked: null,
      fal_api_key_set: false,
      fal_api_key_masked: "",
      fal_enabled: true,
    },
  });
  vi.stubGlobal("fetch", worker.fetch);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

it("shows the active mode, the CLI account and the key’s last four", async () => {
  await show();
  expect(
    screen.getByRole("heading", { level: 2, name: "Claude" }),
  ).toBeTruthy();
  expect(screen.getByText("Runs on subscription")).toBeTruthy();
  expect(
    within(modes())
      .getByRole("button", { name: "Subscription" })
      .getAttribute("aria-pressed"),
  ).toBe("true");
  const facts = screen.getByText("Runs on", { selector: "dt" }).closest("div")!;
  expect(within(facts).getByText("Subscription")).toBeTruthy();

  const creds = credentials();
  expect(within(creds).getByText("Claude Max subscription")).toBeTruthy();
  expect(within(creds).getByText("Active for Claude")).toBeTruthy();
  expect(within(creds).getByText("Anthropic API key")).toBeTruthy();
  expect(within(creds).getByText("Standby")).toBeTruthy();
  expect(within(creds).queryByRole("switch")).toBeNull();

  // The list rows carry the account and the key's last four.
  expect(screen.getByText("owner@example.com, Max plan")).toBeTruthy();
  expect(screen.getByText("sk-ant-…WXYZ, standby")).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});

it("warns when the active mode has no credential", async () => {
  worker.state.claude = claudeSummary({
    mode: "api_key",
    api_key: { set: false, last4: null },
    gate: {
      ready: false,
      mode: "api_key",
      claude_credential_ready: false,
      reason: "No Anthropic API key is stored on this worker.",
      license_allows_runs: true,
    },
  });
  worker.state.integrations.anthropic_api_key_set = false;
  await show();
  expect(screen.getByText("Not ready")).toBeTruthy();
  expect(
    screen.getByText("No Anthropic API key is stored on this worker."),
  ).toBeTruthy();
  expect(within(credentials()).getByText("No key stored")).toBeTruthy();
  expect(
    screen.getByRole("button", { name: "Anthropic API key, No key stored" }),
  ).toBeTruthy();
});

it("changes the mode only when the owner picks one", async () => {
  await show();
  expect(worker.sent("PUT", "/settings/claude-auth")).toHaveLength(0);
  await fireEvent.click(
    within(modes()).getByRole("button", { name: "API key" }),
  );
  await waitFor(() =>
    expect(worker.sent("PUT", "/settings/claude-auth")).toHaveLength(1),
  );
  expect(worker.sent("PUT", "/settings/claude-auth")[0].body).toEqual({
    mode: "api_key",
  });
  await waitFor(() =>
    expect(
      within(modes())
        .getByRole("button", { name: "API key" })
        .getAttribute("aria-pressed"),
    ).toBe("true"),
  );
  expect(screen.getByText("Runs on API key")).toBeTruthy();
  expect(within(credentials()).getByText("Active for Claude")).toBeTruthy();
  expect(mocks.hydrate).toHaveBeenCalled();
});

it("turns Claude off and says it is out of the picker", async () => {
  await show();
  await fireEvent.click(within(modes()).getByRole("button", { name: "Off" }));
  await waitFor(() =>
    expect(worker.sent("PUT", "/settings/claude-auth")[0]?.body).toEqual({
      mode: "off",
    }),
  );
  await waitFor(() =>
    expect(screen.getByText("Off, not in the picker")).toBeTruthy(),
  );
  expect(within(credentials()).getAllByText("Off")).toHaveLength(2);
});

it("is read-only for other devices", async () => {
  await show({ readOnly: true });
  for (const b of within(modes()).getAllByRole("button"))
    expect((b as HTMLButtonElement).disabled).toBe(true);
  await fireEvent.click(
    within(modes()).getByRole("button", { name: "API key" }),
  );
  await flush();
  expect(worker.sent("PUT", "/settings/claude-auth")).toHaveLength(0);
});
