import argparse
import subprocess
import sys
from pathlib import Path

from PIL import Image

from day_in_lines.cli import check, gamma_of, upload_key_for
from conftest import make_png

ROOT = Path(__file__).resolve().parent.parent


def ns(**kw):
    return argparse.Namespace(**{"open_uploads": False, "upload_key": None, **kw})


def test_upload_key_is_made_once_and_kept(tmp_path):
    first = upload_key_for(ns(), tmp_path)
    assert first and len(first) >= 10
    assert upload_key_for(ns(), tmp_path) == first
    assert oct((tmp_path / "upload_key").stat().st_mode & 0o777) == "0o600"


def test_explicit_key_and_open_uploads(tmp_path):
    assert upload_key_for(ns(upload_key="mine"), tmp_path) == "mine"
    assert upload_key_for(ns(open_uploads=True, upload_key="mine"), tmp_path) is None
    assert not (tmp_path / "upload_key").exists()


def test_check_writes_a_test_card_with_the_file_display(tmp_path, capsys):
    out = tmp_path / "card.png"
    check(argparse.Namespace(display="file", out=str(out), size="400x240", gamma=2.4))
    assert Image.open(out).size == (400, 240)
    assert "ready" in capsys.readouterr().out


def run_cli(*args):
    return subprocess.run([sys.executable, str(ROOT / "a_day_in_lines.py"), *args], capture_output=True, text=True)


def test_launcher_requires_an_image_except_for_check(tmp_path):
    assert run_cli("plan").returncode != 0
    assert run_cli("check", "--out", str(tmp_path / "c.png"), "--size", "100x60").returncode == 0


def test_out_of_range_look_is_rejected():
    r = run_cli("--image", "x.png", "--detail", "9", "plan")
    assert r.returncode == 2 and "between 0.5 and 2" in r.stderr


def test_unknown_display_is_a_clear_error():
    r = run_cli("check", "--display", "magic")
    assert r.returncode != 0 and "unknown display" in r.stderr


def test_gamma_defaults_follow_the_medium_unless_given():
    assert gamma_of(argparse.Namespace(medium="pencil", gamma=None)) == 2.4
    assert gamma_of(argparse.Namespace(medium="paint", gamma=None)) == 1.6
    assert gamma_of(argparse.Namespace(medium="paint", gamma=2.0)) == 2.0
    assert gamma_of(argparse.Namespace(gamma=None)) == 2.4


def test_unknown_medium_is_rejected():
    r = run_cli("--image", "x.png", "--medium", "oil", "plan")
    assert r.returncode == 2 and "invalid choice" in r.stderr


def test_a_painting_frame_from_the_command_line(tmp_path):
    img = make_png(tmp_path / "p.png", (300, 180))
    out = tmp_path / "f.png"
    r = run_cli("--image", str(img), "--medium", "paint", "--seed", "1", "frame", "40", "-o", str(out), "--mono")
    assert r.returncode == 0, r.stderr
    assert Image.open(out).mode == "1"
