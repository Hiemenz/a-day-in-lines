# Lessons

One JSON file per drawing, in a folder for its subject. The file is the drawing: preview images are made from it and aren't saved here.

To add one, put it in a subject folder (or make a new folder), add a row below, then check it and look at it:

```bash
python3 a_day_in_lines.py lesson lessons/ --check
python3 a_day_in_lines.py lesson lessons/<subject>/<name>.json --preview preview/
```

File names must be different from each other, even in different folders. The format is described in the [main README](../README.md#lesson-files).

| Folder | Drawing | Steps | Source |
|---|---|---|---|
| animals | [Cat Seen from Behind](animals/cat-from-behind.json) | 10 | Made for this project |
| animals | [Owl on a Branch](animals/owl.json) | 10 | Made for this project |
| animals | [Swan](animals/swan.json) | 20 | Traced from E. G. Lutz, 1913 |
| holidays | [Decorated Tree](holidays/decorated-tree.json) | 10 | Made for this project |
| holidays | [Wrapped Present](holidays/present.json) | 10 | Made for this project |
| holidays | [Christmas Stocking](holidays/stocking.json) | 10 | Made for this project |
| home | [Teapot](home/teapot.json) | 10 | Made for this project |
| sea | [Lighthouse](sea/lighthouse.json) | 10 | Made for this project |
| sea | [Sailing Ship](sea/sailing-ship.json) | 10 | Made for this project |
| winter | [Cabin in the Snow](winter/cabin.json) | 10 | Made for this project |
| winter | [Igloo](winter/igloo.json) | 10 | Made for this project |
| winter | [Pine Tree](winter/pine-tree.json) | 10 | Made for this project |
| winter | [Snowman](winter/snowman.json) | 10 | Made for this project |
