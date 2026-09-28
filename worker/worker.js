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
// Bindings: KV "FFD" ; EMAIL "NOTIFY" (Email Workers, destination help@freefaxdesk.com) ; secrets ADMIN_SECRET, MAILERLITE_KEY (optional)
// Vars: NOTIFY_TO, NOTIFY_FROM, FOUNDING_CAP, ML_GROUP_UPDATES, ML_GROUP_PROVIDERS, ML_GROUP_FOUNDING

const ORIGINS = ["https://freefaxdesk.com", "https://www.freefaxdesk.com", "http://127.0.0.1:8750"];
const clean = (s, n) => String(s == null ? "" : s).replace(/[\u0000-\u001f\u007f]/g, " ").trim().slice(0, n);
const isEmail = (e) => /^[^\s@]{1,64}@[^\s@]{1,190}\.[a-z]{2,}$/i.test(e);
const json = (o, code = 200, extra = {}) => new Response(JSON.stringify(o), { status: code, headers: { "content-type": "application/json; charset=utf-8", ...extra } });

function cors(req) {
  const o = req.headers.get("Origin") || "";
  const ok = ORIGINS.includes(o) ? o : ORIGINS[0];
  return { "Access-Control-Allow-Origin": ok, "Access-Control-Allow-Methods": "GET,POST,OPTIONS", "Access-Control-Allow-Headers": "content-type", "Vary": "Origin" };
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
