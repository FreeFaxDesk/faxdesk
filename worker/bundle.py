"""bundle.py - make worker.bundle.js: worker.js + relay.js in ONE file for the Cloudflare dashboard editor (which takes a single module).
   python bundle.py   -> worker.bundle.js  (paste that; never edit it by hand)"""
import re, pathlib
here = pathlib.Path(__file__).parent
relay = (here / "relay.js").read_text(encoding="utf-8")
worker = (here / "worker.js").read_text(encoding="utf-8")
relay = relay.replace("export async function road(", "road = async function(")
assert "import" not in relay.split("\n")[0]
w = re.sub(r'^import \{ road \} from "\./relay\.js";[^\n]*\n', "let road;\n", worker, flags=re.M)
assert w != worker, "import line not found"
bundle = "// BUILT FILE - python bundle.py (worker.js + relay.js). Edit the sources, not this.\n" + w + "\n\n// ===== relay.js =====\n{\n" + relay + "\n}\n"
(here / "worker.bundle.js").write_text(bundle, encoding="utf-8", newline="\n")
print("worker.bundle.js", len(bundle), "bytes")
