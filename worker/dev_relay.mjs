// Dev shim: serves ../out/road.html (+ sw/manifest) and mounts the real worker.js at /road/* with an in-memory KV.
// node dev_relay.mjs [port]  -> http://127.0.0.1:8787/road.html
import http from "node:http";
import fs from "node:fs";
import path from "node:path";
import w from "./worker.js";
const port = Number(process.argv[2] || 8787);
const store = new Map();
const env = { FFD: { get: async k => store.get(k) ?? null, put: async (k, v) => store.set(k, v), delete: async k => store.delete(k), list: async ({ prefix }) => ({ keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) }) }, ADMIN_SECRET: "s3cret" };
const out = path.resolve("../out");
const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml" };
http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://127.0.0.1:" + port);
  if (u.pathname === "/road" || u.pathname.startsWith("/road/")) {
    const chunks = []; for await (const c of req) chunks.push(c);
    const body = chunks.length ? Buffer.concat(chunks) : undefined;
    const r = await w.fetch(new Request("https://api.freefaxdesk.com" + u.pathname + u.search, { method: req.method, headers: { ...req.headers, Origin: "https://freefaxdesk.com", "CF-Connecting-IP": "127.0.0.1" }, body: ["GET", "HEAD"].includes(req.method) ? undefined : body }), env);
    res.writeHead(r.status, Object.fromEntries(r.headers)); res.end(Buffer.from(await r.arrayBuffer())); return;
  }
  if (u.pathname === "/__dump") { res.writeHead(200, { "content-type": "application/json" }); res.end(JSON.stringify([...store.entries()])); return; }
  const f = path.join(out, u.pathname === "/" ? "road.html" : u.pathname);
  if (!f.startsWith(out) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) { res.writeHead(404); res.end("nf"); return; }
  res.writeHead(200, { "content-type": types[path.extname(f)] || "application/octet-stream", "cache-control": "no-store" }); res.end(fs.readFileSync(f));
}).listen(port, "127.0.0.1", () => console.log("dev relay on", port));
