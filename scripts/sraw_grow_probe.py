"""v16_5 §47.4 diagnostic: where does creating a GPU block cost its time?

    sraw_grow_probe.py [--count 6] [--rounds 2]   -> one JSON line

In a QOpenGLWidget on the real display (GL 3.3 core, as the viewer), for
each block size (256 / 64 / 32 layers of 512x512 R8UI = 64 / 16 / 8 MiB),
allocation call (glTexImage3D as the store, or glTexStorage3D) and first-use
payment (none / old = the store's former FBO warm / touch = the store's
`gpu_touch`: attach + one sampled texel),
creates COUNT blocks in a row and times per block: draining queued work
(glFinish before), the allocation call, its glFinish, the warm, then three
real uses (a full tile upload + a draw sampling that layer + glFinish) in
layer 0, the middle layer, the last layer, the middle again and the layer
after it. Variants run in a rotated
order per round. Median and max per stage and variant.
"""


import argparse
import json
import os
import statistics
import sys

from PyQt5 import QtCore, QtGui, QtWidgets

STAGES = ["drain", "alloc_call", "alloc_finish", "warm", "use_layer0", "use_middle", "use_last",
          "reuse_middle", "use_next"]
VARIANTS = [(layers, storage, warm) for layers in (256, 64, 32)
            for storage in (False, True) for warm in ("old", "none", "touch")]


class Probe(QtWidgets.QOpenGLWidget):
    def __init__(self, count, rounds):
        super().__init__()
        fmt = QtGui.QSurfaceFormat()
        fmt.setRenderableType(QtGui.QSurfaceFormat.OpenGL)
        fmt.setVersion(3, 3)
        fmt.setProfile(QtGui.QSurfaceFormat.CoreProfile)
        self.setFormat(fmt)
        self.count, self.rounds, self.result, self.done = count, rounds, None, False

    def paintGL(self):  # noqa: N802
        if self.done:
            return
        self.done = True
        import fusion_viewer_core as fvc
        context = self.context()

        def get_proc(name):
            pointer = context.getProcAddress(QtCore.QByteArray(name))
            return int(pointer) if pointer else 0

        fmt = context.format()
        storage_ok = ((fmt.majorVersion(), fmt.minorVersion()) >= (4, 2)
                      or context.hasExtension(QtCore.QByteArray(b"GL_ARB_texture_storage")))
        variants = [v for v in VARIANTS if storage_ok or not v[1]]   # glTexStorage3D needs 4.2 / ARB
        samples = {v: [] for v in variants}
        for r in range(self.rounds):
            order = variants[r % len(variants):] + variants[:r % len(variants)]
            for v in (order if r % 2 == 0 else order[::-1]):
                samples[v] += fvc.grow_probe_for_test(get_proc, v[0], v[1], v[2], self.count)
        rows = []
        for (layers, storage, warm), blocks in samples.items():
            row = {"layers": layers, "mib": layers // 4, "api": "storage" if storage else "image",
                   "first_use": warm, "blocks": len(blocks)}
            for i, name in enumerate(STAGES):
                vals = [b[i] for b in blocks]
                row[name] = round(statistics.median(vals), 3) if vals else None
                row[name + "_max"] = round(max(vals), 3) if vals else None
            row["total"] = round(sum(row[n] for n in STAGES[1:] if row[n] is not None), 3)
            rows.append(row)
        self.result = {"gl": f"{fmt.majorVersion()}.{fmt.minorVersion()}", "rows": rows}
        QtCore.QTimer.singleShot(0, QtWidgets.QApplication.quit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=2)
    args = ap.parse_args()
    app = QtWidgets.QApplication(sys.argv)
    w = Probe(args.count, args.rounds)
    w.resize(320, 240)
    w.show()
    QtCore.QTimer.singleShot(120000, app.quit)
    app.exec_()
    print(json.dumps(w.result), flush=True)
    os._exit(0 if w.result else 1)


if __name__ == "__main__":
    main()
