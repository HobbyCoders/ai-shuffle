/**
 * Tool input schemas.
 *
 * SCHEMA FLOOR (hard requirement for OpenAI strict mode + Gemini compat, so the
 * translator gateway never has to get clever): flat objects only — property
 * types restricted to string / number / boolean / enum / array-of-primitives.
 * NO anyOf / oneOf / allOf / $ref / nested object properties.
 *
 * We hand-author raw JSON Schema (rather than deriving it from zod) precisely so
 * none of those banned constructs can leak in via a code generator. A tiny
 * built-in validator enforces the schemas at call time.
 */

export type JsonSchema = {
  type: "object";
  properties: Record<string, PropSchema>;
  required?: string[];
  additionalProperties: false;
};

export type PropSchema = {
  type: "string" | "number" | "integer" | "boolean" | "array";
  description?: string;
  enum?: string[];
  items?: { type: "string" | "number" | "integer" | "boolean" };
  minimum?: number;
  maximum?: number;
};

export const ACT_KINDS = [
  "click",
  "dblclick",
  "type",
  "fill",
  "key",
  "hover",
  "scroll",
  "select",
  "drag",
] as const;

export const navigateSchema: JsonSchema = {
  type: "object",
  properties: {
    url: {
      type: "string",
      description: 'URL to open, or "back" / "forward" to move through history. Bare domains get https://.',
    },
  },
  required: ["url"],
  additionalProperties: false,
};

export const snapshotSchema: JsonSchema = {
  type: "object",
  properties: {
    filter: {
      type: "string",
      enum: ["interactive", "all"],
      description: 'interactive = controls + headings/landmarks (default all).',
    },
    scope_ref: {
      type: "string",
      description: "Re-snapshot only the subtree under this ref (e.g. e12).",
    },
    max_chars: {
      type: "integer",
      description: "Truncation budget (default 40000).",
      minimum: 500,
    },
  },
  additionalProperties: false,
};

export const actSchema: JsonSchema = {
  type: "object",
  properties: {
    kind: {
      type: "string",
      enum: [...ACT_KINDS],
      description: "Action to perform.",
    },
    ref: {
      type: "string",
      description: "Target element ref (e.g. e12). Provide exactly one of ref or coordinate.",
    },
    coordinate: {
      type: "array",
      items: { type: "number" },
      description: "Target [x,y] in the last screenshot's pixel space. Exactly one of ref or coordinate.",
    },
    value: {
      type: "string",
      description: "Text for type/fill, or option value for select.",
    },
    key: {
      type: "string",
      description: 'Key name for kind=key (e.g. "Enter", "ArrowDown").',
    },
    modifiers: {
      type: "array",
      items: { type: "string" },
      description: 'Modifier keys for kind=key (e.g. ["Control","Shift"]).',
    },
    direction: {
      type: "string",
      enum: ["up", "down", "left", "right"],
      description: "Scroll direction for kind=scroll.",
    },
    amount: {
      type: "integer",
      description: "Scroll distance in pixels for kind=scroll.",
    },
    to_coordinate: {
      type: "array",
      items: { type: "number" },
      description: "Drop point [x,y] for kind=drag.",
    },
  },
  required: ["kind"],
  additionalProperties: false,
};

export const screenshotSchema: JsonSchema = {
  type: "object",
  properties: {
    region: {
      type: "array",
      items: { type: "number" },
      description: "Crop [x,y,width,height] in page pixels. Omit for the viewport.",
    },
    full_page: {
      type: "boolean",
      description: "Capture the full scrollable page instead of the viewport.",
    },
  },
  additionalProperties: false,
};

export const findSchema: JsonSchema = {
  type: "object",
  properties: {
    query: {
      type: "string",
      description:
        "What to find, in plain words (e.g. \"the checkout button\", \"email field\").",
    },
  },
  required: ["query"],
  additionalProperties: false,
};

export const readTextSchema: JsonSchema = {
  type: "object",
  properties: {},
  additionalProperties: false,
};

export const evalSchema: JsonSchema = {
  type: "object",
  properties: {
    code: {
      type: "string",
      description:
        "JavaScript run in the page. REPL semantics: top-level await allowed. A single bare expression returns its value automatically; multi-statement code must `return` the value explicitly.",
    },
  },
  required: ["code"],
  additionalProperties: false,
};

export const consoleSchema: JsonSchema = {
  type: "object",
  properties: {
    pattern: {
      type: "string",
      description:
        "REQUIRED substring or regex; only matching console messages are returned.",
    },
    only_errors: {
      type: "boolean",
      description: "Keep only level=error messages.",
    },
    limit: {
      type: "integer",
      description: "Max messages to return, newest last (default 20).",
      minimum: 1,
    },
    clear: {
      type: "boolean",
      description: "Empty the console buffer for this page after returning.",
    },
  },
  required: ["pattern"],
  additionalProperties: false,
};

export const networkSchema: JsonSchema = {
  type: "object",
  properties: {
    url_pattern: {
      type: "string",
      description: "Substring or regex to filter the request list by URL.",
    },
    request_id: {
      type: "string",
      description: "Return this recorded request's response body instead of a list.",
    },
    limit: {
      type: "integer",
      description: "Max requests to list, newest last (default 20).",
      minimum: 1,
    },
    clear: {
      type: "boolean",
      description: "Empty the network buffer for this page after returning.",
    },
  },
  additionalProperties: false,
};

export const TABS_OPS = ["list", "create", "close", "select"] as const;

export const tabsSchema: JsonSchema = {
  type: "object",
  properties: {
    op: {
      type: "string",
      enum: [...TABS_OPS],
      description: "list | create | close | select.",
    },
    tab_id: {
      type: "string",
      description: "Target tab (e.g. tab2) for op=close or op=select.",
    },
  },
  required: ["op"],
  additionalProperties: false,
};

export const RESIZE_PRESETS = ["mobile", "tablet", "desktop"] as const;

export const resizeSchema: JsonSchema = {
  type: "object",
  properties: {
    preset: {
      type: "string",
      enum: [...RESIZE_PRESETS],
      description: "mobile 375x812 | tablet 768x1024 | desktop 1280x800.",
    },
    width: {
      type: "integer",
      description: "Explicit viewport width (with height; overrides preset).",
      minimum: 1,
      maximum: 4096,
    },
    height: {
      type: "integer",
      description: "Explicit viewport height (with width; overrides preset).",
      minimum: 1,
      maximum: 4096,
    },
  },
  additionalProperties: false,
};

export const uploadSchema: JsonSchema = {
  type: "object",
  properties: {
    ref: {
      type: "string",
      description: "Ref of the file <input> to populate (e.g. e12).",
    },
    paths: {
      type: "array",
      items: { type: "string" },
      description:
        "Absolute file paths to upload. Only files under BROWSER_UPLOAD_DIR are allowed.",
    },
  },
  required: ["ref", "paths"],
  additionalProperties: false,
};

export const batchSchema: JsonSchema = {
  type: "object",
  properties: {
    actions: {
      type: "array",
      items: { type: "string" },
      description:
        'Each item is a JSON string {"tool":"browser_...","input":{...}}, run in order, stop on first error. Cannot contain browser_batch.',
    },
  },
  required: ["actions"],
  additionalProperties: false,
};

export const TOOL_SCHEMAS = {
  browser_navigate: navigateSchema,
  browser_snapshot: snapshotSchema,
  browser_act: actSchema,
  browser_screenshot: screenshotSchema,
  browser_find: findSchema,
  browser_read_text: readTextSchema,
  browser_eval: evalSchema,
  browser_console: consoleSchema,
  browser_network: networkSchema,
  browser_tabs: tabsSchema,
  browser_resize: resizeSchema,
  browser_upload: uploadSchema,
  browser_batch: batchSchema,
} as const;

export interface ValidationResult {
  ok: boolean;
  error?: string;
}

/**
 * Translated Responses tool schemas are strict and require every property, so
 * callers fill optional strings and arrays with empty placeholders. Remove only
 * those placeholders before validation and dispatch. `value` is exempt because
 * an empty value intentionally clears an input in browser_act.
 */
export function normalizeOptionalInput(
  schema: JsonSchema,
  input: Record<string, unknown>,
): Record<string, unknown> {
  const required = new Set(schema.required ?? []);
  const normalized: Record<string, unknown> = {};

  for (const [key, value] of Object.entries(input)) {
    const isOptional = key in schema.properties && !required.has(key);
    const isEmptyPlaceholder =
      (value === "" && key !== "value") || (Array.isArray(value) && value.length === 0);
    if (isOptional && isEmptyPlaceholder) continue;
    normalized[key] = value;
  }

  return normalized;
}

/**
 * Minimal validator for the flat schema subset above. Enforces: unknown keys,
 * required keys, primitive types, enum membership, array item primitives, and
 * numeric bounds. Intentionally does not implement JSON Schema at large.
 */
export function validateInput(schema: JsonSchema, input: unknown): ValidationResult {
  if (typeof input !== "object" || input === null || Array.isArray(input)) {
    return { ok: false, error: "input must be an object" };
  }
  const obj = input as Record<string, unknown>;

  for (const key of Object.keys(obj)) {
    if (!(key in schema.properties)) {
      return { ok: false, error: `unknown property: ${key}` };
    }
  }
  for (const req of schema.required ?? []) {
    if (!(req in obj) || obj[req] === undefined) {
      return { ok: false, error: `missing required property: ${req}` };
    }
  }
  for (const [key, prop] of Object.entries(schema.properties)) {
    if (!(key in obj) || obj[key] === undefined) continue;
    const v = obj[key];
    const err = validateProp(key, prop, v);
    if (err) return { ok: false, error: err };
  }
  return { ok: true };
}

function validateProp(key: string, prop: PropSchema, v: unknown): string | null {
  switch (prop.type) {
    case "string":
      if (typeof v !== "string") return `${key} must be a string`;
      if (prop.enum && !prop.enum.includes(v)) {
        return `${key} must be one of: ${prop.enum.join(", ")}`;
      }
      return null;
    case "number":
    case "integer":
      if (typeof v !== "number" || Number.isNaN(v)) return `${key} must be a number`;
      if (prop.type === "integer" && !Number.isInteger(v)) return `${key} must be an integer`;
      if (prop.minimum !== undefined && v < prop.minimum) return `${key} must be >= ${prop.minimum}`;
      if (prop.maximum !== undefined && v > prop.maximum) return `${key} must be <= ${prop.maximum}`;
      return null;
    case "boolean":
      if (typeof v !== "boolean") return `${key} must be a boolean`;
      return null;
    case "array":
      if (!Array.isArray(v)) return `${key} must be an array`;
      if (prop.items) {
        for (const el of v) {
          if (prop.items.type === "number" || prop.items.type === "integer") {
            if (typeof el !== "number" || !Number.isFinite(el)) {
              return `${key} items must be finite numbers`;
            }
          } else if (typeof el !== prop.items.type) {
            return `${key} items must be ${prop.items.type}`;
          }
        }
      }
      return null;
    default:
      return `${key} has unsupported type`;
  }
}
