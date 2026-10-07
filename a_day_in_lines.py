#!/usr/bin/env python3
"""A Day in Lines: give it any image and it draws it in pencil on e-ink over a day.

This file only launches the program; the code lives in the `day_in_lines` package
next to it. See README.md for the commands, e.g.

  python3 a_day_in_lines.py --image ~/pictures run --display waveshare:epd7in5_V2 --serve 8080
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from day_in_lines.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
