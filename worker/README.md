# freefaxdesk-forms - Cloudflare Worker behind api.freefaxdesk.com

Handles the site's three forms (review, Founding Offices, "tell us your fax service" / email list), serves approved
quotes, and emails help@ on every submission with one-click approve/reject links. Storage: Workers KV. Mail: Email
Workers (send_email binding) to the already-verified help@ destination. List: MailerLite API (optional; skipped if no key).

## One-time setup (~15 min)
1. `npm i -g wrangler` then `wrangler login` (the product Cloudflare account, same one that holds the freefaxdesk.com zone).
2. `wrangler kv namespace create FFD` -> paste the id into wrangler.json (`REPLACE_WITH_KV_ID`).
3. Secrets: `wrangler secret put ADMIN_SECRET` (any long random string; signs the approve links)
   and, once MailerLite exists, `wrangler secret put MAILERLITE_KEY`.
4. MailerLite (free tier): create account with the product Gmail; make three groups: "Updates", "Providers", "Founding";
   put their group IDs in wrangler.json vars ML_GROUP_*. Turn on double opt-in (Account > Settings > Subscribe settings).
   Add custom fields `provider` and `company` (Subscribers > Fields). Until this is done the Worker still stores every
   address in KV, so nothing is lost.
5. Email Workers: Cloudflare dashboard > Email > Email Routing > verify that help@freefaxdesk.com routes to the Gmail
   (already true) and that `forms@freefaxdesk.com` is allowed as a sender (Email Routing > Email Workers; the
   send_email binding may only send to verified destination addresses, which help@ is).
6. `wrangler deploy` - the custom domain api.freefaxdesk.com is created automatically (routes.custom_domain). The apex
   stays DNS-only for GitHub Pages; only api.* is a Worker.
7. Test: `curl https://api.freefaxdesk.com/health` -> {"ok":true}. Submit the review form on the site; an email arrives at
   help@ with Approve / Reject links. Approve; the quote appears at https://freefaxdesk.com/reviews within 5 minutes.

## Deployed 2026-09-28 (dashboard, not wrangler)
- Worker `freefaxdesk-forms` on account c0873d6e91d8f367f7d827bb74603a13, live at https://api.freefaxdesk.com (custom domain).
- KV namespace FFD = 196743132b234585bc5955a3307987e1. Vars set in the dashboard; ML_GROUP_* are "-" (= unset) until MailerLite exists.
- Email: the dashboard's "Email Service" binding is Cloudflare Email Sending, which needs the Workers Paid plan ($5/mo).
  Until that is bought, no notification emails go out and the Worker logs the failure quietly. Everything else works.
- Owner's inbox page (bookmark; the sig is derived from ADMIN_SECRET): https://api.freefaxdesk.com/admin/pending?sig=7c33c1b9934086ffe82ece57f4ba6922
  Lists pending reviews/applications with Approve/Reject, published reviews with Unpublish, and email sign-ups.
- Code updates: paste worker.js into the dashboard editor
  (https://dash.cloudflare.com/c0873d6e91d8f367f7d827bb74603a13/workers/services/edit/freefaxdesk-forms/production) and Deploy.

## Day to day
- Every submission = one email to help@. Click Approve or Reject. Nothing publishes without Approve.
- Founding Offices: Approve counts toward the 25 (FOUNDING_CAP). You still issue the key by hand:
  `python -m faxdesk.license <their email>` and email it. Past the cap the link says so and the applicant stays on the waitlist.
- Provider requests: each one bumps a KV counter `provider:<name>`; `wrangler kv key list --binding FFD --prefix provider:`
  shows which service to build next.
- Spam controls: honeypot field, 3-second minimum, 5 posts per IP per hour. No CAPTCHA, no third-party script on the site.
- Until the Worker is deployed, the forms fall back to a pre-filled mailto: to help@, so the pages can ship first.

## Files
- worker.js, wrangler.json (this folder is in the public repo; secrets are never in files).
- Site pages: docs/review.html, docs/reviews.html, docs/founding.html, docs/forms.js.

## Road relay (added 2026-10-03, FaxDesk 1.2.0) - office PC <-> phones, sealed end to end
The same Worker now also answers `/road/*` (code in `relay.js`, mounted from `worker.js`). It carries ENCRYPTED envelopes
between an office PC and the phones it invited and pokes phones with a content-free Web Push. It never holds a key and
never sees a document; the KV rows under `road:*` are public keys, tokens (sha256 only), device status and sealed blobs.

### Deploy (one time, ~10 min)
1. The dashboard editor takes ONE file, so build it: `cd worker && python bundle.py` -> `worker.bundle.js`. Paste THAT into
   https://dash.cloudflare.com/c0873d6e91d8f367f7d827bb74603a13/workers/services/edit/freefaxdesk-forms/production and Deploy.
   (With wrangler instead: `wrangler deploy` from this folder picks up the import by itself; no bundle needed.)
2. Push keys (optional, for notifications; everything else works without them). In this folder:
   `node -e "crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},true,['sign','verify']).then(async k=>{const pub=await crypto.subtle.exportKey('jwk',k.publicKey),prv=await crypto.subtle.exportKey('jwk',k.privateKey);const raw=Buffer.concat([Buffer.from([4]),Buffer.from(pub.x,'base64url'),Buffer.from(pub.y,'base64url')]).toString('base64url');console.log('VAPID_PUBLIC='+raw);console.log('VAPID_PRIVATE='+JSON.stringify(prv))})"`
   Add the two lines as secrets (Worker > Settings > Variables and Secrets): `VAPID_PUBLIC` (the base64url string),
   `VAPID_PRIVATE` (the JWK JSON), and a plain var `VAPID_SUBJECT` = `mailto:help@freefaxdesk.com`. Generate once; changing
   the keys later silently breaks every phone's subscription until it re-subscribes.
3. Check: `curl https://api.freefaxdesk.com/road/vapid` -> `{"ok":true,"key":"..."}` (key empty until step 2).
4. The phone page is static: `docs/road.html`, `docs/road-sw.js`, `docs/road.webmanifest` ship with the site on GitHub Pages.

### Tests
- `node test_road.mjs` - relay against an in-memory KV, Node phone <-> Python office (needs python3 + cryptography).
- `node dev_relay.mjs &` then `node test_phone.mjs` - the real road.html in headless Chromium against the real relay code
  and the real `faxdesk.road` office (needs Playwright; writes screenshots to ../out/shots - scratch only).
- `node test.mjs` - the forms worker, unchanged.

### Allowing a phone (what the office sees)
Allow asks for the first 4 characters of the code shown on the phone's "This phone" screen (and on its waiting banner). The
PC compares it with the fingerprint of the key the relay handed over; a mismatch is refused and audited. That is the one
step that stops a relay operator swapping keys at enrolment. Every envelope is also bound to office id + device id (AES-GCM
additional data), so an envelope relabelled for another phone does not open.

### Limits in code
50 devices per office, envelope 6 MB, 30-day TTL on envelopes, invite codes 24 h and one use, per-minute rate limits (register 5/IP,
enrol 10/IP, invite 20/office, envelope POST 60, other 240), bearer tokens 32 random
bytes stored as sha256. Blocking a device on the PC refuses it at the relay at once; "remove" forgets it.
