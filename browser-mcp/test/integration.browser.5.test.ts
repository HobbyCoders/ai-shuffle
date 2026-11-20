// Part 5 of integration.browser.suite.ts; see integration-part.ts.
import { selectPart } from "./integration-part.js";

selectPart(5);
await import("./integration.browser.suite.js");
