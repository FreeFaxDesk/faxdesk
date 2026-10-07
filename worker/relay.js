// Free Fax Desk - Road relay. Carries ENCRYPTED envelopes between an office PC (FaxDesk) and its people's phones, and
// pokes phones with content-free push notifications. The relay never holds a key and never sees a document.
//
// Mounted by worker.js under /road. Storage: the same KV ("FFD"), keys prefixed road:. Envelopes expire after RETAIN_DAYS.
//
//   POST /road/office/register   {pub, name}                      -> {office_id, office_token}       (one PC = one office)
//   GET  /road/office            Bearer office_token             -> {office_id, name, devices:[...], pending:n}
//   POST /road/office/invite     Bearer office_token  {label}     -> {code, expires}                  (8 chars, 24 h, one use)
//   POST /road/office/device     Bearer office_token  {device_id, status: allow|block|remove}
//   POST /road/office/people     Bearer office_token  {people:[names]}   (so a phone can address a person; names only)
//   GET  /road/vapid                                              -> {key}   (public push key for the phone to subscribe)
//   POST /road/device/enroll     {code, pub, name}                -> {device_id, device_token, office_id, office_name, office_pub}
//   GET  /road/device            Bearer device_token             -> {device_id, name, status, office_name, office_pub}
//   POST /road/device/push       Bearer device_token  {subscription}   (Web Push subscription from the phone)
//   POST /road/env               Bearer office|device {to:"office"|device_id, kind, size, body:{v,ek,iv,ct}} -> {id}
//   GET  /road/env               Bearer office|device             -> {rows:[{id, from, kind, size, at}]}   (headers only)
//   GET  /road/env/<id>          Bearer office|device             -> the envelope (only sender or recipient)
//   DELETE /road/env/<id>        Bearer office|device             -> {ok}  (recipient acknowledges = gone)
//
// Secrets (optional): VAPID_PUBLIC, VAPID_PRIVATE (JWK JSON) for push; VAPID_SUBJECT = "mailto:help@freefaxdesk.com".
// Envelopes are AES-GCM with the office id + device id as additional data (roadcrypto.aad_for), so a relabelled
// envelope does not open. Rate limits per minute: register 5/IP, enrol 10/IP, invite 20/office, env POST 60/identity, other 240.

const RETAIN_DAYS = 30, INVITE_TTL = 24 * 3600, MAX_BODY = 6 * 1024 * 1024, MAX_DEVICES = 50;
const json = (o, code = 200, extra = {}) => new Response(JSON.stringify(o), { status: code, headers: { "content-type": "application/json; charset=utf-8", ...extra } });
const clean = (s, n) => String(s == null ? "" : s).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, n);
const b64u = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const b64ud = (s) => Uint8Array.from(atob(String(s || "").replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - String(s || "").length % 4) % 4)), c => c.charCodeAt(0));
const now = () => new Date().toISOString().slice(0, 19);
const rid = (p) => p + "_" + crypto.randomUUID().replace(/-/g, "").slice(0, 20);
const isJwk = (k) => k && k.kty === "EC" && k.crv === "P-256" && typeof k.x === "string" && typeof k.y === "string" && k.x.length >= 40 && k.y.length >= 40 && !k.d;

async function sha256hex(s) { return [...new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, "0")).join(""); }
function newToken() { return b64u(crypto.getRandomValues(new Uint8Array(32))); }

// ---- identity from Bearer token: {kind:"office"|"device", id, office_id, rec}
async function who(env, req) {
  const h = req.headers.get("Authorization") || ""; const t = h.startsWith("Bearer ") ? h.slice(7).trim() : "";
  if (!t || t.length < 20) return null;
  const ref = await env.FFD.get("road:tok:" + await sha256hex(t));
  if (!ref) return null;
  const [kind, id] = ref.split(":");
  const rec = JSON.parse(await env.FFD.get("road:" + kind + ":" + id) || "null");
  if (!rec) return null;
  return { kind, id, office_id: kind === "office" ? id : rec.office_id, rec };
}
async function devicesOf(env, office_id) {
  const l = await env.FFD.list({ prefix: "road:dev-of:" + office_id + ":" });
  const out = [];
  for (const k of l.keys) { const d = JSON.parse(await env.FFD.get("road:device:" + k.name.split(":").pop()) || "null"); if (d) out.push({ device_id: d.device_id, name: d.name, status: d.status, at: d.at, last_seen: d.last_seen || "", push: !!d.push, pub: d.pub }); }
  return out;
}

async function register(env, req, b) {
  if (!isJwk(b.pub)) return json({ ok: false, error: "A P-256 public key is needed." }, 400);
  const office_id = rid("off"), token = newToken();
  await env.FFD.put("road:office:" + office_id, JSON.stringify({ office_id, name: clean(b.name, 80), pub: b.pub, at: now(), ip: req.headers.get("CF-Connecting-IP") || "" }));
  await env.FFD.put("road:tok:" + await sha256hex(token), "office:" + office_id);
  return json({ ok: true, office_id, office_token: token });
}
async function officeInfo(env, me) {
  const devs = await devicesOf(env, me.id);
  return json({ ok: true, office_id: me.id, name: me.rec.name, devices: devs, pending: devs.filter(d => d.status === "pending").length });
}
async function invite(env, me, b) {
  const devs = await devicesOf(env, me.id);
  if (devs.length >= MAX_DEVICES) return json({ ok: false, error: "Device limit reached." }, 400);
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"; let code = "";
  for (const v of crypto.getRandomValues(new Uint8Array(8))) code += alphabet[v % alphabet.length];
  const exp = Math.floor(Date.now() / 1000) + INVITE_TTL;
  await env.FFD.put("road:inv:" + code, JSON.stringify({ office_id: me.id, label: clean(b.label, 60), at: now() }), { expirationTtl: INVITE_TTL });
  return json({ ok: true, code, expires: new Date(exp * 1000).toISOString().slice(0, 19) });
}
async function enroll(env, req, b) {
  const code = clean(b.code, 12).toUpperCase().replace(/[^A-Z0-9]/g, "");
  const inv = JSON.parse(await env.FFD.get("road:inv:" + code) || "null");
  if (!inv) return json({ ok: false, error: "That code is not valid any more. Ask the office for a new one." }, 400);
  if (!isJwk(b.pub)) return json({ ok: false, error: "A P-256 public key is needed." }, 400);
  const office = JSON.parse(await env.FFD.get("road:office:" + inv.office_id) || "null");
  if (!office) return json({ ok: false, error: "That office is gone." }, 400);
  await env.FFD.delete("road:inv:" + code);
  const device_id = rid("dev"), token = newToken();
  const rec = { device_id, office_id: inv.office_id, name: clean(b.name, 60) || inv.label || "phone", label: inv.label, pub: b.pub, status: "pending", at: now(), ua: clean(req.headers.get("User-Agent"), 120) };
  await env.FFD.put("road:device:" + device_id, JSON.stringify(rec));
  await env.FFD.put("road:dev-of:" + inv.office_id + ":" + device_id, "1");
  await env.FFD.put("road:tok:" + await sha256hex(token), "device:" + device_id);
  return json({ ok: true, device_id, device_token: token, office_id: inv.office_id, office_name: office.name, office_pub: office.pub, status: "pending" });
}
async function deviceInfo(env, me) {
  const office = JSON.parse(await env.FFD.get("road:office:" + me.rec.office_id) || "null");
  me.rec.last_seen = now(); await env.FFD.put("road:device:" + me.id, JSON.stringify(me.rec));
  return json({ ok: true, device_id: me.id, name: me.rec.name, status: me.rec.status, office_name: office ? office.name : "", office_pub: office ? office.pub : null, people: office && office.people ? office.people : [] });
}
async function peopleSet(env, me, b) {
  const people = Array.isArray(b.people) ? b.people.map(x => clean(x, 40)).filter(Boolean).slice(0, 40) : [];
  me.rec.people = people; await env.FFD.put("road:office:" + me.id, JSON.stringify(me.rec));
  return json({ ok: true, people });
}
async function deviceSet(env, me, b) {
  const d = JSON.parse(await env.FFD.get("road:device:" + clean(b.device_id, 40)) || "null");
  if (!d || d.office_id !== me.id) return json({ ok: false, error: "Not your device." }, 404);
  const st = String(b.status || "");
  if (st === "remove") {
    await env.FFD.delete("road:device:" + d.device_id); await env.FFD.delete("road:dev-of:" + me.id + ":" + d.device_id);
    return json({ ok: true, removed: d.device_id });
  }
  if (!["allow", "block"].includes(st)) return json({ ok: false, error: "allow, block or remove." }, 400);
  d.status = st === "allow" ? "allowed" : "blocked"; d.status_at = now();
  await env.FFD.put("road:device:" + d.device_id, JSON.stringify(d));
  return json({ ok: true, device: { device_id: d.device_id, name: d.name, status: d.status } });
}
async function pushSub(env, me, b) {
  const s = b.subscription;
  if (!s || typeof s.endpoint !== "string" || !s.keys || !s.keys.p256dh || !s.keys.auth) return json({ ok: false, error: "Not a push subscription." }, 400);
  me.rec.push = { endpoint: s.endpoint.slice(0, 500), keys: { p256dh: String(s.keys.p256dh).slice(0, 200), auth: String(s.keys.auth).slice(0, 100) } };
  await env.FFD.put("road:device:" + me.id, JSON.stringify(me.rec));
  return json({ ok: true });
}

// ---- envelopes
async function envPost(env, me, b) {
  const body = b.body;
  if (!body || body.v !== 1 || !isJwk(body.ek) || typeof body.iv !== "string" || typeof body.ct !== "string") return json({ ok: false, error: "Not a Road envelope." }, 400);
  if (body.ct.length > MAX_BODY * 4 / 3) return json({ ok: false, error: "Too big (6 MB max)." }, 413);
  let to = clean(b.to, 40), office_id = me.office_id;
  if (me.kind === "device") {
    if (me.rec.status !== "allowed") return json({ ok: false, error: "This phone is not allowed yet - the office has to approve it." }, 403);
    to = "office";
  } else {
    const d = JSON.parse(await env.FFD.get("road:device:" + to) || "null");
    if (!d || d.office_id !== office_id || d.status !== "allowed") return json({ ok: false, error: "No such allowed device." }, 404);
  }
  const id = rid("env");
  const head = { id, office_id, from: me.kind === "office" ? "office" : me.id, from_name: me.kind === "office" ? "office" : me.rec.name, to, kind: clean(b.kind, 20) || "doc", size: Number(b.size) || 0, at: now() };
  await env.FFD.put("road:env:" + id, JSON.stringify({ head, body }), { expirationTtl: RETAIN_DAYS * 86400 });
  await env.FFD.put("road:env-for:" + office_id + ":" + to + ":" + id, JSON.stringify(head), { expirationTtl: RETAIN_DAYS * 86400 });
  if (me.kind === "office") { const d = JSON.parse(await env.FFD.get("road:device:" + to) || "null"); if (d && d.push) await push(env, d, head.kind); }
  return json({ ok: true, id, at: head.at });
}
async function envList(env, me) {
  const to = me.kind === "office" ? "office" : me.id;
  const l = await env.FFD.list({ prefix: "road:env-for:" + me.office_id + ":" + to + ":" });
  const rows = [];
  for (const k of l.keys.slice(0, 200)) { const h = JSON.parse(await env.FFD.get(k.name) || "null"); if (h) rows.push(h); }
  rows.sort((a, b) => a.at < b.at ? -1 : 1);
  return json({ ok: true, rows });
}
async function envGet(env, me, id) {
  const e = JSON.parse(await env.FFD.get("road:env:" + id) || "null");
  if (!e || e.head.office_id !== me.office_id) return json({ ok: false, error: "No such envelope." }, 404);
  const mine = me.kind === "office" ? (e.head.to === "office" || e.head.from === "office") : (e.head.to === me.id || e.head.from === me.id);
  if (!mine) return json({ ok: false, error: "No such envelope." }, 404);
  return json({ ok: true, head: e.head, body: e.body });
}
async function envDelete(env, me, id) {
  const e = JSON.parse(await env.FFD.get("road:env:" + id) || "null");
  if (!e || e.head.office_id !== me.office_id) return json({ ok: true, gone: true });
  const to = me.kind === "office" ? "office" : me.id;
  if (e.head.to !== to && e.head.from !== to) return json({ ok: false, error: "No such envelope." }, 404);
  await env.FFD.delete("road:env:" + id); await env.FFD.delete("road:env-for:" + e.head.office_id + ":" + e.head.to + ":" + id);
  return json({ ok: true });
}

// ---- Web Push (VAPID, no payload - the notification text is generic; the phone pulls the real thing)
async function vapidJwt(env, aud) {
  const priv = JSON.parse(env.VAPID_PRIVATE);
  const key = await crypto.subtle.importKey("jwk", priv, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const enc = (o) => b64u(new TextEncoder().encode(JSON.stringify(o)));
  const h = enc({ typ: "JWT", alg: "ES256" }), p = enc({ aud, exp: Math.floor(Date.now() / 1000) + 12 * 3600, sub: env.VAPID_SUBJECT || "mailto:help@freefaxdesk.com" });
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(h + "." + p));
  return h + "." + p + "." + b64u(sig);
}
async function push(env, device, kind) {
  if (!env.VAPID_PRIVATE || !env.VAPID_PUBLIC || !device.push) return;
  try {
    const u = new URL(device.push.endpoint); const jwt = await vapidJwt(env, u.origin);
    const r = await fetch(device.push.endpoint, { method: "POST", headers: { TTL: "3600", Urgency: "high", Authorization: "vapid t=" + jwt + ", k=" + env.VAPID_PUBLIC, "Content-Length": "0" } });
    if (r.status === 404 || r.status === 410) { delete device.push; await env.FFD.put("road:device:" + device.device_id, JSON.stringify(device)); }
  } catch (e) { console.log("push failed", String(e)); }
}

// Rate limits (Admin review 2026-10-04): a KV counter per minute. Generous for real use, tight enough to stop a loop or a guesser.
async function limited(env, kind, id, max) {
  const k = "road:rl:" + kind + ":" + id + ":" + Math.floor(Date.now() / 60000);
  const n = Number(await env.FFD.get(k) || 0) + 1;
  await env.FFD.put(k, String(n), { expirationTtl: 120 });
  return n > max;
}
const LIMITS = { register: 5, enroll: 10, invite: 20, env: 60, other: 240 };

export async function road(req, env, url, p, corsHeaders) {
  const C = corsHeaders;
  const seg = p.split("/").filter(Boolean);           // ["road", ...]
  let body = {};
  if (req.method === "POST") {
    const n = Number(req.headers.get("content-length") || 0); if (n > MAX_BODY * 1.4) return json({ ok: false, error: "Too big." }, 413, C);
    try { body = await req.json(); } catch { return json({ ok: false, error: "Bad JSON." }, 400, C); }
  }
  const wrap = async (r) => { const h = new Headers(r.headers); for (const [k, v] of Object.entries(C)) h.set(k, v); return new Response(r.body, { status: r.status, headers: h }); };
  const ip = req.headers.get("CF-Connecting-IP") || "0";
  if (req.method === "POST" && seg[1] === "office" && seg[2] === "register") { if (await limited(env, "register", ip, LIMITS.register)) return json({ ok: false, error: "Too many tries. Wait a minute." }, 429, C); return wrap(await register(env, req, body)); }
  if (req.method === "POST" && seg[1] === "device" && seg[2] === "enroll") { if (await limited(env, "enroll", ip, LIMITS.enroll)) return json({ ok: false, error: "Too many tries. Wait a minute." }, 429, C); return wrap(await enroll(env, req, body)); }
  if (req.method === "GET" && seg[1] === "vapid") return wrap(json({ ok: true, key: env.VAPID_PUBLIC || "" }));
  const me = await who(env, req);
  if (!me) { await limited(env, "badtoken", ip, 1); return json({ ok: false, error: "Not signed in to the relay." }, 401, C); }
  const bucket = (req.method === "POST" && seg[1] === "env") ? "env" : (req.method === "POST" && seg[2] === "invite") ? "invite" : "other";
  if (await limited(env, bucket, me.id, LIMITS[bucket])) return json({ ok: false, error: "Too many requests. Wait a minute." }, 429, C);
  if (seg[1] === "office" && me.kind === "office") {
    if (req.method === "GET" && !seg[2]) return wrap(await officeInfo(env, me));
    if (req.method === "POST" && seg[2] === "invite") return wrap(await invite(env, me, body));
    if (req.method === "POST" && seg[2] === "device") return wrap(await deviceSet(env, me, body));
    if (req.method === "POST" && seg[2] === "people") return wrap(await peopleSet(env, me, body));
  }
  if (seg[1] === "device" && me.kind === "device") {
    if (req.method === "GET" && !seg[2]) return wrap(await deviceInfo(env, me));
    if (req.method === "POST" && seg[2] === "push") return wrap(await pushSub(env, me, body));
  }
  if (seg[1] === "env") {
    if (req.method === "POST" && !seg[2]) return wrap(await envPost(env, me, body));
    if (req.method === "GET" && !seg[2]) return wrap(await envList(env, me));
    if (req.method === "GET" && seg[2]) return wrap(await envGet(env, me, clean(seg[2], 40)));
    if (req.method === "DELETE" && seg[2]) return wrap(await envDelete(env, me, clean(seg[2], 40)));
  }
  return json({ ok: false, error: "Not found." }, 404, C);
}
