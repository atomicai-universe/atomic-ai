/**
 * Provider guide link checker (BUILD.md).
 *
 * Validates every documentation URL referenced by the per-provider integration
 * guides in src/lib/provider-guides.ts. For each provider it reconstructs the
 * four step links (console, create, scopes, strands) exactly as the UI renders
 * them, de-duplicates, and issues an HTTP request per URL, reporting any that
 * do not resolve to a healthy status.
 *
 * Usage:
 *   node scripts/check-guide-links.mjs            # check all links
 *   node scripts/check-guide-links.mjs --json     # machine-readable output
 *   CHECK_CONCURRENCY=8 node scripts/check-guide-links.mjs
 *
 * Exit code is non-zero when any link is broken, so it can gate CI.
 *
 * Notes on accuracy:
 *   - Many vendor doc sites block HEAD or bot-like requests; we send a
 *     browser-like User-Agent and fall back from HEAD to GET.
 *   - 401/403/405 are treated as REACHABLE (the page exists but requires auth
 *     or disallows the method) rather than broken — a 404/410/5xx or network
 *     failure is what we flag.
 */

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const GUIDES_PATH = path.join(__dirname, "..", "src", "lib", "provider-guides.ts");

const CONCURRENCY = Number(process.env.CHECK_CONCURRENCY ?? 6);
const TIMEOUT_MS = Number(process.env.CHECK_TIMEOUT_MS ?? 15000);
const JSON_OUT = process.argv.includes("--json");

const BROWSER_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36";

// Statuses that mean "the page exists" even if we can't fully load it.
// 400 is included because several vendor doc hosts (Facebook, Asana, Zoho, …)
// reject non-browser HEAD/GET with 400 even though the page exists.
const REACHABLE = new Set([200, 201, 202, 204, 301, 302, 303, 307, 308, 400, 401, 403, 405, 406, 429]);

/**
 * Extract the STRANDS_* constants and the LINKS/GENERIC tables from the guide
 * source, then expand them into the exact URL set the UI would render.
 */
async function collectUrls() {
  const src = await readFile(GUIDES_PATH, "utf8");

  const strandsMcp = matchConst(src, "STRANDS_MCP_TOOLS_URL");
  const strandsTools = matchConst(src, "STRANDS_TOOLS_OVERVIEW_URL");

  const linksTable = sliceBetween(src, "const LINKS", "const STEP_OVERRIDES");
  const genericTable = sliceBetween(src, "const GENERIC", "getProviderGuide");

  const providers = parseProviderEntries(linksTable);
  const generic = parseSingleEntry(genericTable);

  // Map url -> list of "provider.step" references, so a broken url reports where.
  const urlRefs = new Map();
  const addRef = (url, ref) => {
    if (!url) return;
    if (!urlRefs.has(url)) urlRefs.set(url, []);
    urlRefs.get(url).push(ref);
  };

  for (const [name, links] of Object.entries(providers)) {
    addRef(links.console, `${name}.console`);
    addRef(links.create, `${name}.create`);
    addRef(links.scopes, `${name}.scopes`);
    addRef(links.strands ?? strandsMcp, `${name}.strands`);
  }
  // Generic fallback links are user-visible for any unmapped provider.
  addRef(generic.console, "generic.console");
  addRef(generic.create, "generic.create");
  addRef(generic.scopes, "generic.scopes");
  // Strands links are shared.
  addRef(strandsMcp, "strands.mcpTools");
  addRef(strandsTools, "strands.toolsOverview");

  // Also validate every URL used by the hand-written STEP_OVERRIDES, since those
  // replace the generated steps for many providers and add extra URLs (e.g.
  // Google API library pages, IAM/service-account pages, Entra quickstart).
  const overridesBlock = sliceBetween(src, "const STEP_OVERRIDES", "const GENERIC");
  const overrideUrlRe = /url:\s*"([^"]+)"/g;
  let om;
  let oi = 0;
  while ((om = overrideUrlRe.exec(overridesBlock)) !== null) {
    addRef(om[1], `override[${oi++}]`);
  }

  return { urlRefs, providerCount: Object.keys(providers).length };
}

function matchConst(src, name) {
  const re = new RegExp(`const\\s+${name}\\s*=\\s*\\n?\\s*"([^"]+)"`);
  const m = src.match(re);
  return m ? m[1] : null;
}

function sliceBetween(src, startToken, endToken) {
  const start = src.indexOf(startToken);
  const end = src.indexOf(endToken, start + startToken.length);
  return src.slice(start, end === -1 ? undefined : end);
}

/** Parse `  name: { console: "..", create: "..", scopes: "..", strands?: ".." },` */
function parseProviderEntries(block) {
  const out = {};
  // Match each `key: { ... }` entry (non-greedy on the object body).
  const entryRe = /^\s{2}([a-z0-9_]+):\s*\{([\s\S]*?)\},/gm;
  let m;
  while ((m = entryRe.exec(block)) !== null) {
    out[m[1]] = parseKvPairs(m[2]);
  }
  return out;
}

function parseSingleEntry(block) {
  const start = block.indexOf("{");
  const end = block.indexOf("}", start);
  return parseKvPairs(block.slice(start + 1, end));
}

function parseKvPairs(body) {
  const obj = {};
  const kvRe = /([a-z]+):\s*"([^"]+)"/g;
  let m;
  while ((m = kvRe.exec(body)) !== null) {
    obj[m[1]] = m[2];
  }
  return obj;
}

async function checkOnce(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  const opts = {
    redirect: "follow",
    signal: controller.signal,
    headers: {
      "User-Agent": BROWSER_UA,
      Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.9",
    },
  };
  try {
    // Try HEAD first (cheap); fall back to GET when HEAD is blocked/unsupported.
    let res = await fetch(url, { ...opts, method: "HEAD" });
    // Some hosts mishandle HEAD (405/501/403) or even 404/400 a HEAD they would
    // serve via GET (e.g. Google Help); confirm with a GET before trusting it.
    if (!REACHABLE.has(res.status)) {
      res = await fetch(url, { ...opts, method: "GET" });
    }
    return { url, status: res.status, ok: REACHABLE.has(res.status) };
  } catch (err) {
    return {
      url,
      status: 0,
      ok: false,
      error: err?.name === "AbortError" ? "timeout" : String(err?.message ?? err),
    };
  } finally {
    clearTimeout(timer);
  }
}

// URL prefixes that are valid in a real browser but reject all non-browser
// (bot/HEAD/GET) requests, so the checker can never reach them. Treated as OK.
const BROWSER_ONLY_PREFIXES = [
  "https://console.cloud.google.com/apis/library/",
];

function isBrowserOnly(url) {
  return BROWSER_ONLY_PREFIXES.some((p) => url.startsWith(p));
}

async function checkUrl(url) {
  if (isBrowserOnly(url)) {
    return { url, status: "browser-only", ok: true };
  }
  // One retry on a transient network failure/timeout (status 0) before flagging,
  // since vendor doc CDNs occasionally drop a bot-like request.
  let result = await checkOnce(url);
  if (!result.ok && result.status === 0) {
    await new Promise((r) => setTimeout(r, 500));
    const retry = await checkOnce(url);
    if (retry.ok || retry.status !== 0) result = retry;
  }
  return result;
}

async function runPool(urls, worker) {
  const results = [];
  let i = 0;
  const runners = Array.from({ length: Math.min(CONCURRENCY, urls.length) }, async () => {
    while (i < urls.length) {
      const idx = i++;
      results[idx] = await worker(urls[idx]);
    }
  });
  await Promise.all(runners);
  return results;
}

async function main() {
  const { urlRefs, providerCount } = await collectUrls();
  const urls = [...urlRefs.keys()];

  if (!JSON_OUT) {
    console.log(
      `Checking ${urls.length} unique links across ${providerCount} providers ` +
        `(concurrency ${CONCURRENCY}, timeout ${TIMEOUT_MS}ms)…\n`,
    );
  }

  const results = await runPool(urls, checkUrl);
  const broken = results.filter((r) => !r.ok);
  const ok = results.filter((r) => r.ok);

  if (JSON_OUT) {
    console.log(
      JSON.stringify(
        {
          total: results.length,
          ok: ok.length,
          broken: broken.map((b) => ({
            url: b.url,
            status: b.status,
            error: b.error ?? null,
            refs: urlRefs.get(b.url),
          })),
        },
        null,
        2,
      ),
    );
  } else {
    for (const r of results) {
      const refs = urlRefs.get(r.url).join(", ");
      const mark = r.ok ? "OK " : "XX ";
      const status = r.error ? r.error : r.status;
      if (!r.ok) {
        console.log(`${mark}[${status}] ${r.url}\n      used by: ${refs}`);
      }
    }
    console.log(`\n${ok.length}/${results.length} links reachable.`);
    if (broken.length) {
      console.log(`\n${broken.length} link(s) need attention (listed above).`);
    } else {
      console.log("All guide links are reachable. ✅");
    }
  }

  process.exit(broken.length ? 1 : 0);
}

main().catch((err) => {
  console.error("Link checker failed:", err);
  process.exit(2);
});
