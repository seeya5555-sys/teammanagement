#!/usr/bin/env python3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
routes = (root / 'routes_core.py').read_text()
base = (root / 'templates/base.html').read_text()
shell = (root / 'templates/dock_manager.html').read_text()

assert "@bp.route('/dock-manager')" in routes
assert '@admin_required\ndef dock_manager_shell' in routes
assert base.count("routes_core.dock_manager_shell") >= 4
assert 'src="/drydock/?embedded=1"' in shell
assert "frame-src 'self'; frame-ancestors 'self'" in routes
print('✅ Dock Manager TRMT shell contract pass')
