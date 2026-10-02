/**
 * Test-only fake of the worker's `/api/v1` routes for the Providers tests.
 * Install it with `vi.stubGlobal('fetch', worker.fetch)`; every request is
 * recorded in `calls` so tests can assert the exact method, path and body.
 */
import type { ModelProvider } from "$lib/api/model-providers";
import type { ClaudeAuthMode, ClaudeAuthSummary } from "$lib/api/claude-auth";
import type { ClaudeCliStatus } from "$lib/api/claude-cli";
import type { LoopGuardStatus } from "$lib/api/loop-guard";
import { claudeSummary } from "./fixtures";

export interface WorkerCall {
  method: string;
  /** Path under `/api/v1`, with its query. */
  path: string;
  body: unknown;
}

export type RouteHandler = (body: unknown, path: string) => unknown;

export interface FakeWorkerState {
  rows: ModelProvider[];
  claude: ClaudeAuthSummary;
  integrations: {
    anthropic_api_key_set: boolean;
    anthropic_api_key_masked: string | null;
    fal_api_key_set: boolean;
    fal_api_key_masked: string;
    fal_enabled: boolean;
  };
  loopGuard: LoopGuardStatus;
  cli: ClaudeCliStatus;
}

export const LOOP_DEFAULTS = {
  enabled: true,
  session_turns_per_hour: 12,
  install_turns_per_hour: 60,
  worker_max_minutes: 60,
};

export function loopGuardStatus(
  partial: Partial<LoopGuardStatus> = {},
): LoopGuardStatus {
  return {
    ...LOOP_DEFAULTS,
    defaults: LOOP_DEFAULTS,
    applies: true,
    unattended_sources: ["automation", "wakeup", "provider_agent", "connector"],
    usage: { install_last_hour: 3 },
    held: [],
    ...partial,
  } as LoopGuardStatus;
}

export function cliStatus(
  partial: Partial<ClaudeCliStatus> = {},
): ClaudeCliStatus {
  return {
    installed: true,
    signed_in: true,
    auth_method: "claude.ai",
    auth_method_label: "Claude account",
    email: "owner@example.com",
    org_name: "HobbyCoders",
    plan: "max",
    api_provider: null,
    cli_version: "2.1.285",
    binary_path: "/home/appuser/.local/bin/claude",
    error: null,
    checked_at: 0,
    login: {
      surface: "docker",
      terminal: true,
      command: "claude auth login",
      logout_command: "claude auth logout",
      status_command: "claude auth status",
      docker_command:
        "docker exec -it -u appuser <container> claude auth login",
    },
    ...partial,
  };
}

const json = (status: number, body: unknown) =>
  new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

/**
 * A worker with Claude on the subscription (CLI signed in, Anthropic key
 * stored), the given model_providers rows and a fal.ai key. `routes` add or
 * override handlers by `"METHOD /path"` (path without query).
 */
export function fakeWorker(
  init: Partial<FakeWorkerState> = {},
  routes: Record<string, RouteHandler> = {},
) {
  const state: FakeWorkerState = {
    rows: [],
    claude: claudeSummary(),
    integrations: {
      anthropic_api_key_set: true,
      anthropic_api_key_masked: "sk-ant-…4f2a",
      fal_api_key_set: true,
      fal_api_key_masked: "fal-…9c1e",
      fal_enabled: true,
    },
    loopGuard: loopGuardStatus(),
    cli: cliStatus(),
    ...init,
  };
  const calls: WorkerCall[] = [];

  const defaults: Record<string, RouteHandler> = {
    "GET /model-providers": () => ({ providers: state.rows }),
    "PATCH /model-providers/:id": (body, path) => {
      const id = decodeURIComponent(path.split("/")[2]);
      state.rows = state.rows.map((r) =>
        r.id === id ? { ...r, ...(body as object) } : r,
      );
      return state.rows.find((r) => r.id === id);
    },
    "GET /settings/integrations": () => state.integrations,
    "PUT /settings/integrations/fal-ai/enabled": (body) => {
      state.integrations.fal_enabled = (body as { enabled: boolean }).enabled;
      return {
        fal_enabled: state.integrations.fal_enabled,
        fal_api_key_set: state.integrations.fal_api_key_set,
      };
    },
    "GET /settings/claude-auth": () => state.claude,
    "PUT /settings/claude-auth": (body) => {
      const mode = (body as { mode: ClaudeAuthMode }).mode;
      state.claude = claudeSummary({
        ...state.claude,
        mode,
        gate: claudeSummary({ mode }).gate,
      });
      return state.claude;
    },
    "GET /settings/loop-guard": () => state.loopGuard,
    "PUT /settings/loop-guard": (body) => {
      state.loopGuard = { ...state.loopGuard, ...(body as object) };
      return state.loopGuard;
    },
    "GET /auth/claude/cli": () => state.cli,
    "GET /auth/claude/cli-login": () => ({
      supported: true,
      active: false,
      running: false,
      exit_code: null,
      succeeded: null,
      output: "",
      offset: 0,
      reset: false,
      url: null,
      error: null,
    }),
    "GET /usage/limits": () => ({ providers: [] }),
  };
  const table = { ...defaults, ...routes };

  function match(method: string, path: string): RouteHandler | undefined {
    const exact = table[`${method} ${path}`];
    if (exact) return exact;
    const parts = path.split("/");
    for (const [key, handler] of Object.entries(table)) {
      const [m, pattern] = key.split(" ");
      if (m !== method) continue;
      const p = pattern.split("/");
      if (
        p.length === parts.length &&
        p.every((seg, i) => seg.startsWith(":") || seg === parts[i])
      )
        return handler;
    }
    return undefined;
  }

  async function fetchImpl(
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> {
    const url = new URL(
      String(input instanceof Request ? input.url : input),
      "http://localhost",
    );
    const method = (init?.method ?? "GET").toUpperCase();
    const full = url.pathname.replace(/^\/api\/v1/, "") + url.search;
    const body =
      typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    calls.push({ method, path: full, body });
    const handler = match(method, url.pathname.replace(/^\/api\/v1/, ""));
    if (!handler)
      return json(404, { detail: `No route for ${method} ${full}` });
    try {
      return json(
        200,
        await handler(body, url.pathname.replace(/^\/api\/v1/, "")),
      );
    } catch (e) {
      const err = e as { status?: number; detail?: string; message?: string };
      return json(err.status ?? 500, {
        detail: err.detail ?? err.message ?? "Failed",
      });
    }
  }

  return {
    state,
    calls,
    fetch: fetchImpl,
    /** Calls with this method and this path (query ignored). */
    sent(method: string, path: string): WorkerCall[] {
      return calls.filter(
        (c) => c.method === method && c.path.split("?")[0] === path,
      );
    },
  };
}
