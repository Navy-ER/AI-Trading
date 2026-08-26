# Phase 8: verifikasi rotasi log (10MB -> bridge.log.bak)
import os, tempfile, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge

tmpdir = tempfile.mkdtemp(prefix="logrot_")
main_path = os.path.join(tmpdir, "bridge.log")
bak_path = main_path + ".bak"

orig_main, orig_max = bridge.BRIDGE_LOG, bridge.LOG_MAX_BYTES
try:
    bridge.BRIDGE_LOG = main_path
    bridge.LOG_MAX_BYTES = 10  # tiny, supaya langsung rotate

    bridge.log("first")
    assert os.path.exists(main_path), "log pertama harus tertulis"
    bridge.log("second")  # size > max -> rotate dulu, baru tulis
    assert os.path.exists(bak_path), "file .bak harus dibuat saat rotate"
    with open(bak_path, encoding="utf-8") as f:
        assert "first" in f.read(), ".bak harus berisi log lama"
    with open(main_path, encoding="utf-8") as f:
        assert "second" in f.read(), "log baru harus di file utama"
    assert "first" not in open(main_path, encoding="utf-8").read(), "file utama harus fresh setelah rotate"
    print("PASS rotate: main fresh + .bak berisi log lama")
    print("ALL PASS")
finally:
    bridge.BRIDGE_LOG, bridge.LOG_MAX_BYTES = orig_main, orig_max
    for p in (main_path, bak_path):
        try:
            os.remove(p)
        except OSError:
            pass
    os.rmdir(tmpdir)
