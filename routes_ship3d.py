"""선박 3D — 도면 기반 3D 모델(glb) 뷰어.

모델은 회사 도면에서 맥 데쿠가 Blender로 생성한다. 저장소가 공개이므로 glb는 git에 넣지 않고
서버 `instance/ship3d/`에만 둔다(배포 zip에 포함되지 않음). 열람·다운로드는 소유자 세션만.
"""
import json
import os
import re

from flask import Blueprint, abort, render_template, request, send_from_directory, session

from app_core import INSTANCE_DIR
from helpers_shared import login_required

bp = Blueprint('routes_ship3d', __name__)

OWNER_USERNAME = os.environ.get('JOURNAL_OWNER_USERNAME', 'SS0094')
SHIP3D_DIR = os.path.join(INSTANCE_DIR, 'ship3d')
_NAME = re.compile(r'^[a-z0-9][a-z0-9_-]{0,60}$')


def _owner_ok():
    return session.get('username') == OWNER_USERNAME


def _models():
    """instance/ship3d/<slug>.glb + 선택적 <slug>.json({"title","note"}) 목록."""
    out = []
    try:
        names = sorted(os.listdir(SHIP3D_DIR))
    except OSError:
        return out
    for fn in names:
        slug, ext = os.path.splitext(fn)
        if ext != '.glb' or not _NAME.match(slug):
            continue
        meta = {}
        try:
            with open(os.path.join(SHIP3D_DIR, slug + '.json'), encoding='utf-8') as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
        if not isinstance(meta, dict):      # null·배열·문자열 메타로 탭 전체가 500 나지 않게
            meta = {}
        out.append({'slug': slug, 'title': str(meta.get('title') or slug)[:80],
                    'note': str(meta.get('note') or '')[:300]})
    return out


@bp.after_request
def _no_store(resp):
    if request.path.startswith('/ship3d'):
        resp.headers['Cache-Control'] = 'no-store, private'
    return resp


@bp.route('/ship3d')
@login_required
def ship3d_page():
    if not _owner_ok():
        abort(404)
    models = _models()
    cur = request.args.get('m') or (models[0]['slug'] if models else '')
    if cur and not any(m['slug'] == cur for m in models):
        cur = models[0]['slug'] if models else ''
    return render_template('ship3d.html', models=models, current=cur)


@bp.route('/ship3d/model/<slug>.glb')
@login_required
def ship3d_model(slug):
    if not _owner_ok() or not _NAME.match(slug):
        abort(404)
    if not os.path.isfile(os.path.join(SHIP3D_DIR, slug + '.glb')):
        abort(404)
    return send_from_directory(SHIP3D_DIR, slug + '.glb', mimetype='model/gltf-binary')
