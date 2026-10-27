# -*- coding: utf-8 -*-
"""P0 测试基线：钉住 sync_all 的核心校验与成败判定行为。

背景：本次要把 sync_one() 的成败判定从"文件数增减（delta）"改为
"文件名集合差集"。改动前先给现有行为建一组回归测试，改完再跑一遍：
- 通过 -> 说明改动没有破坏既有语义；
- 失败 -> 立即暴露回归，不至于像上次那样靠 CI 变红才发现。

覆盖重点（都是备份工具里"错了会静默出错"的地方）：
  1. parse_index   ：必须拒绝 0/负数/越界，否则"输入 0 选中最后一个"
  2. valid_apple_id：Apple ID 会进 argv 与 schtasks 命令串，需挡住注入字符
  3. safe_folder   ：空串=直接放备份根目录（老配置语义），None 才回退默认名
  4. count_files   ：遍历失败必须返回 None，绝不能返回部分计数
  5. sync_one      ：delta>0 / ==0 / <0 与 icloudpd 异常码 的判定
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sync_all  # noqa: E402


# ---------------------------------------------------------------- parse_index
def test_parse_index_converts_one_based_to_zero_based():
    assert sync_all.parse_index("1", 3) == 0
    assert sync_all.parse_index("3", 3) == 2


def test_parse_index_rejects_zero():
    # 核心：int("0")-1 == -1，Python 会把它当成最后一个元素，
    # 会变成"输入 0 却删掉/选中最后一个账号"的静默误操作。
    assert sync_all.parse_index("0", 3) is None


def test_parse_index_rejects_negative():
    assert sync_all.parse_index("-1", 3) is None
    assert sync_all.parse_index("-2", 3) is None


def test_parse_index_rejects_out_of_range():
    assert sync_all.parse_index("4", 3) is None
    assert sync_all.parse_index("999", 3) is None


def test_parse_index_rejects_non_numeric():
    assert sync_all.parse_index("abc", 3) is None
    assert sync_all.parse_index("", 3) is None
    assert sync_all.parse_index("  ", 3) is None


def test_parse_index_tolerates_whitespace():
    assert sync_all.parse_index(" 2 ", 3) == 1


# ------------------------------------------------------------ valid_apple_id
def test_valid_apple_id_accepts_normal_emails():
    assert sync_all.valid_apple_id("a@b.com") is True
    assert sync_all.valid_apple_id("769739689@qq.com") is True


@pytest.mark.parametrize("bad", [
    "",                 # 空
    "abc",              # 没有 @
    "a@b@c.com",        # 多个 @
    "-a@b.com",         # 以 - 开头，会被 argparse 当选项
    "a b@c.com",        # 空格
    "a\tb@c.com",       # 制表符
    'a"b@c.com',        # 双引号，破坏 schtasks 引号嵌套
    "a'b@c.com",        # 单引号
    "a\\b@c.com",       # 反斜杠
    "a;b@c.com",        # 命令分隔
    "a|b@c.com",        # 管道
    "a&b@c.com",        # 后台执行
    " a@b.com",         # 前后空白
    "a@b.com ",
])
def test_valid_apple_id_rejects_injection_chars(bad):
    assert sync_all.valid_apple_id(bad) is False


# ----------------------------------------------------------- sanitize_folder
def test_sanitize_folder_strips_at_and_dot():
    assert sync_all.sanitize_folder("a@b.com") == "abcom"


def test_sanitize_folder_lowercases():
    assert sync_all.sanitize_folder("A@B.COM") == "abcom"


def test_sanitize_folder_replaces_unsafe_chars():
    assert sync_all.sanitize_folder("a b!@c") == "a_b_c"


# --------------------------------------------------------------- safe_folder
def test_safe_folder_empty_string_keeps_backup_root_semantics():
    # 关键语义：folder 为空串 = 照片直接放备份根目录（老配置常见写法）。
    # 若被"补默认值"替换成子文件夹名，会导致全部照片被重新下载到新目录。
    assert sync_all.safe_folder("", "someone@example.com") == ""


def test_safe_folder_whitespace_only_becomes_empty():
    assert sync_all.safe_folder("   ", "someone@example.com") == ""


def test_safe_folder_none_falls_back_to_default_name():
    # 只有键缺失（None）才回退为默认子文件夹名
    assert sync_all.safe_folder(None, "someone@example.com") == "someoneexamplecom"


def test_safe_folder_accepts_relative_nested_path():
    assert sync_all.safe_folder("a/b", "someone@example.com") == os.path.join("a", "b")


def test_safe_folder_rejects_windows_absolute_path():
    # Windows 绝对路径必须回退（防止照片写到备份根目录之外）
    assert sync_all.safe_folder("C:\\Windows", "someone@example.com") == "someoneexamplecom"


def test_safe_folder_rejects_posix_absolute_path():
    # 期望：POSIX 绝对路径同样应回退为默认子文件夹名
    assert sync_all.safe_folder("/etc/passwd", "someone@example.com") == "someoneexamplecom"


def test_safe_folder_rejects_parent_traversal():
    assert sync_all.safe_folder("../evil", "someone@example.com") == "someoneexamplecom"
    assert sync_all.safe_folder("a/../b", "someone@example.com") == "someoneexamplecom"


# --------------------------------------------------------------- count_files
def test_count_files_counts_files_and_bytes_recursively(tmp_path):
    (tmp_path / "a.txt").write_text("12345")      # 5 字节
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("12")             # 2 字节
    assert sync_all.count_files(str(tmp_path)) == (2, 7)


def test_count_files_empty_directory(tmp_path):
    assert sync_all.count_files(str(tmp_path)) == (0, 0)


def test_count_files_missing_directory_returns_none(tmp_path):
    # 目录丢失不能当成空目录，否则"失败"会被算成"有新照片"
    assert sync_all.count_files(str(tmp_path / "nope")) is None


# ----------------------------------------------------------------- list_files
def test_list_files_returns_relative_paths(tmp_path):
    (tmp_path / "a.txt").write_text("1")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("12")
    assert sync_all.list_files(str(tmp_path)) == {"a.txt", os.path.join("sub", "b.txt")}


def test_list_files_empty_directory(tmp_path):
    assert sync_all.list_files(str(tmp_path)) == set()


def test_list_files_missing_directory_returns_none(tmp_path):
    assert sync_all.list_files(str(tmp_path / "nope")) is None


# ---------------------------------------------------------- account_directory
def test_account_directory_with_folder():
    assert sync_all.account_directory(
        {"backup_root": "/root"}, {"folder": "sub"}) == os.path.join("/root", "sub")


def test_account_directory_empty_folder_uses_backup_root():
    assert sync_all.account_directory({"backup_root": "/root"}, {"folder": ""}) == "/root"


# ------------------------------------------------------------------- base_cmd
def test_base_cmd_includes_domain_when_set():
    cmd = sync_all.base_cmd({"tool": "icloudpd", "domain": "cn", "cookie_dir": "/ck"},
                            "a@b.com", "/dir")
    assert "--domain" in cmd and "cn" in cmd


def test_base_cmd_omits_domain_when_empty():
    # 空 domain 是合法值（非中国大陆，交给 icloudpd 默认区域），不能被写回 cn
    cmd = sync_all.base_cmd({"tool": "icloudpd", "domain": "", "cookie_dir": "/ck"},
                            "a@b.com", "/dir")
    assert "--domain" not in cmd


# -------------------------------------------------- sync_one：成败判定（核心）
def _cfg(root):
    return {"settings": {"tool": "fake-tool",
                         "cookie_dir": os.path.join(str(root), "ck"),
                         "backup_root": str(root),
                         "domain": "cn"}}


def _acc(folder=""):
    return {"id": "account_ci", "apple_id": "ci@example.com", "folder": folder}


def _isolate_logs(monkeypatch):
    """测试期间停掉日志输出，避免污染真实 logs/sync.log。

    注意：不要改成"把 LOGDIR 指到被统计的目录"——那样 log() 写入的
    sync.log 会被 count_files 数进去，使 delta 判定失真（曾导致
    "文件减少"用例误判为成功）。
    """
    monkeypatch.setattr(sync_all, "log", lambda *a, **k: None)


def test_sync_one_ok_when_files_added(tmp_path, monkeypatch):
    _isolate_logs(monkeypatch)

    def fake_run(cmd, interactive):
        (tmp_path / "new.jpg").write_bytes(b"x" * 10)
        return 1.0, 0

    monkeypatch.setattr(sync_all, "run_icloudpd", fake_run)
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_OK


def test_sync_one_ok_when_already_up_to_date(tmp_path, monkeypatch):
    """重复点全量下载：delta==0 必须判成功，不能误报失败。"""
    _isolate_logs(monkeypatch)
    monkeypatch.setattr(sync_all, "run_icloudpd", lambda c, i: (1.0, 0))
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_OK


def test_sync_one_fail_when_files_vanish_during_run(tmp_path, monkeypatch):
    """运行期间文件数减少 -> 必须判失败。"""
    _isolate_logs(monkeypatch)
    (tmp_path / "gone.jpg").write_bytes(b"x" * 10)

    def fake_run(cmd, interactive):
        os.remove(os.path.join(str(tmp_path), "gone.jpg"))
        return 1.0, 0

    monkeypatch.setattr(sync_all, "run_icloudpd", fake_run)
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_FAIL


def test_sync_one_detects_removal_even_when_total_unchanged(tmp_path, monkeypatch):
    """集合差集的核心价值：文件总数不变时，仍能发现"有文件消失"。

    旧逻辑只比较文件总数：少了 1 个又多了 1 个，delta==0 会被判成功（假成功）。
    新逻辑用集合差集，同时得到"新增"与"消失"两个集合，此处应判失败。
    """
    _isolate_logs(monkeypatch)
    (tmp_path / "old.jpg").write_bytes(b"x" * 10)

    def fake_run(cmd, interactive):
        os.remove(os.path.join(str(tmp_path), "old.jpg"))
        (tmp_path / "new.jpg").write_bytes(b"y" * 10)
        return 1.0, 0

    monkeypatch.setattr(sync_all, "run_icloudpd", fake_run)
    # 总数前后都是 1，但内容变了：old.jpg 消失、new.jpg 新增 -> 必须失败
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_FAIL


def test_sync_one_fail_when_icloudpd_broken(tmp_path, monkeypatch):
    """找不到 icloudpd / 超时 / 异常：即使 delta>0 也必须判失败。"""
    _isolate_logs(monkeypatch)

    def fake_run(cmd, interactive):
        (tmp_path / "new.jpg").write_bytes(b"x" * 10)
        return 1.0, -1

    monkeypatch.setattr(sync_all, "run_icloudpd", fake_run)
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_FAIL


def test_sync_one_fail_when_before_listing_fails(tmp_path, monkeypatch):
    """同步前"列出文件"失败（返回 None）-> 不能拿来判定成败，必须判失败。"""
    _isolate_logs(monkeypatch)
    monkeypatch.setattr(sync_all, "run_icloudpd", lambda c, i: (1.0, 0))
    monkeypatch.setattr(sync_all, "list_files", lambda root: None)
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_FAIL


def test_sync_one_fail_when_after_listing_fails(tmp_path, monkeypatch):
    """同步后"列出文件"失败 -> 同样必须判失败。"""
    _isolate_logs(monkeypatch)
    calls = {"n": 0}

    def fake_list(root):
        calls["n"] += 1
        return None if calls["n"] > 1 else set()

    monkeypatch.setattr(sync_all, "run_icloudpd", lambda c, i: (1.0, 0))
    monkeypatch.setattr(sync_all, "list_files", fake_list)
    assert sync_all.sync_one(_acc(), _cfg(tmp_path), "full", False) == sync_all.EXIT_FAIL


def test_incremental_mode_adds_until_found_flag(tmp_path, monkeypatch):
    """增量模式必须带 --until-found，且值来自常量（不能写死）。

    目录用 pytest 的 tmp_path（可写、干净），不要直接统计系统 /tmp：
    否则在 Linux 上 /tmp 含不可读的系统子目录，list_files 会抛
    PermissionError 而提前返回，桩函数不被调用导致断言失败（CI 变红）。
    """
    seen = {}

    def fake_run(cmd, interactive):
        seen["cmd"] = cmd
        return 1.0, 0

    monkeypatch.setattr(sync_all, "run_icloudpd", fake_run)
    sync_all.sync_one(_acc(), _cfg(tmp_path), "incremental", False)
    cmd = seen["cmd"]
    assert "--until-found" in cmd
    assert sync_all.INCREMENTAL_UNTIL in cmd


# ------------------------------------------------------- 编码修复（回归保护）
def test_source_has_utf8_stdout_guard():
    """保护 Windows CI 修复：脚本顶部须有 stdout/stderr 的 utf-8 兜底。

    该修复解决：en-US Windows runner 管道 stdout 为 cp1252 时，
    打印中文菜单触发 UnicodeEncodeError 导致进程崩溃（CI 全红）。
    """
    src = open(os.path.join(ROOT, "sync_all.py"), encoding="utf-8").read()
    head = src[:1200]   # 只看文件开头，修复必须在这里才来得及生效
    assert "reconfigure" in head, "顶部缺少 sys.stdout.reconfigure 兜底"
    assert 'encoding="utf-8"' in head


def test_menu_exits_zero_under_cp1252_stdout():
    """端到端回归：管道 stdout 强制为 cp1252 时，菜单仍须正常退 0。"""
    import subprocess

    env = dict(os.environ, PYTHONIOENCODING="cp1252")
    p = subprocess.run([sys.executable, "sync_all.py"], cwd=ROOT,
                       input=b"8\n", capture_output=True, env=env, timeout=180)
    err = p.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in err, err
    assert p.returncode == 0, err
