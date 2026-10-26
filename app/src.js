import { createClient, createAccount, generatePrivateKey } from "genlayer-js";
import { studionet } from "genlayer-js/chains";

const CONTRACT = "0xca60Ef61C9B3f4383174131067B72e392a877Ba0";
const EXPLORER = `https://explorer-studio.genlayer.com/address/${CONTRACT}`;
const KEY_SLOT = "claimwatch.keys";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

let logTimer;
function log(msg) {
  const box = $("log");
  box.textContent = msg;
  box.style.display = "block";
  clearTimeout(logTimer);
  logTimer = setTimeout(() => (box.style.display = "none"), 7000);
}

function loadKeys() {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY_SLOT) || "[]");
    return Array.isArray(raw) ? raw.filter((k) => typeof k === "string" && k) : [];
  } catch {
    return [];
  }
}

let keys = loadKeys();
let active = 0;
let account = keys.length ? createAccount(keys[0]) : null;
let me = account ? String(account.address) : "";
let client = account ? createClient({ chain: studionet, account }) : null;
const reader = createClient({ chain: studionet, account: createAccount(generatePrivateKey()) });

function short(a) {
  return a ? `${a.slice(0, 6)}…${a.slice(-4)}` : "";
}

function saveKeys() {
  localStorage.setItem(KEY_SLOT, JSON.stringify(keys));
}

function useKey(i) {
  active = i;
  account = keys.length ? createAccount(keys[i]) : null;
  me = account ? String(account.address) : "";
  client = account ? createClient({ chain: studionet, account }) : null;
  paintKey();
}

function paintKey() {
  const sel = $("acting");
  sel.replaceChildren();
  if (!keys.length) {
    const o = el("option", "", "no key");
    o.value = "";
    sel.append(o);
  }
  keys.forEach((k, i) => {
    const o = el("option", "", `key ${i + 1} · ${short(String(createAccount(k).address))}`);
    o.value = String(i);
    sel.append(o);
  });
  sel.value = String(active);
  $("addr").textContent = me || "no key yet";
  $("walletchip").textContent = me ? `key ${active + 1} · ${short(me)}` : "No key";
}

$("contractlink").textContent = short(CONTRACT);
$("contractlink").href = EXPLORER;
paintKey();

$("acting").onchange = (e) => useKey(Number(e.target.value) || 0);

$("genkey").onclick = () => {
  keys.push(generatePrivateKey());
  saveKeys();
  useKey(keys.length - 1);
  log("new key added");
};

$("importkey").onclick = () => {
  const k = prompt("Private key (0x…)");
  if (!k) return;
  keys.push(k.trim());
  saveKeys();
  useKey(keys.length - 1);
  log("key imported");
};

async function read(functionName, args) {
  return reader.readContract({ address: CONTRACT, functionName, args });
}

async function send(functionName, args) {
  if (!client) {
    log("add a key first");
    return;
  }
  log(`${functionName}: signing`);
  const hash = await client.writeContract({ address: CONTRACT, functionName, args, value: 0n });
  log(`${functionName}: ${hash.slice(0, 12)}… waiting for the round`);
  await client.waitForTransactionReceipt({ hash, status: "FINALIZED", retries: 160, interval: 3000 });
  log(`${functionName}: done`);
  await refresh();
}

function when(ts) {
  const n = Number(ts || 0);
  if (!n) return "—";
  return new Date(n * 1000).toLocaleString();
}

function actionButton(label, fn, primary = false) {
  const b = el("button", `small${primary ? " primary" : ""}`, label);
  b.onclick = async () => {
    b.disabled = true;
    try {
      await fn();
    } catch (e) {
      log(String(e.message || e).slice(0, 200));
    } finally {
      b.disabled = false;
    }
  };
  return b;
}

function renderHistory(host, checks) {
  const box = el("div", "history");
  if (!checks.length) {
    box.append(el("div", "", "no checks yet"));
  }
  checks
    .slice()
    .reverse()
    .forEach((c) => {
      const line = el("div", "");
      const missing = (c.unmet || []).length ? ` · missing ${c.unmet.join(", ")}` : "";
      line.innerHTML = `#${c.seq} · <b class="pill ${c.verdict}">${c.verdict}</b>${missing} · ${when(c.checked_at)} · ${short(c.caller)}`;
      box.append(line);
      if (c.quote) {
        const q = el("div", "");
        q.append(el("q", "", c.quote));
        box.append(q);
      }
    });
  host.append(box);
}

function renderWatch(w, checks) {
  const card = el("div", "watch");
  const head = el("div", "meta");
  head.append(el("span", `pill ${w.state || "active"}`, w.state || "active"));
  head.append(el("span", `pill ${w.last_verdict || "unchecked"}`, w.last_verdict || "unchecked"));
  head.append(el("span", "pill", `${w.checks} check${w.checks === "1" ? "" : "s"}`));
  card.append(head);

  card.append(el("h3", "", w.claim));

  if ((w.requirements || []).length > 1) {
    const list = el("ol", "reqs");
    w.requirements.forEach((line, i) => {
      const missing = (w.unmet || []).includes(i + 1);
      const row = el("li", missing ? "missing" : "");
      row.append(document.createTextNode(line));
      if (missing) row.append(el("span", "tag", "missing"));
      list.append(row);
    });
    card.append(list);
  }

  if ((w.unmet || []).length > 0) {
    const parts = [`missing ${w.unmet.join(", ")}`, w.material ? "material" : "cosmetic"];
    card.append(el("div", "kv", parts.join(" · ")));
  }
  if (w.state === "breached") {
    card.append(el("div", "kv breach", `cure window ends ${when(w.cure_deadline)}`));
  }
  if (w.state === "cured") {
    card.append(el("div", "kv", `${w.breaches} breach on record · cured ${when(w.last_checked_at)}`));
  }
  const url = el("div", "kv");
  url.innerHTML = `page <a href="${w.url}" target="_blank" rel="noreferrer">${w.url.replace(/^https?:\/\//, "").slice(0, 56)}</a>`;
  card.append(url);
  card.append(el("div", "kv", `owner ${short(w.owner)} · created ${when(w.created_at)} · last check ${when(w.last_checked_at)}`));

  const latest = checks.length ? checks[checks.length - 1] : null;
  if (latest) {
    const v = el("div", "verdict");
    v.append(el("b", "", `${latest.verdict} · round ${latest.seq}`));
    v.append(document.createTextNode(latest.reason || ""));
    if (latest.quote) {
      const q = el("q", "", ` ${latest.quote}`);
      v.append(q);
    }
    card.append(v);
  }

  const acts = el("div", "actions");
  acts.append(actionButton("Run check", () => send("run_check", [w.id]), true));
  card.append(acts);
  renderHistory(card, checks);
  return card;
}

async function refresh() {
  try {
    const [stats, rows] = await Promise.all([read("stats", []), read("list_watches", [0, 30])]);
    const s = $("stats");
    s.replaceChildren();
    const cells = [
      ["Watches", stats.watches],
      ["Checks", stats.checks],
      ["Supported", stats.last_verdicts.supported || 0],
      ["Contradicted", stats.last_verdicts.contradicted || 0],
      ["Breached", (stats.states && stats.states.breached) || 0],
      ["Cured", (stats.states && stats.states.cured) || 0],
    ];
    for (const [k, v] of cells) {
      const c = el("div", "stat");
      c.append(el("span", "", k), el("b", "", String(v)));
      s.append(c);
    }

    const list = $("list");
    list.replaceChildren();
    if (!rows.length) {
      const empty = el("div", "card");
      empty.append(el("h2", "", "No watches yet"));
      empty.append(el("p", "sub", "Create the first one on the right: a URL and the sentence the page is supposed to say."));
      list.append(empty);
      return;
    }
    const histories = await Promise.all(rows.map((w) => read("checks_of", [w.id])));
    rows.forEach((w, i) => list.append(renderWatch(w, histories[i])));
  } catch (e) {
    log(`read failed: ${String(e.message || e).slice(0, 160)}`);
  }
}

$("create").onclick = async () => {
  const url = $("f-url").value.trim();
  const claim = $("f-claim").value.trim();
  if (!url || !claim) {
    log("url and claim are required");
    return;
  }
  const id = `w-${Date.now()}`;
  try {
    await send("create_watch", [id, url, claim]);
    $("f-url").value = "";
    $("f-claim").value = "";
  } catch (e) {
    log(String(e.message || e).slice(0, 200));
  }
};

refresh();
