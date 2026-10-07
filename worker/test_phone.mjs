// End-to-end: real road.html in Chromium (phone) <-> real worker.js (dev_relay.mjs on :8787) <-> real faxdesk.road.Road (office_cli.py).
// Run: node dev_relay.mjs &  then  node test_phone.mjs
import { chromium } from "/home/claude/.npm-global/lib/node_modules/playwright/index.mjs";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
const ST = "../state_rd_e2e"; fs.rmSync(ST, { recursive: true, force: true });
const office = (...a) => JSON.parse(execFileSync("python3", ["office_cli.py", ST, ...a], { encoding: "utf8" }).trim());
let fails = 0; const ok = (c, m) => { if (!c) { fails++; console.log("FAIL", m); } else console.log("  ok ", m); };
const shot = async (p, n) => p.screenshot({ path: "../out/shots/road_" + n + ".png" });
fs.mkdirSync("../out/shots", { recursive: true });

office("enable"); const inv = office("invite"); ok(/^[A-Z0-9]{8}$/.test(inv.code), "office invite " + inv.code);
const br = await chromium.launch(); const ctx = await br.newContext({ viewport: { width: 390, height: 780 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
const p = await ctx.newPage(); const errs = []; p.on("pageerror", e => errs.push(String(e))); p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
await p.goto("http://127.0.0.1:8787/road.html#" + inv.code); await p.waitForTimeout(300);
ok(await p.inputValue("#e-code") === inv.code, "code filled from link"); await shot(p, "1_join");
await p.fill("#e-name", "Dr. Example"); await p.click("#e-go"); await p.waitForSelector("#v-inbox", { state: "visible" }); await p.waitForTimeout(500);
ok(await p.isVisible("#pending"), "enrolled -> pending banner"); await shot(p, "2_pending");
const dev = JSON.parse(await p.evaluate(() => localStorage.getItem("ffd_road_dev"))); ok(dev.office_pub && dev.device_token, "device record saved; key in IndexedDB");
ok((await p.evaluate(async () => { const db = await new Promise(r => { const q = indexedDB.open("ffd-road", 1); q.onsuccess = () => r(q.result); }); return new Promise(r => { const t = db.transaction("kv").objectStore("kv").get("key"); t.onsuccess = () => r(t.result && t.result.privateKey && t.result.privateKey.extractable === false); }); })), "private key non-extractable");

// office allows + publishes people
const al = office("allow"); ok(al.length === 1 && al[0].device.status === "allowed", "office allowed the phone");
await p.reload(); await p.waitForTimeout(600); ok(!(await p.isVisible("#pending")), "banner gone after allow");

// phone -> office: two photographed pages
await p.click('#tabs button[data-v="send"]'); await p.waitForTimeout(200);
const chips = await p.$$eval("#s-to button", b => b.map(x => x.textContent)); ok(chips.join() === "Front desk,Nat,Dr. Example", "people chips from relay: " + chips.join(","));
await p.click('#s-to button:has-text("Nat")');
const png = (txt) => execFileSync("python3", ["-c", `
import sys
from PIL import Image, ImageDraw
im = Image.new("RGB", (1200, 1600), "white"); d = ImageDraw.Draw(im); d.text((80, 80), sys.argv[1], fill="black"); d.rectangle((60, 60, 1140, 1540), outline="black", width=4)
im.save(sys.stdout.buffer, "JPEG", quality=85)`, txt], { maxBuffer: 1 << 24 });
fs.writeFileSync("../out/shots/_p1.jpg", png("CONSENT page 1")); fs.writeFileSync("../out/shots/_p2.jpg", png("CONSENT page 2"));
await p.setInputFiles("#s-camin", ["../out/shots/_p1.jpg", "../out/shots/_p2.jpg"]); await p.waitForTimeout(800);
ok(await p.$$eval("#s-pages img", i => i.length) === 2, "two pages thumbnailed");
await p.fill("#s-name", "Consent - J. Doe"); await p.fill("#s-note", "signed in the car"); await shot(p, "3_send");
await p.click("#s-go"); await p.waitForSelector("#s-ok:not(:empty)", { timeout: 15000 }); ok((await p.textContent("#s-ok")).includes("for Nat"), "sent: " + await p.textContent("#s-ok"));
const landed = office("pull"); ok(landed.length === 1 && landed[0].assigned_to === "Nat" && landed[0].note === "signed in the car" && landed[0].pages === 2, "office landed it as one PDF assigned to Nat: " + JSON.stringify(landed[0] && { pages: landed[0].pages, assigned_to: landed[0].assigned_to, from_name: landed[0].from_name }));
const pdf = fs.readFileSync(ST + "/inbox/" + landed[0].id + ".pdf"); ok(pdf.slice(0, 5).toString() === "%PDF-" && pdf.includes("DCTDecode"), "landed PDF is a real JPEG-page PDF (" + pdf.length + " bytes)");

// office -> phone: a fax
fs.writeFileSync("../out/shots/_fax.pdf", "%PDF-1.4 incoming fax from Valley Ortho\n%%EOF");
const sent = office("send", dev.device_id, "../out/shots/_fax.pdf"); ok(sent.ok, "office sent fax to phone");
await p.click('#tabs button[data-v="inbox"]'); await p.waitForSelector("#in-list .item", { timeout: 8000 }); ok(await p.textContent("#t-n") === "1", "badge 1"); await shot(p, "4_inbox");
await p.click("#in-list .item button"); await p.waitForSelector("#viewer.on"); await p.waitForTimeout(300);
ok((await p.textContent("#vw-title")).includes("Valley Ortho"), "opened + decrypted on the phone: " + await p.textContent("#vw-title")); await shot(p, "5_viewer");
await p.click("#vw-done"); await p.waitForTimeout(600); ok(await p.$$eval("#in-list .item", i => i.length) === 0, "acked -> gone");

// relay never saw plaintext
const dump = await (await fetch("http://127.0.0.1:8787/__dump")).text(); ok(!dump.includes("CONSENT") && !dump.includes("Valley Ortho") && !dump.includes("signed in the car") && !dump.includes("J. Doe"), "relay has no plaintext");
await p.click('#tabs button[data-v="me"]'); await p.waitForTimeout(400); const fpShown = (await p.textContent("#fp-me")).trim(); const fpOffice = JSON.parse(execFileSync("python3", ["-c", "import sys,json;sys.path.insert(0,'../src');from faxdesk import roadcrypto as rc;print(json.dumps(rc.fingerprint(json.loads(sys.argv[1]))))", JSON.stringify(dev.pub || {})], { encoding: "utf8" }).trim());
ok(fpShown.length === 24 && fpShown === fpOffice, "This phone shows the same code the office checks at Allow: " + fpShown); await shot(p, "6_me");
ok(errs.length === 0, "no page errors" + (errs.length ? ": " + errs.join(" | ") : ""));
await br.close(); console.log(fails ? `road phone e2e: ${fails} FAIL` : "road phone e2e: ALL PASS"); process.exit(fails ? 1 : 0);
