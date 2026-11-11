"""Run the cinematic production with a Holopix membership instead of Molica.

Same production, same OpenMontage stage order, same checkpoints and board:
the scene clips are generated through OpenMontage's `video_selector`, routed
to the `holopix_video` tool (a Holopix membership session).

    python scripts/holopix_cinematic_run.py --scenes 1 --model seedance

Every generation spends the Holopix account's compute points (算力). The
script reads the balance before it starts and refuses to begin a run it
cannot pay for.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

RUNNER = Path(__file__).with_name("molica_cinematic_run.py")

if __name__ == "__main__":
    sys.argv = [str(RUNNER), "--tool", "holopix_video", *sys.argv[1:]]
    runpy.run_path(str(RUNNER), run_name="__main__")
