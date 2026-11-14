"""Regressions: a file replaced in place, deleting a picture that has copies, an empty upload
key, captions that look like options, and a picture that failed once."""
import argparse
import os

import pytest

from conftest import make_png
from day_in_lines.cli import upload_key_for
from day_in_lines.library import Library
from day_in_lines.slack_app import parse_options
from day_in_lines.submit import BadRequest


@pytest.fixture
def lib(tmp_path):
    (tmp_path / "pics").mkdir()
    return Library(tmp_path / "cache", tmp_path / "pics", "crop")


def test_a_file_replaced_by_another_picture_is_no_longer_the_old_one(lib, tmp_path):
    pics = tmp_path / "pics"
    make_png(pics / "a.png", seed=1)
    (old,) = lib.scan()
    make_png(pics / "a.png", seed=2)
    os.utime(pics / "a.png", (1_000_000, 1_000_000))            # a different mtime, as a real replacement has
    (new,) = lib.scan()
    assert old != new and lib.usable(new) and not lib.present(old) and not lib.usable(old)
    assert lib.unbaked() == [new]                               # the old one is not baked or picked
    assert lib.pick(__import__("datetime").date(2026, 3, 1)) == new
    lib.delete(old)                                             # removing the stale one leaves the new file alone
    assert (pics / "a.png").exists() and lib.usable(new)


def test_the_old_version_comes_back_if_the_file_does(lib, tmp_path):
    pics = tmp_path / "pics"
    make_png(pics / "a.png", seed=1)
    (old,) = lib.scan()
    make_png(pics / "a.png", seed=2)
    os.utime(pics / "a.png", (1_000_000, 1_000_000))
    lib.scan()
    make_png(pics / "a.png", seed=1)
    os.utime(pics / "a.png", (1_100_000, 1_100_000))
    lib.scan()
    assert lib.usable(old)


def test_deleting_a_picture_removes_its_copies_too(lib, tmp_path):
    pics = tmp_path / "pics"
    make_png(pics / "b.png", seed=3)
    make_png(pics / "b_copy.png", seed=3)
    keep = make_png(pics / "other.png", seed=4)
    lib.scan()
    (h,) = [h for h, i in lib.data["images"].items() if i["name"].startswith("b")]
    lib.delete(h)
    assert sorted(p.name for p in pics.iterdir()) == ["other.png"] and keep.exists()
    assert lib.scan() == [] and h not in lib.data["images"]     # nothing to add back


def test_an_empty_upload_key_is_replaced_not_treated_as_open(tmp_path):
    args = argparse.Namespace(open_uploads=False, upload_key=None)
    f = tmp_path / "upload_key"
    f.write_text("")
    key = upload_key_for(args, tmp_path)
    assert key and len(key) >= 10 and f.read_text().strip() == key
    f.write_text("   \n")
    assert upload_key_for(args, tmp_path)
    assert oct(f.stat().st_mode & 0o777) == "0o600" and not f.with_suffix(".tmp").exists()
    assert upload_key_for(args, tmp_path) == f.read_text().strip()


@pytest.mark.parametrize("caption", ["a picture from grandma", "taken from the beach", "sunset from my window",
                                     "next to my house", "love this one", "Photo by the lake, from Jo"])
def test_ordinary_captions_are_not_options(caption):
    o = parse_options(caption)
    assert (o.given, o.first, o.when) == (False, 0, "now"), caption


def test_real_options_still_work_and_typos_are_still_caught():
    assert parse_options("from grandma's, from 300").first == 300
    assert parse_options("from shading").first == 270
    assert parse_options("next").when == "next" and parse_options("sent from the beach next").when == "next"
    for typo in ("from shadng", "from linwork", "from finishin"):
        with pytest.raises(BadRequest):
            parse_options(typo)


def test_a_picture_that_failed_is_tried_again_later_and_only_reported_once(lib, tmp_path):
    make_png(tmp_path / "pics" / "a.png")
    (h,) = lib.scan()
    lib.failed.add(h)
    lib.failed_at[h] = 1000.0
    lib.failures[h] = "disk full"
    lib.retry_failed(now=1000.0 + lib.RETRY_AFTER - 1)
    assert not lib.usable(h)
    lib.retry_failed(now=1000.0 + lib.RETRY_AFTER)
    assert lib.usable(h) and h in lib.unbaked() and lib.failures[h] == "disk full"
