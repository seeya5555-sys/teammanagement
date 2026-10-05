"""_contain_in_static 단일 정본(dock_report_docx) 경로순회 가드 회귀."""
import os

import boarding_report_docx
import dock_report_docx
from app_core import app


def test_boarding_reuses_dock_helper():
    assert boarding_report_docx._contain_in_static is dock_report_docx._contain_in_static


def test_rejects_traversal_and_accepts_inside():
    root = os.path.realpath(app.static_folder)
    f = dock_report_docx._contain_in_static
    assert f(os.path.join(root, '..', 'app.py')) is None
    assert f(os.path.join(root, 'js', 'app.js')) == os.path.join(root, 'js', 'app.js')
    assert f(root) == root
    assert f(root + '_evil') is None
