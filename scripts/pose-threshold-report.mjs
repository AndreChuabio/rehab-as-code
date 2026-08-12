// scripts/pose-threshold-report.mjs
//
// Diff pose.js hardcoded angle targets against every prescribed
// ROM_target_deg in the protocol library. Informational only - changes no
// behavior. YAML is line-scanned (entries are "- name: x" blocks with an
// optional ROM_target_deg line) to avoid a YAML dependency.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const pose = readFileSync("frontend/pose.js", "utf8");
const block = pose.slice(pose.indexOf("const EXERCISES"), pose.indexOf("};", pose.indexOf("const EXERCISES")));
const targets = {};
for (const m of block.matchAll(/(\w+):\s*\{\s*primary:\s*"[^"]+",\s*target:\s*(\d+|null),\s*mode:\s*"(\w+)"/g)) {
  targets[m[1]] = { target: m[2] === "null" ? null : Number(m[2]), mode: m[3] };
}

const protocolROMs = {};
const walk = (dir) => {
  for (const f of readdirSync(dir)) {
    const p = join(dir, f);
    if (statSync(p).isDirectory()) { walk(p); continue; }
    if (!f.endsWith(".yaml")) continue;
    const lines = readFileSync(p, "utf8").split("\n");
    let current = null;
    for (const line of lines) {
      const name = line.match(/^\s*-\s*name:\s*(\S+)/);
      if (name) current = name[1];
      const rom = line.match(/^\s*ROM_target_deg:\s*(\d+)/);
      if (rom && current) (protocolROMs[current] ??= []).push({ file: p, deg: Number(rom[1]) });
    }
  }
};
walk("protocols/protocol-library");

const rows = [];
for (const [ex, { target, mode }] of Object.entries(targets)) {
  const roms = protocolROMs[ex];
  if (!roms) { rows.push([ex, mode, target, "-", "no-protocol-entry"]); continue; }
  const degs = roms.map((r) => r.deg);
  const conflict = target != null && !degs.includes(target);
  rows.push([ex, mode, target, degs.join("/"), conflict ? "CONFLICT" : "consistent"]);
}
const pad = (s, n) => String(s ?? "-").padEnd(n);
console.log(pad("exercise", 32) + pad("mode", 16) + pad("pose", 6) + pad("protocol ROMs", 16) + "verdict");
for (const r of rows) console.log(pad(r[0], 32) + pad(r[1], 16) + pad(r[2], 6) + pad(r[3], 16) + r[4]);
