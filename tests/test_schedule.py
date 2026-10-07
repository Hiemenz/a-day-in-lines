import datetime as dt

from day_in_lines.constants import TOTAL
from day_in_lines.schedule import (Overrides, drawing_day, next_start, override_end, parse_end, progress,
                                   window)

D = dt.datetime
T = dt.time
DAY = dt.timedelta(minutes=TOTAL)


class Lib:
    """Stands in for the library: every picture is usable."""
    def usable(self, h):
        return True


def test_window_default_is_twelve_hours():
    assert window(T(7), None) == DAY


def test_window_start_to_end_including_midnight():
    assert window(T(7), T(19)) == dt.timedelta(hours=12)
    assert window(T(22), T(6)) == dt.timedelta(hours=8)
    assert window(T(7), T(7)) == dt.timedelta(days=1)


def test_parse_end():
    assert parse_end(None) is None
    assert parse_end("18:30") == T(18, 30)


def test_progress_spreads_events_evenly():
    began = D(2026, 3, 10, 7)
    assert progress(began - dt.timedelta(minutes=5), began, DAY) == 0
    assert progress(began + DAY / 2, began, DAY) == TOTAL // 2
    assert progress(began + DAY * 3, began, DAY) == TOTAL
    short = dt.timedelta(hours=6)                                  # 720 events in 6 h: two a minute
    assert progress(began + dt.timedelta(minutes=30), began, short) == 60


def test_before_the_start_it_is_still_yesterdays_drawing():
    day, k = drawing_day(D(2026, 3, 10, 6, 0), T(7), DAY)
    assert (day, k) == (dt.date(2026, 3, 9), TOTAL)


def test_a_drawing_that_runs_past_midnight():
    dur = window(T(22), T(6))
    assert drawing_day(D(2026, 3, 10, 2, 0), T(22), dur) == (dt.date(2026, 3, 9), TOTAL // 2)
    assert drawing_day(D(2026, 3, 10, 22, 0), T(22), dur) == (dt.date(2026, 3, 10), 0)


def test_next_start():
    assert next_start(D(2026, 3, 10, 6), T(7)) == D(2026, 3, 10, 7)
    assert next_start(D(2026, 3, 10, 7), T(7)) == D(2026, 3, 10, 7)
    assert next_start(D(2026, 3, 10, 8), T(7)) == D(2026, 3, 11, 7)


def test_override_stays_up_until_next_start_unless_it_finished_early():
    assert override_end(D(2026, 3, 9, 20), D(2026, 3, 10, 8), T(7), DAY) == D(2026, 3, 10, 8)      # finished 1 h in
    assert override_end(D(2026, 3, 10, 8), D(2026, 3, 10, 20), T(7), DAY) == D(2026, 3, 11, 7)    # near the day's end


def make(tmp_path):
    return Overrides(tmp_path, T(7), DAY)


def test_queued_upload_waits_for_the_sheet_to_be_free(tmp_path):
    ov = make(tmp_path)
    ov.add("a", D(2026, 3, 10, 8), D(2026, 3, 10, 20), fixed=True)
    start, end = ov.add("b", D(2026, 3, 10, 9), D(2026, 3, 10, 21), fixed=False)
    assert start == D(2026, 3, 11, 7)
    assert end - start == dt.timedelta(hours=12)                    # keeps its length


def test_a_later_fixed_upload_takes_over(tmp_path):
    ov = make(tmp_path)
    ov.add("a", D(2026, 3, 10, 8), D(2026, 3, 10, 20), fixed=True)
    ov.add("c", D(2026, 3, 10, 12), D(2026, 3, 10, 14), fixed=True)
    assert ov.current(D(2026, 3, 10, 10), Lib())[0] == "a"
    assert ov.current(D(2026, 3, 10, 13), Lib())[0] == "c"
    assert ov.hashes() == {"c"}                      # a was cut short and dropped


def test_current_drops_finished_uploads(tmp_path):
    ov = make(tmp_path)
    ov.add("d", D(2026, 3, 10, 7, 30), D(2026, 3, 10, 9), fixed=True)   # early finish: hands back at 09:00
    assert ov.current(D(2026, 3, 10, 8), Lib())[0] == "d"
    assert ov.current(D(2026, 3, 10, 9, 30), Lib()) is None
    assert ov.hashes() == set()


def test_queued_uploads_chain_one_after_another(tmp_path):
    ov = make(tmp_path)
    first, _ = ov.add("a", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    second, _ = ov.add("b", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    third, _ = ov.add("c", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    assert (second - first, third - second) == (dt.timedelta(days=1),) * 2
    assert ov.hashes() == {"a", "b", "c"}


def test_corrupt_override_file_is_ignored(tmp_path):
    (tmp_path / "override.json").write_text("{not json")
    assert make(tmp_path).current(D(2026, 3, 10, 8), Lib()) is None


def test_a_new_queued_upload_never_bumps_one_already_promised_a_time(tmp_path):
    ov = make(tmp_path)
    ov.add("a", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    promised, _ = ov.add("b", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    ov.add("c", D(2026, 3, 10, 7), D(2026, 3, 10, 19), fixed=False)
    b = next(o for o in ov._load() if o["hash"] == "b")
    assert dt.datetime.fromisoformat(b["start"]) == promised
