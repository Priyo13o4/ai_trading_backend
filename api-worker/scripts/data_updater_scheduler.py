#!/usr/bin/env python3
"""Compatibility shim for the scheduler entrypoint.

The implementation now lives in scripts/worker/data_updater_scheduler.py.
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.worker.data_updater_scheduler import main


if __name__ == "__main__":
    main()
