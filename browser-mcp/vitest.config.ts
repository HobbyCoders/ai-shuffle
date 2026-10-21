import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
    // Chromium shares the runner with Vitest. Unbounded file workers compete
    // with its renderer and turn the observation windows into scheduler tests.
    maxWorkers: 2,
    // integration.browser drives a real Chromium over CDP; its default-timeout
    // tests (upload, find, tabs) exceed 5 s on a loaded 2-vCPU runner.
    testTimeout: 20_000,
  },
});
