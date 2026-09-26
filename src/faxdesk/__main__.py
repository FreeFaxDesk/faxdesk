"""python -m faxdesk            run (opens the browser on first run)
   python -m faxdesk --selftest  fake Phone.com on loopback, temp state, no network
   python -m faxdesk --no-browser / --port N / --state DIR"""
import os
import sys
import threading
import time
import webbrowser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        from . import selftest
        return selftest.run()
    if "--state" in argv:
        os.environ["FAXDESK_STATE"] = argv[argv.index("--state") + 1]
    from .server import make, VERSION
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else None
    try:
        app, srv = make(port=port)
    except OSError as e:
        url = "http://127.0.0.1:%d/" % (port or 8750)
        print("FaxDesk is already running at %s (%s) - opening it." % (url, type(e).__name__))
        if "--no-browser" not in argv:
            webbrowser.open(url)
        return 0
    app.worker.start()
    url = "http://127.0.0.1:%d/" % srv.server_address[1]
    print("FaxDesk %s at %s   (state: %s)   Ctrl+C stops it." % (VERSION, url, app.store.root))
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    if os.name == "nt" and "--no-tray" not in argv:
        # Windows: the server serves on a thread; the main thread owns the tray icon (Open / Quit) until Quit.
        from . import tray
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        if tray.run(url, lambda: (srv.shutdown(), app.worker.stop()), state_dir=str(app.store.root)):
            return 0
        try:                                   # no tray (odd session): the serving thread carries on until Ctrl+C
            while t.is_alive():
                t.join(1)
        except KeyboardInterrupt:
            srv.shutdown()
        app.worker.stop()
        return 0
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    app.worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
