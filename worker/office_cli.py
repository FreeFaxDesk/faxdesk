"""Drive the real faxdesk.road.Road against the dev relay, from the command line (used by test_phone.mjs).
   python3 office_cli.py <state_dir> <cmd> [args]   cmds: enable | invite | allow | people | pull | send <device_id> <pdf>"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from faxdesk import store as _store, road as _road  # noqa: E402

state_dir = sys.argv[1]
os.makedirs(state_dir, exist_ok=True)
os.environ["FAXDESK_STATE"] = state_dir
st = _store.Store(state_dir)
st.save_config({"office": "Westside Family Practice"}); st.save_people(["Nat", "Dr. Example"])
rd = _road.Road(st, relay="http://127.0.0.1:8787/road")
cmd = sys.argv[2]
if cmd == "enable":
    print(json.dumps(rd.enable("test")[1]))
elif cmd == "invite":
    print(json.dumps(rd.invite("test", "test phone")[1]))
elif cmd == "allow":
    devs = rd.refresh_devices(); rd.sync_people(force=True)
    from faxdesk import roadcrypto as _rc
    print(json.dumps([rd.device("test", d["device_id"], "allow", _rc.fingerprint(d["pub"])[:4])[1] for d in devs if d["status"] == "pending"]))
elif cmd == "pull":
    rd.pull_once()
    import glob
    rows = [json.load(open(f)) for f in sorted(glob.glob(os.path.join(state_dir, "inbox", "rd-*.json")))]
    print(json.dumps(rows))
elif cmd == "send":
    did, pdf = sys.argv[3], sys.argv[4]
    print(json.dumps(rd.send("test", did, open(pdf, "rb").read(), {"name": "fax-valley-ortho.pdf", "from": "Valley Ortho (818) 555-0123", "kind": "fax", "pages": 1, "note": "for Dr. Example"})[1]))
elif cmd == "devices":
    print(json.dumps(rd.refresh_devices()))
