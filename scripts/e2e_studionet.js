#!/usr/bin/env node
/**
 * End to end run of ClaimWatch on GenLayer Studio Network.
 *
 *   node scripts/e2e_studionet.js <contractAddress>
 *
 * Nothing is mocked: each check is a real consensus round where the validators
 * fetch the page themselves. The run creates one watch on a claim the live docs
 * do support and one on a claim they do not, checks both, then checks the first
 * one again so the history on chain has two entries.
 */
const fs = require("fs");
const path = require("path");
const { createClient, createAccount, generatePrivateKey } = require("genlayer-js");
const { studionet } = require("genlayer-js/chains");

const ADDRESS = process.argv[2];
if (!ADDRESS) {
  console.error("usage: node scripts/e2e_studionet.js <contractAddress>");
  process.exit(1);
}

const KEYFILE = path.join(__dirname, ".e2e-keys.json");
function keys() {
  if (fs.existsSync(KEYFILE)) return JSON.parse(fs.readFileSync(KEYFILE, "utf8"));
  const k = { buyer: generatePrivateKey(), seller: generatePrivateKey() };
  fs.writeFileSync(KEYFILE, JSON.stringify(k, null, 2), { mode: 0o600 });
  return k;
}
const k = keys();
const me = createClient({ chain: studionet, account: createAccount(k.buyer) });
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

async function write(functionName, args) {
  console.log(`\n[caller] ${functionName}(${args.map((a) => JSON.stringify(a).slice(0, 90)).join(", ")})`);
  const hash = await me.writeContract({ address: ADDRESS, functionName, args, value: 0n });
  console.log(`  tx ${hash}`);
  const receipt = await withRetry(
    () =>
      me.waitForTransactionReceipt({ hash, status: "FINALIZED", retries: 160, interval: 3000 }),
    "receipt"
  );
  const status = receipt.status_name || receipt.status;
  console.log(`  ${status}  (execution ${receipt.result_name || receipt.result || "?"})`);
  const trace = JSON.stringify(receipt.genvm_result || receipt.last_round || {});
  if (trace.includes('"ERROR"') || /Traceback|Error:/.test(trace)) {
    console.log("  contract execution FAILED:", trace.slice(0, 700));
    throw new Error(`${functionName} failed inside the contract`);
  }
  return receipt;
}

async function read(functionName, args) {
  return withRetry(() => reader.readContract({ address: ADDRESS, functionName, args }), "read");
}

const sleep_s = async (s) => sleep(s * 1000);

(async () => {
  console.log("contract", ADDRESS);
  console.log("caller  ", createAccount(k.buyer).address);
  const stamp = Date.now();
  const supportedId = `w-${stamp}-true`;
  const contradictedId = `w-${stamp}-false`;

  const url = "https://docs.genlayer.com";
  await write("create_watch", [
    supportedId,
    url,
    "GenLayer publishes its developer documentation at docs.genlayer.com.",
  ]);
  await write("create_watch", [
    contradictedId,
    url,
    "The GenLayer docs state that the network runs without validators.",
  ]);

  console.log("\n[validators] check the claim the page does support");
  const t0 = Date.now();
  await write("run_check", [supportedId]);
  console.log(`  round took ${Math.round((Date.now() - t0) / 1000)}s`);
  const watchA = await read("get_watch", [supportedId]);
  const firstChecks = await read("checks_of", [supportedId]);
  console.log("  verdict:", watchA.last_verdict);
  console.log("  quote  :", (firstChecks[0] && firstChecks[0].quote) || "");
  if (watchA.last_verdict !== "supported") {
    throw new Error(`expected supported for the docs claim, got ${watchA.last_verdict}`);
  }

  console.log("\n[validators] check the claim the page contradicts");
  const t1 = Date.now();
  await write("run_check", [contradictedId]);
  console.log(`  round took ${Math.round((Date.now() - t1) / 1000)}s`);
  const watchB = await read("get_watch", [contradictedId]);
  console.log("  verdict:", watchB.last_verdict);
  if (watchB.last_verdict === "supported") {
    throw new Error("the false claim came back supported: the judgement is not reading the page");
  }

  console.log("\n[validators] second check on the first watch, to leave a history");
  await write("run_check", [supportedId]);
  const history = await read("checks_of", [supportedId]);
  console.log("  history:", history.map((h) => `#${h.seq} ${h.verdict}`).join(", "));
  if (history.length !== 2) throw new Error(`expected 2 checks in the history, got ${history.length}`);

  console.log("\nstats");
  console.log(JSON.stringify(await read("stats", []), null, 2));
  console.log("\nRESULT: a supported claim, a contradicted claim, and a two entry history, all on chain");
})().catch((e) => {
  console.error("\nFAILED:", e.message);
  if (e.cause) console.error(String(e.cause).slice(0, 800));
  process.exit(1);
});
