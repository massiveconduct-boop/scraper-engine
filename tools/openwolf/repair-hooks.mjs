// tools/openwolf/repair-hooks.mjs
//
// Re-applies this repo's patches to OpenWolf's hook scripts. Registered as a
// SessionStart hook in .claude/settings.json (outside .wolf/hooks/, so
// `openwolf update` leaves the registration alone) because `openwolf update`
// and `openwolf init` overwrite .wolf/hooks/ with the unpatched copies from
// the global npm package — which is how the stop-hook reminders kept coming
// back after every fix.
//
// Patched copies live in tools/openwolf/hooks/. A hook file is replaced only
// when it is byte-identical to the upstream release those patches were made
// against (UPSTREAM below). Any other content means OpenWolf itself changed:
// the file is left alone and a warning says the patch needs porting, so a
// newer OpenWolf is never silently downgraded. Always exits 0 — a repair
// problem must not block the session.
//
// Patches (details in .wolf/cerebrum.md Do-Not-Repeat, 2026-09-22):
//   stop.js   — writes the end-of-turn memory.md summary itself instead of
//               asking for it every turn; buglog reminder also accepts the
//               file's mtime.
//   shared.js — countSemanticEntries also counts "| HH:MM |" rows.
//
// Run by hand: node tools/openwolf/repair-hooks.mjs [--check]

import { createHash } from "node:crypto";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

// sha256 of the unpatched files shipped in openwolf 2.0.1 (dist/src/hooks/).
const UPSTREAM = {
    "stop.js": "1837f5a1630eae16a748c1349c1c46b8788647beda78cf55495618ffb5dac96e",
    "shared.js": "57c31a5d26d29f6bc1b6a860a73f3f4b8655216489f0748f8ae877cf8ace10f8",
};

const here = path.dirname(fileURLToPath(import.meta.url));
const projectDir = process.env.CLAUDE_PROJECT_DIR || path.resolve(here, "..", "..");
const liveDir = path.join(projectDir, ".wolf", "hooks");
const patchedDir = path.join(here, "hooks");
const checkOnly = process.argv.includes("--check");

const sha256 = (file) => createHash("sha256").update(fs.readFileSync(file)).digest("hex");

let problems = 0;
for (const [name, upstreamHash] of Object.entries(UPSTREAM)) {
    const live = path.join(liveDir, name);
    const patched = path.join(patchedDir, name);
    try {
        if (!fs.existsSync(live)) continue; // OpenWolf not initialised here
        const liveHash = sha256(live);
        if (liveHash === sha256(patched)) continue;
        if (liveHash === upstreamHash) {
            if (checkOnly) {
                console.error(`openwolf-repair: .wolf/hooks/${name} is unpatched`);
                problems++;
            } else {
                fs.copyFileSync(patched, live);
                console.error(`openwolf-repair: re-applied patch to .wolf/hooks/${name}`);
            }
            continue;
        }
        console.error(
            `openwolf-repair: .wolf/hooks/${name} matches neither openwolf 2.0.1 nor the ` +
                `patched copy (OpenWolf was upgraded?). Not touched — port the patch in ` +
                `tools/openwolf/hooks/${name} and update UPSTREAM in tools/openwolf/repair-hooks.mjs.`,
        );
        problems++;
    } catch (err) {
        console.error(`openwolf-repair: ${name}: ${err.message}`);
        problems++;
    }
}
process.exit(checkOnly && problems ? 1 : 0);
