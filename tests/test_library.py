import io
import os
import time

import pytest
from PIL import Image

from day_in_lines.constants import MAX_UPLOAD_PIXELS
from day_in_lines.library import Library
from day_in_lines.planner import Look
from conftest import make_png


def lib_for(tmp_path, **kw):
    src = tmp_path / "pictures"
    src.mkdir(exist_ok=True)
    return Library(tmp_path / "cache", src, "crop", **kw), src


def png_bytes(size=(60, 40), fmt="PNG", color=120):
    buf = io.BytesIO()
    Image.new("RGB", size, (color,) * 3).save(buf, fmt)
    return buf.getvalue()


def bake_dir(lib, h):
    d = lib.dir(h)
    (d / "frames").mkdir(parents=True)
    (d / "meta.json").write_text("{}")
    return d


# ---- scanning and identity ----
def test_scan_finds_pictures_once(tmp_path):
    lib, src = lib_for(tmp_path)
    make_png(src / "a.png")
    assert len(lib.scan()) == 1
    assert lib.scan() == []


def test_renaming_a_picture_keeps_its_identity(tmp_path):
    lib, src = lib_for(tmp_path)
    p = make_png(src / "a.png")
    (h,) = lib.scan()
    p.rename(src / "renamed.png")
    assert lib.scan() == []
    assert lib.data["images"][h]["name"] == "renamed"


def test_files_still_being_copied_are_left_for_the_next_scan(tmp_path):
    lib, src = lib_for(tmp_path)
    Image.new("L", (20, 20)).save(src / "new.png")        # modified just now
    assert lib.scan() == []
    old = time.time() - 60
    os.utime(src / "new.png", (old, old))
    assert len(lib.scan()) == 1


def test_unknown_file_types_are_ignored(tmp_path):
    lib, src = lib_for(tmp_path)
    (src / "notes.txt").write_text("hi")
    assert lib.scan() == []


# ---- uploads ----
def test_upload_is_saved_and_deduplicated(tmp_path):
    lib, src = lib_for(tmp_path)
    h = lib.add_upload(png_bytes(), "holiday.png")
    assert (src / "holiday.png").exists()
    assert lib.add_upload(png_bytes(), "again.png") == h
    assert [p.name for p in src.iterdir()] == ["holiday.png"]


def test_upload_names_are_made_safe_and_unique(tmp_path):
    lib, src = lib_for(tmp_path)
    lib.add_upload(png_bytes(color=10), "../../etc/pass wd?.png")
    lib.add_upload(png_bytes(color=20), "../../etc/pass wd?.png")
    names = sorted(p.name for p in src.iterdir())
    assert names == ["pass_wd_-2.png", "pass_wd_.png"]


def test_upload_that_is_not_an_image_is_refused(tmp_path):
    lib, _ = lib_for(tmp_path)
    with pytest.raises(ValueError, match="not an image"):
        lib.add_upload(b"MZ definitely not a picture", "x.png")


def test_upload_that_is_too_many_pixels_is_refused(tmp_path):
    lib, src = lib_for(tmp_path)
    side = int(MAX_UPLOAD_PIXELS ** .5) + 50
    buf = io.BytesIO()
    Image.new("1", (side, side)).save(buf, "PNG")           # tiny file, huge picture
    with pytest.raises(ValueError, match="too large"):
        lib.add_upload(buf.getvalue(), "bomb.png")
    assert list(src.iterdir()) == []


def test_gif_upload_is_stored_as_png(tmp_path):
    lib, src = lib_for(tmp_path)
    lib.add_upload(png_bytes(fmt="GIF"), "anim.gif")
    assert [p.name for p in src.iterdir()] == ["anim.png"]


def test_single_file_mode_keeps_uploads_in_the_cache(tmp_path):
    one = make_png(tmp_path / "only.png")
    lib = Library(tmp_path / "cache", one, "crop")
    lib.add_upload(png_bytes(), "extra.png")
    assert (tmp_path / "cache" / "uploads" / "extra.png").exists()
    assert {p.name for p in lib.sources()} == {"only.png", "extra.png"}


# ---- the daily pick ----
def test_new_pictures_go_first_and_the_pick_is_remembered(tmp_path):
    import datetime as dt
    lib, src = lib_for(tmp_path)
    make_png(src / "a.png", seed=1)
    make_png(src / "b.png", seed=2)
    for h in lib.scan():
        bake_dir(lib, h)
    day1, day2 = dt.date(2026, 3, 10), dt.date(2026, 3, 11)
    first = lib.pick(day1)
    assert lib.pick(day1) == first                    # reboots agree
    assert lib.pick(day2) != first                    # then the other one, not the same twice
    assert lib.pick(dt.date(2026, 3, 12)) == first    # then whichever was drawn longest ago


def test_pick_with_no_pictures(tmp_path):
    import datetime as dt
    lib, _ = lib_for(tmp_path)
    assert lib.pick(dt.date(2026, 3, 10)) is None


# ---- stored frames ----
def test_folder_name_changes_with_settings_and_version(tmp_path):
    lib, src = lib_for(tmp_path)
    make_png(src / "a.png")
    (h,) = lib.scan()
    plain = lib.dir(h).name
    assert plain.endswith("-v2") and "-d" not in plain
    other = Library(tmp_path / "cache", src, "crop", look=Look(1.5, 1.0))
    other.data = lib.data
    assert other.dir(h).name != plain and "-d1.5-c1" in other.dir(h).name


def test_prune_frees_old_settings_and_missing_pictures_but_not_live_ones(tmp_path):
    lib, src = lib_for(tmp_path)
    make_png(src / "a.png", seed=1)
    pb = make_png(src / "b.png", seed=2)
    ha, hb = lib.scan()
    live, gone = bake_dir(lib, ha), bake_dir(lib, hb)
    old_settings = lib.plans / "0123456789abcdef-crop-800x480-mono-g2.4-s1"
    (old_settings / "frames").mkdir(parents=True)
    pb.unlink()
    stale_part = lib.plans / ".x.99.part"
    stale_part.mkdir()
    long_ago = time.time() - 3 * 86400
    os.utime(stale_part, (long_ago, long_ago))
    fresh_part = lib.plans / ".y.100.part"
    fresh_part.mkdir()

    removed = lib.prune(keep=0)

    assert set(removed) == {old_settings.name, gone.name, stale_part.name}
    assert live.exists() and fresh_part.exists()


def test_prune_keeps_the_newest_few_deleted_pictures_and_anything_protected(tmp_path):
    lib, src = lib_for(tmp_path)
    paths = [make_png(src / f"{i}.png", seed=i) for i in range(4)]
    hashes = lib.scan()
    dirs = [bake_dir(lib, h) for h in hashes]
    for i, d in enumerate(dirs):
        os.utime(d, (1000 + i, 1000 + i))
    for p in paths:
        p.unlink()
    removed = lib.prune(keep=2, protect={hashes[0]})
    survivors = {d.name for d in dirs if d.exists()}
    assert dirs[0].name in survivors                        # protected (e.g. on the sheet now)
    assert dirs[3].name in survivors and dirs[2].name in survivors   # the two newest
    assert dirs[1].name not in survivors and len(removed) == 1


def test_prune_never_deletes_when_asked_to_keep_everything(tmp_path):
    lib, src = lib_for(tmp_path)
    p = make_png(src / "a.png")
    (h,) = lib.scan()
    d = bake_dir(lib, h)
    p.unlink()
    assert lib.prune(keep=-1) == []
    assert d.exists()
