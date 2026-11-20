// Part 1 of integration.browser.suite.ts; see integration-part.ts.
import { selectPart } from "./integration-part.js";

selectPart(1);
await import("./integration.browser.suite.js");
