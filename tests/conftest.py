# -*- coding: utf-8 -*-
"""让 tests/ 下的用例能导入仓库根目录的 sync_all.py。

仓库里 sync_all 位于根目录，而 pytest 默认把 tests/ 目录放上 sys.path，
这里补一行把仓库根也加进去，保证 `import sync_all` 成功。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
