#!/usr/bin/env node
/**
 * Diagnostic: list GitHub repositories visible to the authenticated
 * CURSOR_API_KEY via Cursor's platform integration. Helps diagnose the
 * "Failed to determine repository default branch" error (usually means the
 * Cursor GitHub App isn't installed / doesn't have access to the target
 * repo).
 *
 * Usage:
 *   CURSOR_API_KEY=crsr_... npx tsx src/list_repos.ts
 */

import { Cursor } from "@cursor/sdk";

async function main() {
  const me = await Cursor.me();
  console.log("authenticated as:", JSON.stringify(me, null, 2));
  const repos = await Cursor.repositories.list();
  console.log(`\n${repos.length} repository(ies) connected to Cursor:`);
  for (const r of repos) {
    console.log(" -", r.url);
  }
  if (repos.length === 0) {
    console.log(
      "\nNo repos connected. Install the Cursor GitHub App from\n" +
      "  https://cursor.com/dashboard  (or /integrations)\n" +
      "and grant access to AndreChuabio/rehab-protocols-andre."
    );
  }
}

main().catch((err) => {
  console.error("error:", err?.message ?? err);
  process.exit(1);
});
