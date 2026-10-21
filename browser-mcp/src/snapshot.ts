/**
 * Snapshot engine.
 *
 * We rely on Playwright's public aria-snapshot AI surface:
 *
 *     page.locator(scope).ariaSnapshot({ mode: "ai", boxes: true })
 *
 * (verified against playwright-core 1.61) which emits a YAML-ish tree where
 * interactable, visible, pointer-receiving nodes carry `[ref=eN]` and, with
 * `boxes:true`, `[box=x,y,w,h]`. Everything below is a pure post-processor over
 * that string — filtering, repeated-sibling collapsing, and self-describing
 * truncation — so version churn in Playwright is contained to the one call
 * above. All the transforms are exported and browser-free for unit testing.
 */

export interface SnapNode {
  /** Nesting depth (0 = root). */
  depth: number;
  /** ARIA role token, or "" for bare text / quoted-string nodes. */
  role: string;
  /** Accessible name (contents of the first "…"), or "". */
  name: string;
  /** Bracketed attributes other than ref, e.g. ["level=1", "checked"]. */
  attrs: string[];
  /** Ref id (e.g. "e5") if present. */
  ref?: string;
  /** Everything after the leading "- ", verbatim (used for faithful re-render). */
  raw: string;
  /** true when the source line ended with ":" (has children or a block value). */
  hasColon: boolean;
  children: SnapNode[];
}

export interface SnapshotRenderOptions {
  filter?: "interactive" | "all";
  maxChars?: number;
  collapseThreshold?: number;
  collapseKeep?: number;
}

export interface RenderedSnapshot {
  text: string;
  refs: string[];
  truncated: boolean;
  fullChars: number;
}

export const DEFAULT_MAX_CHARS = 40000;
export const DEFAULT_COLLAPSE_THRESHOLD = 8;
export const DEFAULT_COLLAPSE_KEEP = 5;

/**
 * Roles kept by filter="interactive": actionable controls plus headings and
 * landmarks so the model retains page orientation.
 */
export const INTERACTIVE_ROLES = new Set<string>([
  // actionable controls
  "link",
  "button",
  "textbox",
  "searchbox",
  "combobox",
  "checkbox",
  "radio",
  "tab",
  "menuitem",
  "menuitemcheckbox",
  "menuitemradio",
  "option",
  "slider",
  "spinbutton",
  "switch",
  "listbox",
  // orientation: headings
  "heading",
  // orientation: landmarks
  "banner",
  "navigation",
  "main",
  "complementary",
  "contentinfo",
  "region",
  "search",
  "form",
  "dialog",
  "alertdialog",
]);

const INDENT = "  ";

const LINE_RE = /^(\s*)-\s?(.*)$/;
// Pull a [ref=eN] (or iframe [ref=fMeN]) token out of the content.
const REF_RE = /\[ref=((?:f\d+)?e\d+)\]/;

/**
 * Parse Playwright aria-snapshot (ai mode) YAML into a node tree.
 * Indentation is two spaces per level; list items start with "- ".
 * Continuation lines of block scalars (rare) are attached to the last node's raw.
 */
export function parseAriaYaml(text: string): SnapNode[] {
  const roots: SnapNode[] = [];
  // Stack of { indent, node } for building the tree by indentation.
  const stack: Array<{ indent: number; node: SnapNode }> = [];

  const lines = text.split("\n");
  for (const rawLine of lines) {
    if (rawLine.trim() === "") continue;
    const m = LINE_RE.exec(rawLine);
    if (!m) {
      // Continuation of a block scalar value — append to the deepest open node.
      if (stack.length > 0) {
        stack[stack.length - 1].node.raw += "\n" + rawLine;
      }
      continue;
    }
    const indent = m[1].length;
    const content = m[2];
    const node = parseNodeContent(content, indentToDepth(indent));

    // Pop until we find the parent (strictly smaller indent).
    while (stack.length > 0 && stack[stack.length - 1].indent >= indent) {
      stack.pop();
    }
    if (stack.length === 0) {
      node.depth = 0;
      roots.push(node);
    } else {
      const parent = stack[stack.length - 1].node;
      node.depth = parent.depth + 1;
      parent.children.push(node);
    }
    stack.push({ indent, node });
  }
  return roots;
}

function indentToDepth(indent: number): number {
  return Math.floor(indent / 2);
}

/** Parse the portion of a line after the leading "- ". */
export function parseNodeContent(content: string, depth: number): SnapNode {
  let raw = content;
  let hasColon = false;

  // A trailing ":" (optionally followed by an inline value) marks children/block.
  // We only strip a bare trailing colon; "text: value" keeps its value in raw.
  const node: SnapNode = {
    depth,
    role: "",
    name: "",
    attrs: [],
    raw,
    hasColon,
    children: [],
  };

  // ref
  // Ref-shaped text in an accessible name or inline value is page text,
  // not a Playwright attribute. Never register/count it as a control.
  const attributes = content.replace(/"(?:[^"\\]|\\.)*"/g, '""').split(":", 1)[0];
  const refM = REF_RE.exec(attributes);
  if (refM) node.ref = refM[1];

  // Detect a bare trailing colon (has children).
  if (/:$/.test(content.trimEnd())) {
    hasColon = true;
    node.hasColon = true;
  }

  // Leading role token: letters/dashes before a space, quote, bracket, or colon.
  const roleM = /^([a-zA-Z][a-zA-Z-]*)/.exec(content);
  if (roleM) node.role = roleM[1];

  // Accessible name: first double-quoted string (supports escaped quotes).
  const nameM = /"((?:[^"\\]|\\.)*)"/.exec(content);
  if (nameM) node.name = nameM[1];

  // Bracketed attributes, excluding ref.
  const attrRe = /\[([^\]]+)\]/g;
  let a: RegExpExecArray | null;
  while ((a = attrRe.exec(content)) !== null) {
    if (a[1].startsWith("ref=")) continue;
    node.attrs.push(a[1]);
  }

  return node;
}

/**
 * filter="interactive": retain a node if its role is interactive/orientational
 * OR it has a retained descendant (preserving the path to controls).
 */
export function filterInteractive(roots: SnapNode[]): SnapNode[] {
  const out: SnapNode[] = [];
  for (const node of roots) {
    const kept = filterNode(node);
    if (kept) out.push(kept);
  }
  return out;
}

function filterNode(node: SnapNode): SnapNode | null {
  const keptChildren: SnapNode[] = [];
  for (const child of node.children) {
    const k = filterNode(child);
    if (k) keptChildren.push(k);
  }
  const selfInteresting = node.role !== "" && INTERACTIVE_ROLES.has(node.role);
  if (!selfInteresting && keptChildren.length === 0) return null;
  return { ...node, children: keptChildren };
}

/**
 * Normalize an accessible name into a "shape" so siblings that differ only in
 * their variable content (numbers, punctuation) collapse together:
 *   "Item 12"  -> "item #"
 *   "Item 305" -> "item #"
 */
export function nameShape(name: string): string {
  return name
    .toLowerCase()
    .replace(/\d+/g, "#")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Collapse long runs of similar siblings. When a parent has more than
 * `threshold` consecutive children sharing role + identical non-empty name, keep the first
 * `keep` and replace the rest with a self-describing marker pointing at a ref
 * the caller can scope into.
 */
export function collapseSiblings(
  roots: SnapNode[],
  threshold = DEFAULT_COLLAPSE_THRESHOLD,
  keep = DEFAULT_COLLAPSE_KEEP,
): SnapNode[] {
  return roots.map((n) => collapseNode(n, threshold, keep));
}

function collapseNode(node: SnapNode, threshold: number, keep: number): SnapNode {
  const children = node.children.map((c) => collapseNode(c, threshold, keep));
  const collapsed: SnapNode[] = [];

  let i = 0;
  while (i < children.length) {
    const start = i;
    const sig = siblingSignature(children[i]);
    let j = i + 1;
    while (sig !== undefined && j < children.length && siblingSignature(children[j]) === sig) j++;
    const runLen = j - start;
    if (runLen > threshold) {
      for (let k = start; k < start + keep; k++) collapsed.push(children[k]);
      const hidden = runLen - keep;
      const scopeRef = node.ref ?? firstRef(children[start]);
      const hint = scopeRef ? ` (scope_ref=${scopeRef} to expand)` : "";
      collapsed.push(makeMarker(node.depth + 1, `... and ${hidden} more similar items${hint}`));
    } else {
      for (let k = start; k < j; k++) collapsed.push(children[k]);
    }
    i = j;
  }

  return { ...node, children: collapsed };
}

function siblingSignature(node: SnapNode): string | undefined {
  // Unnamed containers and numbered products are not interchangeable.
  return node.name.trim() ? `${node.role}\0${node.name}` : undefined;
}

function firstRef(node: SnapNode): string | undefined {
  if (node.ref) return node.ref;
  for (const child of node.children) {
    const r = firstRef(child);
    if (r) return r;
  }
  return undefined;
}

function makeMarker(depth: number, textValue: string): SnapNode {
  return {
    depth,
    role: "",
    name: "",
    attrs: [],
    raw: textValue,
    hasColon: false,
    children: [],
  };
}

/** Serialize a node tree back to the YAML-ish "- …" form. */
export function serialize(roots: SnapNode[]): string {
  const lines: string[] = [];
  for (const node of roots) serializeNode(node, 0, lines);
  return lines.join("\n");
}

function serializeNode(node: SnapNode, depth: number, lines: string[]): void {
  const pad = INDENT.repeat(depth);
  // `raw` may contain embedded newlines from block scalars; indent them too.
  const rawLines = node.raw.split("\n");
  lines.push(`${pad}- ${rawLines[0]}`);
  for (let i = 1; i < rawLines.length; i++) lines.push(rawLines[i]);
  for (const child of node.children) serializeNode(child, depth + 1, lines);
}

/** Collect every ref present in a tree, in document order. */
export function collectRefs(roots: SnapNode[]): string[] {
  const refs: string[] = [];
  const walk = (n: SnapNode) => {
    if (n.ref) refs.push(n.ref);
    n.children.forEach(walk);
  };
  roots.forEach(walk);
  return refs;
}

/**
 * Truncate at a line boundary with a self-describing tail. `fullChars` is the
 * untruncated length so the tail can report it.
 */
export function truncateWithTail(text: string, maxChars: number, filter: "interactive" | "all" = "all"): { text: string; truncated: boolean; fullChars: number } {
  const fullChars = text.length;
  if (fullChars <= maxChars) return { text, truncated: false, fullChars };

  const tail = `\n[truncated: full snapshot is ${fullChars} chars; narrow with scope_ref${filter === "interactive" ? "" : " or filter=interactive"}, or raise max_chars]`;
  const budget = Math.max(0, maxChars - tail.length);
  let cut = text.lastIndexOf("\n", budget);
  if (cut <= 0) cut = budget; // no newline in budget: hard cut
  const head = text.slice(0, cut).replace(/\n+$/, "");
  return { text: head + tail, truncated: true, fullChars };
}

/**
 * Full post-processing pipeline over a raw aria-snapshot string.
 * Pure: no browser needed.
 */
export function renderSnapshot(rawYaml: string, options: SnapshotRenderOptions = {}): RenderedSnapshot {
  const {
    filter = "all",
    maxChars = DEFAULT_MAX_CHARS,
    collapseThreshold = DEFAULT_COLLAPSE_THRESHOLD,
    collapseKeep = DEFAULT_COLLAPSE_KEEP,
  } = options;

  let tree = parseAriaYaml(rawYaml);
  if (filter === "interactive") tree = filterInteractive(tree);
  // Register refs before collapsing or truncating the output.
  const refs = collectRefs(tree);
  tree = collapseSiblings(tree, collapseThreshold, collapseKeep);

  const serialized = serialize(tree);
  const { text, truncated, fullChars } = truncateWithTail(serialized, maxChars, filter);
  return { text, refs, truncated, fullChars };
}

// ---------------------------------------------------------------------------
// Secret masking
// ---------------------------------------------------------------------------

/** Replaces a password field's value in a snapshot line. */
export const MASKED_VALUE = "••••••";

/** Playwright's YAML double-quoted form of a value (mirrors yamlEscapeValueIfNeeded). */
function yamlQuoted(value: string): string {
  const escaped = value.replace(/[\\"\x00-\x1f\x7f-\x9f]/g, (c) => {
    switch (c) {
      case "\\": return "\\\\";
      case '"': return '\\"';
      case "\b": return "\\b";
      case "\f": return "\\f";
      case "\n": return "\\n";
      case "\r": return "\\r";
      case "\t": return "\\t";
      default: return "\\x" + c.charCodeAt(0).toString(16).padStart(2, "0");
    }
  });
  return `"${escaped}"`;
}

/**
 * Mask password-field values in a raw aria snapshot.
 *
 * Playwright renders an `<input type=password>` as an ordinary `textbox` whose
 * value is the plain-text password, so an autofilled or typed password would
 * otherwise reach the model (and the browser_find sub-model) in every snapshot.
 * `secrets` are the current values of the page's password inputs; any textbox
 * line whose value is one of them is masked. Pure; the browser read lives in
 * the session.
 */
export function maskSecretValues(yaml: string, secrets: readonly string[]): string {
  const tails = new Set<string>();
  for (const secret of secrets) {
    if (typeof secret !== "string" || secret === "") continue;
    tails.add(secret);
    tails.add(yamlQuoted(secret));
  }
  if (tails.size === 0) return yaml;
  return yaml
    .split("\n")
    .map((line) => {
      if (!/^\s*- textbox\b/.test(line)) return line;
      for (const tail of tails) {
        if (line.endsWith(`: ${tail}`)) return line.slice(0, line.length - tail.length) + MASKED_VALUE;
      }
      return line;
    })
    .join("\n");
}
