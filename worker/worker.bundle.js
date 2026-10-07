// BUILT FILE - python bundle.py (worker.js + relay.js). Edit the sources, not this.
// Free Fax Desk - forms API. Cloudflare Worker + KV. Endpoints (all JSON, CORS from freefaxdesk.com):
//   POST /review     {name, role, office_type, city_state, desks, rating, quote, may_publish, may_contact, email, subscribe, hp, t}
//   POST /founding   {name, office, office_type, city_state, desks, provider, email, phone, why, agree, subscribe, hp, t}
//   POST /subscribe  {email, provider, source, hp, t}
//   GET  /reviews    -> {rows:[{name, role, office_type, city_state, rating, quote, at}]}  (approved only)
//   GET  /founding/count -> {cap, approved, left}
//   GET  /admin/approve?kind=review|founding&id=..&sig=..   (link in the notification email; sig = HMAC(ADMIN_SECRET, kind:id))
//   GET  /admin/reject ?kind=..&id=..&sig=..
//   GET  /admin/unpublish?id=..&sig=..   (takes a published review off the site; kept on file)
//   GET  /admin/pending?sig=HMAC(ADMIN_SECRET,"pending")   (owner's inbox page: everything awaiting a decision + sign-ups)
//   /road/*          Road relay - see relay.js (office PC <-> phones, encrypted envelopes, push)
// Bindings: KV "FFD" ; EMAIL "NOTIFY" (Email Workers, destination help@freefaxdesk.com) ; secrets ADMIN_SECRET, MAILERLITE_KEY (optional)
// Vars: NOTIFY_TO, NOTIFY_FROM, FOUNDING_CAP, ML_GROUP_UPDATES, ML_GROUP_PROVIDERS, ML_GROUP_FOUNDING

let road;

const ORIGINS = ["https://freefaxdesk.com", "https://www.freefaxdesk.com", "http://127.0.0.1:8750"];
const clean = (s, n) => String(s == null ? "" : s).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, n);
const isEmail = (e) => /^[^\s@]{1,64}@[^\s@]{1,190}\.[a-z]{2,}$/i.test(e);
const json = (o, code = 200, extra = {}) => new Response(JSON.stringify(o), { status: code, headers: { "content-type": "application/json; charset=utf-8", ...extra } });

function cors(req) {
  const o = req.headers.get("Origin") || "";
  const ok = ORIGINS.includes(o) ? o : ORIGINS[0];
  return { "Access-Control-Allow-Origin": ok, "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS", "Access-Control-Allow-Headers": "content-type, authorization", "Vary": "Origin" };
}
async function hmac(secret, msg) {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const s = await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg));
  return [...new Uint8Array(s)].map(b => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
}
const newId = () => new Date().toISOString().replace(/[-:T]/g, "").slice(0, 14) + "-" + crypto.randomUUID().slice(0, 8);

// spam: honeypot must be empty, form must have been open >= 3 s, and <= 5 posts per IP per hour
async function gate(env, req, body) {
  if (body.hp) return "spam";
  const t = Number(body.t || 0); if (!t || Date.now() - t < 3000) return "too fast";
  const ip = req.headers.get("CF-Connecting-IP") || "0";
  const key = "rl:" + ip + ":" + new Date().toISOString().slice(0, 13);
  const n = Number(await env.FFD.get(key) || 0);
  if (n >= 5) return "rate";
  await env.FFD.put(key, String(n + 1), { expirationTtl: 3700 });
  return "";
}

async function notify(env, subject, text) {
  if (!env.NOTIFY) return;
  const from = env.NOTIFY_FROM, to = env.NOTIFY_TO;
  try { // Email Service binding (object form)
    await env.NOTIFY.send({ from: { email: from, name: "Free Fax Desk forms" }, to: [{ email: to }], subject, text });
    return;
  } catch (e1) {
    try { // classic send_email binding (EmailMessage form)
      const { EmailMessage } = await import("cloudflare:email");
      const raw = `From: Free Fax Desk forms <${from}>\r\nTo: ${to}\r\nSubject: ${subject}\r\nContent-Type: text/plain; charset=utf-8\r\nMIME-Version: 1.0\r\n\r\n${text}\r\n`;
      await env.NOTIFY.send(new EmailMessage(from, to, raw));
    } catch (e2) { console.log("notify failed", String(e1), String(e2)); }
  }
}

async function mailerlite(env, email, groups, fields) {
  if (!env.MAILERLITE_KEY || !isEmail(email)) return "skipped";
  try {
    const r = await fetch("https://connect.mailerlite.com/api/subscribers", {
      method: "POST", headers: { "content-type": "application/json", "authorization": "Bearer " + env.MAILERLITE_KEY },
      body: JSON.stringify({ email, groups: groups.filter(g => g && g !== "-"), fields, status: "unconfirmed" }) // unconfirmed = MailerLite sends the double opt-in
    });
    return r.ok ? "ok" : "ml " + r.status;
  } catch (e) { return "ml error"; }
}

async function adminLinks(env, kind, id) {
  const sig = await hmac(env.ADMIN_SECRET, kind + ":" + id);
  const b = env.API_BASE || "https://api.freefaxdesk.com";
  return `Approve: ${b}/admin/approve?kind=${kind}&id=${id}&sig=${sig}\nReject:  ${b}/admin/reject?kind=${kind}&id=${id}&sig=${sig}`;
}

async function postReview(env, req, b) {
  const rec = {
    id: newId(), at: new Date().toISOString(), status: "pending",
    name: clean(b.name, 60), role: clean(b.role, 60), office_type: clean(b.office_type, 60), city_state: clean(b.city_state, 60),
    desks: clean(b.desks, 10), rating: Math.min(5, Math.max(1, Number(b.rating) || 5)), quote: clean(b.quote, 600),
    may_publish: !!b.may_publish, may_contact: !!b.may_contact, email: clean(b.email, 254).toLowerCase(),
  };
  if (rec.quote.length < 10 || !rec.name) return json({ ok: false, error: "Please add your name and a sentence or two." }, 400, cors(req));
  await env.FFD.put("review:" + rec.id, JSON.stringify(rec));
  let ml = "skipped";
  if (b.subscribe && isEmail(rec.email)) ml = await mailerlite(env, rec.email, [env.ML_GROUP_UPDATES], { name: rec.name, company: rec.office_type });
  await notify(env, `[FFD] New review ${rec.rating}/5 from ${rec.name} (${rec.office_type})`,
    `"${rec.quote}"\n\n${rec.name}, ${rec.role}, ${rec.office_type}, ${rec.city_state}, ${rec.desks} desks\nemail ${rec.email || "-"}  publish:${rec.may_publish} contact:${rec.may_contact} list:${ml}\n\n${await adminLinks(env, "review", rec.id)}`);
  return json({ ok: true, message: "Thank you. We read every one; if you said we may publish it, it appears after a quick check." }, 200, cors(req));
}

async function postFounding(env, req, b) {
  const cap = Number(env.FOUNDING_CAP || 25);
  const approved = Number(await env.FFD.get("founding:approved") || 0);
  const rec = {
    id: newId(), at: new Date().toISOString(), status: "pending",
    name: clean(b.name, 60), office: clean(b.office, 80), office_type: clean(b.office_type, 60), city_state: clean(b.city_state, 60),
    desks: clean(b.desks, 10), provider: clean(b.provider, 60), email: clean(b.email, 254).toLowerCase(), phone: clean(b.phone, 30),
    why: clean(b.why, 600), agree: !!b.agree,
  };
  if (!rec.name || !rec.office || !isEmail(rec.email) || !rec.agree) return json({ ok: false, error: "Name, office, a working email and the agreement box are needed." }, 400, cors(req));
  if (approved >= cap) { rec.status = "waitlist"; }
  await env.FFD.put("founding:" + rec.id, JSON.stringify(rec));
  const ml = b.subscribe ? await mailerlite(env, rec.email, [env.ML_GROUP_FOUNDING, env.ML_GROUP_UPDATES], { name: rec.name, company: rec.office, provider: rec.provider }) : "skipped";
  await notify(env, `[FFD] Founding Office application: ${rec.office} (${rec.office_type}, ${rec.desks} desks, ${rec.provider}) ${rec.status === "waitlist" ? "WAITLIST" : approved + 1 + "/" + cap}`,
    `${rec.name}  ${rec.email}  ${rec.phone}\n${rec.office}, ${rec.office_type}, ${rec.city_state}, ${rec.desks} desks, fax service: ${rec.provider}\n\nWhy: ${rec.why}\nlist:${ml}\n\n${await adminLinks(env, "founding", rec.id)}\n(approve = counts toward the cap; you still issue the key by email)`);
  return json({ ok: true, waitlist: rec.status === "waitlist", message: rec.status === "waitlist" ? "The 25 are taken; you're on the waitlist and we'll email you if a spot opens." : "Got it. We'll reply by email within two business days." }, 200, cors(req));
}

async function postSubscribe(env, req, b) {
  const email = clean(b.email, 254).toLowerCase(); const provider = clean(b.provider, 60); const source = clean(b.source, 40);
  if (!isEmail(email)) return json({ ok: false, error: "That email doesn't look right." }, 400, cors(req));
  const rec = { id: newId(), at: new Date().toISOString(), email, provider, source };
  await env.FFD.put("sub:" + rec.id, JSON.stringify(rec));
  if (provider) { const k = "provider:" + provider.toLowerCase().replace(/[^a-z0-9]+/g, "-"); await env.FFD.put(k, String(Number(await env.FFD.get(k) || 0) + 1)); }
  const ml = await mailerlite(env, email, [provider ? env.ML_GROUP_PROVIDERS : env.ML_GROUP_UPDATES], { provider, company: source });
  if (provider) await notify(env, `[FFD] Provider request: ${provider}`, `${email} asked for ${provider} (from ${source}). list:${ml}`);
  return json({ ok: true, message: provider ? `Thanks. We'll email you the moment ${provider} is supported. Check your inbox to confirm.` : "Thanks. Check your inbox to confirm." }, 200, cors(req));
}

async function getReviews(env, req) {
  const list = await env.FFD.list({ prefix: "approved:review:" });
  const rows = [];
  for (const k of list.keys) { const r = JSON.parse(await env.FFD.get(k.name) || "{}"); if (r.quote) rows.push({ name: r.name.split(" ")[0], role: r.role, office_type: r.office_type, city_state: r.city_state, rating: r.rating, quote: r.quote, at: r.at.slice(0, 10) }); }
  rows.sort((a, b) => b.at.localeCompare(a.at));
  return json({ rows }, 200, { ...cors(req), "cache-control": "public, max-age=300" });
}

async function admin(env, req, url, action) {
  const kind = url.searchParams.get("kind"), id = url.searchParams.get("id"), sig = url.searchParams.get("sig");
  if (!["review", "founding"].includes(kind) || !id || sig !== await hmac(env.ADMIN_SECRET, kind + ":" + id)) return new Response("bad link", { status: 403 });
  const key = kind + ":" + id; const raw = await env.FFD.get(key); if (!raw) return new Response("not found (already handled?)", { status: 404 });
  const rec = JSON.parse(raw);
  if (rec.status === "approved" || rec.status === "rejected") return new Response(`already ${rec.status}`, { status: 200 });
  rec.status = action; rec.decided_at = new Date().toISOString();
  await env.FFD.put(key, JSON.stringify(rec));
  if (action === "approved") {
    if (kind === "review" && rec.may_publish) await env.FFD.put("approved:review:" + id, JSON.stringify(rec));
    if (kind === "founding") {
      const cap = Number(env.FOUNDING_CAP || 25), a = Number(await env.FFD.get("founding:approved") || 0);
      if (a >= cap) { rec.status = "waitlist"; await env.FFD.put(key, JSON.stringify(rec)); return new Response(`cap reached (${a}/${cap}); left on the waitlist. Raise FOUNDING_CAP to approve more.`, { status: 409 }); }
      await env.FFD.put("founding:approved", String(a + 1));
    }
  }
  return new Response(`${kind} ${id}: ${action}` + (kind === "founding" && action === "approved" ? `\n\nNow issue the key: python -m faxdesk.license ${rec.email}  and email it to ${rec.name} <${rec.email}>.` : ""), { headers: { "content-type": "text/plain" } });
}

async function pending(env, req, url) {
  if (url.searchParams.get("sig") !== await hmac(env.ADMIN_SECRET, "pending")) return new Response("bad link", { status: 403 });
  const esc = (x) => String(x == null ? "" : x).replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));
  const b = env.API_BASE || "https://api.freefaxdesk.com";
  let h = "<!doctype html><meta charset=utf-8><title>FFD pending</title><style>body{font:15px system-ui;max-width:900px;margin:30px auto;padding:0 16px}div.c{border:1px solid #ccc;border-radius:6px;padding:12px;margin:10px 0}a.b{display:inline-block;padding:6px 12px;border-radius:5px;color:#fff;text-decoration:none;margin-right:8px}small{color:#666}</style><h1>Awaiting a decision</h1>";
  let n = 0;
  for (const kind of ["review", "founding"]) {
    const list = await env.FFD.list({ prefix: kind + ":" });
    for (const k of list.keys) {
      if (k.name.startsWith("founding:approved")) continue;
      const r = JSON.parse(await env.FFD.get(k.name) || "{}"); if (!r.id || !["pending", "waitlist"].includes(r.status)) continue;
      n++; const sig = await hmac(env.ADMIN_SECRET, kind + ":" + r.id);
      h += `<div class=c><b>${kind.toUpperCase()}</b> <small>${esc(r.at)} ${esc(r.status)}</small><br>` +
        (kind === "review" ? `${"&#9733;".repeat(r.rating || 0)} "${esc(r.quote)}"<br>${esc(r.name)}, ${esc(r.role)}, ${esc(r.office_type)}, ${esc(r.city_state)}, ${esc(r.desks)} desks - ${esc(r.email) || "no email"} - publish:${r.may_publish} contact:${r.may_contact}`
                          : `${esc(r.name)} - ${esc(r.office)} (${esc(r.office_type)}, ${esc(r.city_state)}, ${esc(r.desks)} desks, ${esc(r.provider)})<br>${esc(r.email)} ${esc(r.phone)}<br><i>${esc(r.why)}</i>`) +
        `<br><br><a class=b style="background:#2e7d32" href="${b}/admin/approve?kind=${kind}&id=${r.id}&sig=${sig}">Approve</a><a class=b style="background:#b23a1e" href="${b}/admin/reject?kind=${kind}&id=${r.id}&sig=${sig}">Reject</a></div>`;
    }
  }
  const pub = await env.FFD.list({ prefix: "approved:review:" }); h += `<h2>Published (${pub.keys.length})</h2>`;
  for (const k of pub.keys) { const r = JSON.parse(await env.FFD.get(k.name) || "{}"); const sig = await hmac(env.ADMIN_SECRET, "review:" + r.id);
    h += `<div class=c>${"&#9733;".repeat(r.rating || 0)} "${esc(r.quote)}" <small>- ${esc(r.name)}, ${esc(r.office_type)}</small><br><br><a class=b style="background:#555" href="${b}/admin/unpublish?id=${r.id}&sig=${sig}">Unpublish</a></div>`; }
  const subs = await env.FFD.list({ prefix: "sub:" }); h += `<h2>Email sign-ups (${subs.keys.length})</h2>`;
  for (const k of subs.keys.slice(-50).reverse()) { const r = JSON.parse(await env.FFD.get(k.name) || "{}"); h += `<div class=c>${esc(r.at)} - ${esc(r.email)} - ${esc(r.provider) || "(updates)"} - ${esc(r.source)}</div>`; }
  if (!n) h = h.replace("</h1>", "</h1><p>Nothing pending.</p>");
  return new Response(h, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url); const p = url.pathname.replace(/\/+$/, "") || "/";
    if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: cors(req) });
    if (p === "/road" || p.startsWith("/road/")) { try { return await road(req, env, url, p, cors(req)); } catch (e) { console.log("road", String(e)); return json({ ok: false, error: "Relay error." }, 500, cors(req)); } }
    try {
      if (req.method === "GET") {
        if (p === "/reviews") return getReviews(env, req);
        if (p === "/founding/count") { const cap = Number(env.FOUNDING_CAP || 25), a = Number(await env.FFD.get("founding:approved") || 0); return json({ cap, approved: a, left: Math.max(0, cap - a) }, 200, { ...cors(req), "cache-control": "public, max-age=60" }); }
        if (p === "/admin/approve") return admin(env, req, url, "approved");
        if (p === "/admin/reject") return admin(env, req, url, "rejected");
        if (p === "/admin/pending") return pending(env, req, url);
        if (p === "/admin/unpublish") {
          const id = url.searchParams.get("id"), sig = url.searchParams.get("sig");
          if (!id || sig !== await hmac(env.ADMIN_SECRET, "review:" + id)) return new Response("bad link", { status: 403 });
          await env.FFD.delete("approved:review:" + id);
          const raw = await env.FFD.get("review:" + id); if (raw) { const r = JSON.parse(raw); r.status = "unpublished"; await env.FFD.put("review:" + id, JSON.stringify(r)); }
          return new Response("review " + id + ": unpublished (kept on file, off the site within 5 minutes)", { headers: { "content-type": "text/plain" } });
        }
        if (p === "/health") return json({ ok: true }, 200, cors(req));
        return json({ error: "not found" }, 404, cors(req));
      }
      if (req.method === "POST") {
        let b; try { b = await req.json(); } catch { return json({ ok: false, error: "bad json" }, 400, cors(req)); }
        const g = await gate(env, req, b); if (g) return json({ ok: false, error: g === "rate" ? "Too many submissions from this connection; try again in an hour." : "Please try again." }, 429, cors(req));
        if (p === "/review") return postReview(env, req, b);
        if (p === "/founding") return postFounding(env, req, b);
        if (p === "/subscribe") return postSubscribe(env, req, b);
      }
      return json({ error: "not found" }, 404, cors(req));
    } catch (e) { return json({ ok: false, error: "server error" }, 500, cors(req)); }
  }
};


// ===== relay.js =====
{
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

road = async function(req, env, url, p, corsHeaders) {
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

}
