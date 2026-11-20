// Part 2 of integration.browser.suite.ts; see integration-part.ts.
import { selectPart } from "./integration-part.js";

selectPart(2);
await import("./integration.browser.suite.js");
