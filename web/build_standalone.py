"""Rebuild web/standalone.html from viewer.html, with a sample drawing planned from examples/tanker.png.

Run it after changing viewer.html or the planner:  python web/build_standalone.py"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))

from day_in_lines.export import viewer_page, viewer_plan   # noqa: E402
from day_in_lines.planner import Look                      # noqa: E402
from day_in_lines.media import plan                        # noqa: E402

events = plan(root / "examples" / "tanker.png", 1, "crop", Look())
(root / "web" / "standalone.html").write_text(viewer_page(viewer_plan(events)))
print("wrote web/standalone.html")
