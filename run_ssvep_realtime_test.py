"""Run from the project root. Frozen/spawn imports must not initialize Qt."""
import multiprocessing
import os

for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[variable] = "1"
# QWindow's fallback update idle delay can consume an entire 210 Hz frame.
# SwapInterval=1 still provides pacing; this only removes the extra Qt delay.
os.environ.setdefault("QT_QPA_UPDATE_IDLE_TIME", "0")

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from ssvep_realtime_test.app import main
    raise SystemExit(main())
