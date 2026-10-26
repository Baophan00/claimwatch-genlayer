#!/usr/bin/env node
/**
 * End to end run of ClaimWatch on GenLayer Studio Network.
 *
 *   node scripts/e2e_breach.js open <contract> <url>
 *   node scripts/e2e_breach.js cure <contract> <url> <watchId>
 *
 * The claim carries three requirements. The page names the project and the proof
 * path, and it does not promise anything about an audit, so the first check must
 * contradict the claim, name requirement 3 as unmet, and breach the watch.
 *
 * Between the two halves the page is edited to add the missing promise, which is
 * the only way to show the cure branch honestly: a supported check inside the
 * seven day window has to clear a breach that the same URL caused.
 */
const fs = require("fs");
const path = require("path");
const { createClient, createAccount, generatePrivateKey } = require("genlayer-js");
const { studionet } = require("genlayer-js/chains");

const [mode, ADDRESS, URL_ARG, WATCH_ARG] = process.argv.slice(2);
if (!["open", "cure"].includes(mode) || !ADDRESS || !URL_ARG) {
  console.error("usage: node scripts/e2e_breach.js <open|cure> <contract> <url> [watchId]");
  process.exit(1);
}

const KEYFILE = path.join(__dirname, ".e2e-keys.json");
function keys() {
  const file = fs.existsSync(KEYFILE) ? JSON.parse(fs.readFileSync(KEYFILE, "utf8")) : {};
  let changed = false;
  for (const role of ["owner", "watcher"]) {
    if (!file[role]) {
      file[role] = generatePrivateKey();
      changed = true;
    }
  }
  if (changed) fs.writeFileSync(KEYFILE, JSON.stringify(file, null, 2), { mode: 0o600 });
  return file;
}
const k = keys();
const ownerAccount = createAccount(k.owner);
const watcherAccount = createAccount(k.watcher);
const owner = createClient({ chain: studionet, account: ownerAccount });
const watcher = createClient({ chain: studionet, account: watcherAccount });
const reader = createClient({ chain: studionet, account: createAccount(generatePrivateKey()) });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function withRetry(fn, label) {
  let last;
  for (let i = 0; i < 8; i++) {
    try {
      return await fn();
    } catch (e) {
      last = e;
      const msg = String(e && e.message);
      if (!/DOCTYPE|fetch failed|ECONNRESET|timeout|temporarily unavailable|not available/i.test(msg)) throw e;
      console.log(`  ${label}: rpc hiccup, retry ${i + 1}`);
      await sleep(4000);
    }
  }
  throw last;
}

function refused(receipt) {
  const leader = (receipt.consensus_data && receipt.consensus_data.leader_receipt) || [];
  return String(leader[0] && leader[0].execution_result).toUpperCase() === "ERROR";
}

async function write(client, who, functionName, args) {
  console.log(`\n[${who}] ${functionName}(${args.map((a) => JSON.stringify(a).slice(0, 70)).join(", ")})`);
  const hash = await client.writeContract({ address: ADDRESS, functionName, args, value: 0n });
  console.log(`  tx ${hash}`);
  const receipt = await withRetry(
    () => client.waitForTransactionReceipt({ hash, status: "FINALIZED", retries: 240, interval: 3000 }),
    "receipt"
  );
  const bad = refused(receipt);
  console.log(`  ${receipt.status_name || receipt.status}  (${bad ? "refused by the contract" : "executed"})`);
  if (bad) throw new Error(`${functionName} failed inside the contract`);
  return hash;
}

const read = (functionName, args) =>
  withRetry(() => reader.readContract({ address: ADDRESS, functionName, args }), "read");

const CLAIM = [
  "The page names DomainAttest.",
  "The page shows the well-known proof path.",
  "The page promises a free audit within 24 hours.",
].join("\n");

(async () => {
  console.log("contract", ADDRESS);
  console.log("owner   ", ownerAccount.address);
  console.log("watcher ", watcherAccount.address);

  if (mode === "open") {
    const watchId = WATCH_ARG || `audit-${Date.now().toString(36)}`;
    await write(owner, "owner", "create_watch", [watchId, URL_ARG, CLAIM]);
    const created = await read("get_watch", [watchId]);
    console.log(`  watch ${watchId} with ${created.requirements.length} requirements, state ${created.state}`);

    console.log("\n[validators] check the page against the three requirements");
    const t0 = Date.now();
    await write(watcher, "watcher", "run_check", [watchId]);
    console.log(`  round took ${Math.round((Date.now() - t0) / 1000)}s`);

    const after = await read("get_watch", [watchId]);
    console.log("  verdict:", after.last_verdict, "| unmet:", JSON.stringify(after.unmet), "| material:", after.material);
    console.log("  state:", after.state, "| cure_deadline:", after.cure_deadline);
    console.log("  is_breached:", await read("is_breached", [URL_ARG]));
    if (after.state !== "breached") throw new Error(`expected breached, got ${after.state}`);
    if (!after.material) throw new Error("expected a material loss");
    if (!after.unmet.includes(3)) throw new Error(`expected requirement 3 unmet, got ${JSON.stringify(after.unmet)}`);
    console.log("\nnext: add the promised audit line to the page, then run the cure half with watch id", watchId);
    console.log(JSON.stringify(await read("breaches", [0, 5]), null, 2).slice(0, 600));
    return;
  }

  const watchId = WATCH_ARG;
  if (!watchId) {
    console.error("cure needs the watch id");
    process.exit(1);
  }
  console.log("\n[validators] check the edited page against the same three requirements");
  const t1 = Date.now();
  await write(watcher, "watcher", "run_check", [watchId]);
  console.log(`  round took ${Math.round((Date.now() - t1) / 1000)}s`);

  const after = await read("get_watch", [watchId]);
  console.log("  verdict:", after.last_verdict, "| unmet:", JSON.stringify(after.unmet), "| state:", after.state);
  console.log("  breaches recorded:", after.breaches, "| is_breached:", await read("is_breached", [URL_ARG]));
  if (after.state !== "cured") throw new Error(`expected cured, got ${after.state}`);

  const history = await read("checks_of", [watchId]);
  console.log("\n  history:");
  for (const h of history) {
    console.log(`   #${h.seq} ${h.verdict} unmet=${JSON.stringify(h.unmet)} material=${h.material}`);
  }
  console.log("\nstats");
  console.log(JSON.stringify(await read("stats", []), null, 2));
  console.log("\nRESULT: a material loss breached the watch, the same URL cured it once the page carried the promise, and the breach stays in the record");
})().catch((e) => {
  console.error("\nFAILED:", e.message);
  if (e.cause) console.error(String(e.cause).slice(0, 900));
  process.exit(1);
});
