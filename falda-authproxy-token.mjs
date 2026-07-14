#!/usr/bin/env node
/**
 * falda-authproxy-token.mjs — mint a bearer token for a tenant.
 *
 * Usage:  node falda-authproxy-token.mjs <tenant>
 *
 * Generates a 32-byte random token, prints the RAW token to stdout ONCE, and
 * persists only its sha256 hash -> tenant into the 0600 token map. The raw
 * secret is never stored, so it can't leak from the file. Copy it now; it can't
 * be recovered later (mint a new one if lost).
 */
import { readFileSync, writeFileSync, mkdirSync, chmodSync, existsSync } from "node:fs";
import { randomBytes, createHash } from "node:crypto";
import { homedir } from "node:os";
import { join, dirname } from "node:path";

const tenant = process.argv[2];
if (!tenant || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(tenant)) {
  process.stderr.write("usage: falda-authproxy-token.mjs <tenant>  (lowercase [a-z0-9_-], matches FALDA tenant rules)\n");
  process.exit(2);
}

const TOKENS_PATH = process.env.FALDA_AUTH_TOKENS ?? join(homedir(), ".falda", "authproxy.tokens.json");
const sha256 = (s) => createHash("sha256").update(s, "utf8").digest("hex");

let map = {};
if (existsSync(TOKENS_PATH)) {
  try { map = JSON.parse(readFileSync(TOKENS_PATH, "utf8")) || {}; }
  catch { process.stderr.write(`[token] WARN existing map unreadable, refusing to overwrite: ${TOKENS_PATH}\n`); process.exit(1); }
}

const raw = "falda_" + randomBytes(32).toString("base64url");
map[sha256(raw)] = tenant;

mkdirSync(dirname(TOKENS_PATH), { recursive: true });
writeFileSync(TOKENS_PATH, JSON.stringify(map, null, 2), "utf8");
chmodSync(TOKENS_PATH, 0o600);

process.stderr.write(`[token] minted for tenant='${tenant}', hash stored in ${TOKENS_PATH} (0600)\n`);
process.stderr.write(`[token] RAW TOKEN (shown once — copy now):\n`);
process.stdout.write(raw + "\n");
