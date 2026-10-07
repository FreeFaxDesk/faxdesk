// Road relay test: fake KV, a Node "phone" (WebCrypto), and a Python "office" (roadcrypto.py via child process).
// node test_road.mjs   (from the worker folder; expects ../src/faxdesk/roadcrypto.py and python3 on PATH)
import w from "./worker.js";
import { execFileSync } from "node:child_process";
import { webcrypto as crypto } from "node:crypto";
const store = new Map();
const env = { FFD: { get: async k => store.get(k) ?? null, put: async (k, v) => store.set(k, v), delete: async k => store.delete(k), list: async ({ prefix }) => ({ keys: [...store.keys()].filter(k => k.startsWith(prefix)).map(name => ({ name })) }) }, ADMIN_SECRET: "s3cret" };
const H = { "content-type": "application/json", "Origin": "https://freefaxdesk.com", "CF-Connecting-IP": "1.2.3.4" };
const call = async (method, p, body, token) => { const r = await w.fetch(new Request("https://api.freefaxdesk.com" + p, { method, headers: { ...H, ...(token ? { Authorization: "Bearer " + token } : {}) }, body: body ? JSON.stringify(body) : undefined }), env); return [r.status, await r.json().catch(() => ({}))]; };
const py = (args, input) => execFileSync("python3", ["-c", `
import sys, json, base64
sys.path.insert(0, "../src")
from faxdesk import roadcrypto as rc
cmd = json.loads(sys.stdin.read())
if cmd["op"] == "keys":
    priv, pub = rc.new_keypair(); print(json.dumps({"priv": priv, "pub": pub}))
elif cmd["op"] == "enc":
    blob = rc.pack(cmd["meta"], base64.b64decode(cmd["data"])); print(json.dumps(rc.encrypt_to(cmd["pub"], blob, rc.aad_for(cmd["office"], cmd["device"]))))
elif cmd["op"] == "dec":
    meta, data = rc.unpack(rc.decrypt(cmd["priv"], cmd["env"], rc.aad_for(cmd["office"], cmd["device"]))); print(json.dumps({"meta": meta, "data": base64.b64encode(data).decode()}))
elif cmd["op"] == "fp":
    print(json.dumps(rc.fingerprint(cmd["pub"])))
`], { input: JSON.stringify(input), encoding: "utf8" }).trim();

// ---- phone side crypto (what road.html does)
const b64u = (buf) => Buffer.from(new Uint8Array(buf)).toString("base64url");
const b64ud = (s) => new Uint8Array(Buffer.from(s, "base64url"));
async function phoneKeys() { const k = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]); const pub = await crypto.subtle.exportKey("jwk", k.publicKey); delete pub.key_ops; delete pub.ext; return { k, pub: { kty: pub.kty, crv: pub.crv, x: pub.x, y: pub.y } }; }
const aadOf = (o, d) => new TextEncoder().encode("ffd-road-v1|" + o + "|" + d);
async function sealTo(pubJwk, bytes, aad) {
  const rpub = await crypto.subtle.importKey("jwk", pubJwk, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const eph = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: rpub }, eph.privateKey, 256);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const hk = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: iv, info: new TextEncoder().encode("ffd-road-v1") }, hk, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const ct = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: aad }, key, bytes);
  const ek = await crypto.subtle.exportKey("jwk", eph.publicKey);
  return { v: 1, ek: { kty: "EC", crv: "P-256", x: ek.x, y: ek.y }, iv: b64u(iv), ct: b64u(ct) };
}
async function open(k, envlp, aad) {
  const spub = await crypto.subtle.importKey("jwk", envlp.ek, { name: "ECDH", namedCurve: "P-256" }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: spub }, k.privateKey, 256);
  const iv = b64ud(envlp.iv);
  const hk = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: iv, info: new TextEncoder().encode("ffd-road-v1") }, hk, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
  return new Uint8Array(await crypto.subtle.decrypt({ name: "AES-GCM", iv, additionalData: aad }, key, b64ud(envlp.ct)));
}
const pack = (meta, data) => { const h = new TextEncoder().encode(JSON.stringify(meta)); const out = new Uint8Array(4 + h.length + data.length); new DataView(out.buffer).setUint32(0, h.length); out.set(h, 4); out.set(data, 4 + h.length); return out; };
const unpack = (blob) => { const n = new DataView(blob.buffer, blob.byteOffset).getUint32(0); return [JSON.parse(new TextDecoder().decode(blob.slice(4, 4 + n))), blob.slice(4 + n)]; };

let fails = 0; const ok = (c, m) => { if (!c) { fails++; console.log("FAIL", m); } else console.log("  ok ", m); };

// 1. office registers with a Python key pair
const office = JSON.parse(py([], { op: "keys" }));
let [s, r] = await call("POST", "/road/office/register", { pub: office.pub, name: "Westside Family Practice" });
ok(s === 200 && r.office_token, "office register"); const OT = r.office_token, OID = r.office_id;
[s, r] = await call("POST", "/road/office/register", { pub: { kty: "EC", crv: "P-256", x: "short", y: "short" }, name: "x" }); ok(s === 400, "bad key refused");

// 2. invite + enroll a phone
[s, r] = await call("POST", "/road/office/invite", { label: "Dr. Example iPhone" }, OT); ok(s === 200 && r.code.length === 8, "invite code " + r.code); const CODE = r.code;
const phone = await phoneKeys();
[s, r] = await call("POST", "/road/device/enroll", { code: CODE, pub: phone.pub, name: "Dr. Example" }); ok(s === 200 && r.status === "pending" && r.office_pub.x === office.pub.x, "enroll -> pending, got office pub"); const DT = r.device_token, DID = r.device_id;
[s, r] = await call("POST", "/road/device/enroll", { code: CODE, pub: phone.pub, name: "again" }); ok(s === 400, "code is one-use");

[s, r] = await call("POST", "/road/office/people", { people: ["Nat", "Dr. Example", "<script>x"] }, OT); ok(s === 200 && r.people.length === 3, "office sets people");
[s, r] = await call("GET", "/road/device", null, DT); ok(s === 200 && r.people[0] === "Nat" && r.status === "pending", "device sees people + status");
[s, r] = await call("GET", "/road/vapid"); ok(s === 200 && r.ok, "vapid key endpoint (empty in test)");
// 3. pending device cannot send; office allows; then it can
const doc = new TextEncoder().encode("%PDF-1.4 fake signed consent");
let envp = await sealTo(office.pub, pack({ name: "consent.pdf", from: "Dr. Example", kind: "doc", note: "signed in the car" }, doc), aadOf(OID, DID));
[s, r] = await call("POST", "/road/env", { to: "office", kind: "doc", size: doc.length, body: envp }, DT); ok(s === 403, "pending phone blocked from sending");
[s, r] = await call("GET", "/road/office", null, OT); ok(s === 200 && r.pending === 1 && r.devices[0].name === "Dr. Example", "office sees pending device");
ok(JSON.parse(py([], { op: "fp", pub: r.devices[0].pub })) === JSON.parse(py([], { op: "fp", pub: phone.pub })), "fingerprint the office computes == the phone's own (what Allow checks)");
[s, r] = await call("POST", "/road/office/device", { device_id: DID, status: "allow" }, OT); ok(s === 200 && r.device.status === "allowed", "office allows device");
[s, r] = await call("POST", "/road/env", { to: "office", kind: "doc", size: doc.length, body: envp }, DT); ok(s === 200 && r.id, "phone -> office envelope stored"); const E1 = r.id;

// 4. office pulls, decrypts in Python, acks
[s, r] = await call("GET", "/road/env", null, OT); ok(s === 200 && r.rows.length === 1 && r.rows[0].from === DID && r.rows[0].kind === "doc", "office list shows 1 header, no body");
[s, r] = await call("GET", "/road/env/" + E1, null, OT); ok(s === 200 && r.body.ct, "office fetches envelope");
const dec = JSON.parse(py([], { op: "dec", priv: office.priv, env: r.body, office: OID, device: DID }));
let badAad = "no"; try { py([], { op: "dec", priv: office.priv, env: r.body, office: OID, device: "dev_somebodyelse" }); } catch (e) { badAad = "refused"; }
ok(badAad === "refused", "AAD: the same envelope relabelled to another device does not open");
ok(dec.meta.name === "consent.pdf" && Buffer.from(dec.data, "base64").toString() === "%PDF-1.4 fake signed consent", "PYTHON decrypts what the PHONE sealed");
[s, r] = await call("DELETE", "/road/env/" + E1, null, OT); ok(s === 200, "office acks");
[s, r] = await call("GET", "/road/env", null, OT); ok(r.rows.length === 0, "gone after ack");

// 5. office -> phone (Python seals), phone decrypts in Node
const fax = Buffer.from("%PDF-1.4 incoming fax from Valley Ortho").toString("base64");
const envo = JSON.parse(py([], { op: "enc", pub: phone.pub, meta: { name: "fax-valley-ortho.pdf", from: "Valley Ortho (818) 555-0123", kind: "fax", pages: 3, assigned_to: "Dr. Example" }, data: fax, office: OID, device: DID }));
[s, r] = await call("POST", "/road/env", { to: DID, kind: "fax", size: 40, body: envo }, OT); ok(s === 200, "office -> phone envelope stored"); const E2 = r.id;
[s, r] = await call("GET", "/road/env", null, DT); ok(s === 200 && r.rows.length === 1 && r.rows[0].from === "office", "phone list");
[s, r] = await call("GET", "/road/env/" + E2, null, DT);
const [meta2, data2] = unpack(await open(phone.k, r.body, aadOf(OID, DID)));
ok(meta2.from.startsWith("Valley Ortho") && new TextDecoder().decode(data2).includes("Valley Ortho"), "PHONE decrypts what PYTHON sealed");

// 6. isolation: a second office cannot see the first office's envelopes; a blocked device is refused
const office2 = JSON.parse(py([], { op: "keys" }));
[s, r] = await call("POST", "/road/office/register", { pub: office2.pub, name: "Other" }); const OT2 = r.office_token;
[s, r] = await call("GET", "/road/env/" + E2, null, OT2); ok(s === 404, "other office cannot read");
[s, r] = await call("POST", "/road/office/device", { device_id: DID, status: "block" }, OT2); ok(s === 404, "other office cannot manage our device");
[s, r] = await call("POST", "/road/office/device", { device_id: DID, status: "block" }, OT); ok(s === 200, "block device");
[s, r] = await call("POST", "/road/env", { to: "office", kind: "doc", size: 1, body: envp }, DT); ok(s === 403, "blocked device refused");
[s, r] = await call("GET", "/road/env", null, "nottoken-nottoken-nottoken"); ok(s === 401, "bad token 401");
// 8. rate limit: the 11th enrol attempt from one IP in a minute is refused
let last = 0; for (let i = 0; i < 11; i++) { [last] = await call("POST", "/road/device/enroll", { code: "NOPE1234", pub: phone.pub, name: "x" }); }
ok(last === 429, "rate limit: enrol guessing stops at 10/min");
// 7. relay holds no plaintext
const dump = JSON.stringify([...store.entries()]);
ok(!dump.includes("consent") && !dump.includes("Valley Ortho") && !dump.includes("signed in the car"), "relay storage contains no plaintext");
console.log(fails ? `road relay: ${fails} FAIL` : "road relay: ALL PASS");
process.exit(fails ? 1 : 0);
