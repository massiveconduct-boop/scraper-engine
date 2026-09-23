# tests/unit/test_browser_rss.py
"""core/browser_rss.py — what a live browser really costs (round 67).

No real browser here: psutil is replaced by a fake process tree, because what
is under test is the walk (which processes count as one browser, whose memory
is summed), not psutil itself.
"""

from __future__ import annotations

import sys
import types

import pytest

from scraper_engine.core.browser_rss import sample_browser_rss

MB = 1024 * 1024


class _FakeProc:
    def __init__(self, pid, name, rss_mb, parent=None, raises=None):
        self.pid = pid
        self._name = name
        self._rss = int(rss_mb * MB)
        self._parent = parent
        self._raises = raises

    def name(self):
        if self._raises == "name":
            raise RuntimeError("process gone")
        return self._name

    def parent(self):
        return self._parent

    def memory_info(self):
        if self._raises == "rss":
            raise RuntimeError("process gone")
        return types.SimpleNamespace(rss=self._rss)


def _install_psutil(monkeypatch, children, raises=False):
    class _Process:
        def __init__(self, *_args):
            pass

        def children(self, recursive=False):
            if raises:
                raise RuntimeError("no such process")
            return children

    monkeypatch.setitem(sys.modules, "psutil", types.SimpleNamespace(Process=_Process))


def test_a_browser_and_its_renderers_are_one_browser(monkeypatch):
    browser = _FakeProc(2, "camoufox", 500)
    renderer = _FakeProc(3, "chrome-renderer", 200, parent=browser)
    _install_psutil(monkeypatch, [browser, renderer])

    sample = sample_browser_rss()

    assert sample is not None
    assert sample.count == 1
    assert sample.mean_mb == pytest.approx(700, rel=0.01)


def test_two_browsers_are_averaged(monkeypatch):
    _install_psutil(
        monkeypatch, [_FakeProc(2, "firefox", 400), _FakeProc(3, "chromium", 800)]
    )

    sample = sample_browser_rss()

    assert sample is not None
    assert sample.count == 2
    assert sample.mean_mb == pytest.approx(600, rel=0.01)


def test_a_browser_whose_parent_is_not_a_browser_is_its_own_root(monkeypatch):
    """Xvfb and shells in between are not browsers, so the browser under them
    still counts as one."""
    xvfb = _FakeProc(2, "Xvfb", 50)
    browser = _FakeProc(3, "chrome", 900, parent=xvfb)
    _install_psutil(monkeypatch, [xvfb, browser])

    sample = sample_browser_rss()

    assert sample is not None
    assert sample.count == 1
    assert sample.mean_mb == pytest.approx(900, rel=0.01)


def test_a_child_of_a_browser_outside_this_process_tree_counts_as_a_root(monkeypatch):
    other_tree_parent = _FakeProc(99, "chrome", 100)
    browser = _FakeProc(3, "chrome", 600, parent=other_tree_parent)
    _install_psutil(monkeypatch, [browser])

    sample = sample_browser_rss()

    assert sample is not None
    assert sample.count == 1


def test_no_browsers_is_nothing_to_report(monkeypatch):
    _install_psutil(monkeypatch, [_FakeProc(2, "python", 100)])
    assert sample_browser_rss() is None


def test_a_process_that_exits_mid_walk_is_skipped(monkeypatch):
    _install_psutil(
        monkeypatch,
        [
            _FakeProc(2, "chrome", 700),
            _FakeProc(3, "chrome", 700, raises="name"),
            _FakeProc(4, "chrome", 700, raises="rss"),
        ],
    )

    sample = sample_browser_rss()

    assert sample is not None
    assert sample.count == 1
    assert sample.mean_mb == pytest.approx(700, rel=0.01)


def test_an_unreadable_process_tree_is_not_a_measurement(monkeypatch):
    _install_psutil(monkeypatch, [], raises=True)
    assert sample_browser_rss() is None
