# -*- coding: utf-8 -*-
"""Windows 计划任务安装助手（纯本地，不依赖 GitHub Actions / 自托管 runner）。

本脚本只在 Windows 上创建“任务计划程序”条目，直接调用系统的 schtasks 命令，
与 GitHub 仓库、CI、自托管 runner 完全无关，可在一台普通 Windows 电脑上离线使用。

用法：
    # 创建每日 21:00 的增量同步任务（任务名 iCloudPhotoSyncDaily）
    python install_scheduler.py --daily 21:00

    # 创建一次性全量下载任务（例如今晚 23:30，指定账号）
    python install_scheduler.py --once 23:30 --accounts you@example.com

    # 查看已创建的任务
    python install_scheduler.py --show

    # 删除本工具创建的任务
    python install_scheduler.py --remove

任务实际执行的命令是 `python sync_all.py --sync`（每日）或
`python sync_all.py --full --accounts ...`（一次性），无需人工输入。
"""
import argparse
import sys

import sync_all as app


def _show_tasks():
    for name in (app.TASK_DAILY, app.TASK_ONCE):
        rc, out, err = app.run_schtasks(["schtasks", "/query", "/tn", name])
        if rc == 0:
            print("任务存在：%s" % name)
            if out.strip():
                print(out.strip())
        else:
            print("无任务：%s" % name)


def _remove_tasks():
    for name in (app.TASK_DAILY, app.TASK_ONCE):
        rc, out, err = app.run_schtasks(
            ["schtasks", "/delete", "/tn", name, "/f"])
        if rc == 0:
            print("已删除任务：%s" % name)
        else:
            print("删除失败或不存在：%s（%s）" % (name, (out or err).strip()))


def main():
    parser = argparse.ArgumentParser(
        prog="install_scheduler.py",
        description="在 Windows 任务计划程序中安装 iCloud 照片同步任务（纯本地，无需 GitHub）。")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--daily", metavar="HH:MM",
                   help="创建每日定时增量同步任务（如 21:00）")
    g.add_argument("--once", metavar="HH:MM",
                   help="创建一次性全量下载任务（配合 --accounts 指定账号）")
    g.add_argument("--show", action="store_true", help="查看已创建的任务")
    g.add_argument("--remove", action="store_true", help="删除本工具创建的任务")
    parser.add_argument("--accounts",
                        help="配合 --once：逗号分隔的账号列表，如 a@b.com,b@b.com")
    args = parser.parse_args()

    if args.show:
        _show_tasks()
        return 0
    if args.remove:
        _remove_tasks()
        return 0
    if args.daily:
        ok, hhmm = app.parse_time(args.daily)
        if not ok:
            print("时间格式不对：%s（应为 HH:MM）" % args.daily)
            return 2
        app.schedule_daily(hhmm)
        return 0
    if args.once:
        ok, hhmm = app.parse_time(args.once)
        if not ok:
            print("时间格式不对：%s（应为 HH:MM）" % args.once)
            return 2
        accounts = [a.strip() for a in (args.accounts or "").split(",") if a.strip()]
        if not accounts:
            print("请配合 --once 使用 --accounts，例如 --accounts you@example.com")
            return 2
        app.schedule_once(hhmm, accounts)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
