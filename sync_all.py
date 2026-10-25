# -*- coding: utf-8 -*-
"""iCloud 照片同步工具（多账号 / 可配置 / 可定时）

把 iPhone 等设备的 iCloud 照片库，按 年/月 自动同步到本机，支持多账号，
每个账号单独一个文件夹。底层使用第三方下载器 icloudpd。

日常使用：
    python sync_all.py            # 打开中文菜单，按需操作
后台（由定时任务调用，无需人工）：
    python sync_all.py --sync     # 增量同步（每个账号已下载满 50 张即停）
    python sync_all.py --full     # 全量下载（可选 --accounts a,b,c 指定账号）

设计说明：
    - 账号、备份地址等都写在 config.ini 里，可随时改。
    - 密码不在本工具保存。登录时在终端输入密码+手机验证码，
      icloudpd 把登录凭证(cookie)存到 cookie_dir，之后同步无需密码。
    - “手机删除、电脑跟着删”功能本期未实现，留待后续版本。
"""
import os
import sys
import time
import subprocess
import datetime
import configparser

BASE = os.path.dirname(os.path.abspath(__file__))   # 仓库根目录
PROJECT = BASE
CONFIG = os.path.join(BASE, "config.ini")
LOGDIR = os.path.join(BASE, "logs")

# 通用默认配置（首次运行自动写入 config.ini）
DEFAULT_BACKUP = os.path.expanduser("~/iCloudPhotos")
DEFAULT_COOKIE_DIR = os.path.expanduser("~/.icloud_session")
DEFAULT_TOOL = "icloudpd"        # 假设已加入 PATH；否则填完整路径
DEFAULT_DOMAIN = "cn"            # 中国大陆填 cn；其他地区留空

TASK_DAILY = "iCloudPhotoSyncDaily"
TASK_ONCE = "iCloudPhotoSyncOnce"
INCREMENTAL_UNTIL = "50"          # 增量同步：连续 50 张已下载即停，省时间


def log(msg):
    line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + "  " + str(msg)
    try:
        print(line)
        sys.stdout.flush()
    except Exception:
        pass
    try:
        if not os.path.isdir(LOGDIR):
            os.makedirs(LOGDIR)
        with open(os.path.join(LOGDIR, "sync.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def sanitize_folder(apple_id):
    """把邮箱变成可用的文件夹名，例如 a@b.com -> abcom"""
    s = apple_id.strip().lower().replace("@", "").replace(".", "")
    return "".join(ch if ch.isalnum() else "_" for ch in s)


def load_config():
    cfg = configparser.ConfigParser()
    if not os.path.isfile(CONFIG):
        cfg.add_section("settings")
        cfg["settings"]["backup_root"] = DEFAULT_BACKUP
        cfg["settings"]["cookie_dir"] = DEFAULT_COOKIE_DIR
        cfg["settings"]["tool"] = DEFAULT_TOOL
        cfg["settings"]["domain"] = DEFAULT_DOMAIN
        save_config(cfg)
    cfg.read(CONFIG, encoding="utf-8")
    if not cfg.has_section("settings"):
        cfg.add_section("settings")
    s = cfg["settings"]
    if not s.get("backup_root"):
        s["backup_root"] = DEFAULT_BACKUP
    if not s.get("cookie_dir"):
        s["cookie_dir"] = DEFAULT_COOKIE_DIR
    if not s.get("tool"):
        s["tool"] = DEFAULT_TOOL
    if "domain" not in s:
        s["domain"] = DEFAULT_DOMAIN
    return cfg


def save_config(cfg):
    try:
        if not os.path.isdir(PROJECT):
            os.makedirs(PROJECT)
        with open(CONFIG, "w", encoding="utf-8") as f:
            f.write("# iCloud 照片同步 配置文件（UTF-8）\n")
            f.write("# backup_root : 照片备份根目录；每个账号会在其下建独立子文件夹\n")
            f.write("#               （若某账号 folder 为空，则直接放备份根目录）\n")
            f.write("# cookie_dir  : 登录凭证目录，请勿手动删改\n")
            f.write("# tool        : icloudpd 可执行文件路径（已在 PATH 中可直接写 icloudpd）\n")
            f.write("# domain      : iCloud 区域，中国大陆填 cn，其他地区留空\n")
            f.write("# 账号小节 [account_xxxx] 含 apple_id 与 folder 两项\n\n")
            cfg.write(f)
        log("配置已保存：" + CONFIG)
    except Exception as e:
        log("保存配置失败：" + str(e))


def get_accounts(cfg):
    accs = []
    for sec in cfg.sections():
        if sec.startswith("account_"):
            apple_id = cfg[sec].get("apple_id", "")
            folder = cfg[sec].get("folder", sanitize_folder(apple_id))
            accs.append({"id": sec, "apple_id": apple_id, "folder": folder})
    return accs


def count_files(root):
    n = 0
    size = 0
    try:
        for _r, _d, files in os.walk(root):
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(_r, f))
                except OSError:
                    pass
    except Exception:
        pass
    return n, size


def run_icloudpd(cmd, interactive):
    t0 = time.time()
    code = 0
    try:
        if interactive:
            code = subprocess.call(cmd)
        else:
            # 后台运行：断开输入，避免长时间卡在登录提示；超时报错
            code = subprocess.call(cmd, stdin=subprocess.DEVNULL,
                                   timeout=6 * 3600)
    except subprocess.TimeoutExpired:
        log("超时（>6小时）被强制结束，请检查网络或缩小范围后重试。")
        code = -1
    except Exception as e:
        log("运行异常：" + str(e))
        code = -1
    elapsed = time.time() - t0
    return elapsed, code


def base_cmd(s, apple_id, directory):
    cmd = [s["tool"], "--domain", s["domain"]] if s.get("domain") else [s["tool"]]
    cmd += ["--cookie-directory", s["cookie_dir"],
            "--directory", directory,
            "--size", "original",
            "--folder-structure", "{:%Y/%m}",
            "--username", apple_id]
    return cmd


def sync_one(acc, cfg, mode, interactive):
    s = cfg["settings"]
    directory = os.path.join(s["backup_root"], acc["folder"]) if acc["folder"] else s["backup_root"]
    os.makedirs(directory, exist_ok=True)
    cmd = base_cmd(s, acc["apple_id"], directory)
    if mode == "incremental":
        cmd += ["--until-found", INCREMENTAL_UNTIL]
    # mode == "full": 不加任何限制，下载全部
    # 注意：本期未实现 --auto-delete（手机删除电脑跟着删），后续版本再加。

    before, before_sz = count_files(directory)
    log("[%s] 开始同步 模式=%s 目录=%s before=%d文件/%.1fMB" %
        (acc["apple_id"], mode, directory, before, before_sz / 1048576.0))
    elapsed, code = run_icloudpd(cmd, interactive)
    after, after_sz = count_files(directory)
    delta = after - before
    log("[%s] 结束 after=%d文件/%.1fMB 本次+%d文件(+%.1fMB) 耗时%.1f分 退出码%d" %
        (acc["apple_id"], after, after_sz / 1048576.0, delta,
         (after_sz - before_sz) / 1048576.0, elapsed / 60.0, code))
    # icloudpd 退出码不可信（参数错也返回0），以文件数是否增加为准
    if delta > 0:
        log("[%s] 结果：OK，有新照片。" % acc["apple_id"])
    elif delta == 0:
        log("[%s] 结果：无新增（可能已最新，或登录凭证已过期需重新登录）。" % acc["apple_id"])
    else:
        log("[%s] 结果：文件数减少，请检查。" % acc["apple_id"])
    return delta


def do_login(acc, cfg):
    s = cfg["settings"]
    directory = os.path.join(s["backup_root"], acc["folder"]) if acc["folder"] else s["backup_root"]
    os.makedirs(directory, exist_ok=True)
    cmd = base_cmd(s, acc["apple_id"], directory)
    cmd += ["--recent", "1"]   # 触发登录（顺便下一张），已登录则跳过
    log("开始登录账号：" + acc["apple_id"])
    log("请在提示中输入密码和手机验证码；若已登录则直接跳过。")
    try:
        subprocess.call(cmd)
    except Exception as e:
        log("登录过程出错：" + str(e))
    log("登录结束。凭证已保存到：" + s["cookie_dir"])


# ---------------- 后台模式 ----------------
def headless(mode, accounts_arg):
    cfg = load_config()
    accs = get_accounts(cfg)
    if not accs:
        log("没有任何账号，请先运行菜单添加账号。")
        return
    if accounts_arg:
        want = set(a.strip().lower() for a in accounts_arg.split(","))
        sel = [a for a in accs if a["apple_id"].lower() in want]
        if not sel:
            log("未找到指定账号：" + accounts_arg)
            return
    else:
        sel = accs
    log("=" * 56)
    log("后台同步开始  模式=%s  账号数=%d" % (mode, len(sel)))
    for acc in sel:
        sync_one(acc, cfg, mode, interactive=False)
    log("后台同步结束")
    log("=" * 56)


# ---------------- 计划任务（仅 Windows） ----------------
def run_schtasks(cmd):
    """调用 Windows 计划任务程序；若不可用则友好返回，不崩溃。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", "未找到 schtasks（计划任务程序不可用，非 Windows 或需管理员权限）。"
    except Exception as e:
        return -1, "", str(e)


def ensure_windows():
    if os.name != "nt":
        print("计划任务功能仅支持 Windows（任务计划程序）。")
        print("其他系统请用 cron 定时执行：python sync_all.py --sync")
        return False
    return True


def schedule_once(hhmm, accounts):
    if not ensure_windows():
        return
    py = sys.executable
    script = os.path.abspath(__file__)
    accarg = ",".join(accounts)
    tr = '"%s" "%s" --full --accounts %s' % (py, script, accarg)
    now = datetime.datetime.now()
    hh, mm = map(int, hhmm.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    sd = target.strftime("%Y/%m/%d")
    cmd = ["schtasks", "/create", "/tn", TASK_ONCE, "/tr", tr,
           "/sc", "once", "/sd", sd, "/st", hhmm, "/f"]
    log("创建一次性任务：%s （日期 %s 时间 %s）" % (TASK_ONCE, sd, hhmm))
    rc, out, err = run_schtasks(cmd)
    if rc == 0:
        log("已创建一次性定时任务，到点自动全量下载。")
        if out.strip():
            log(out.strip())
    else:
        log("创建失败：" + (out.strip() or err.strip()))


def schedule_daily(hhmm):
    if not ensure_windows():
        return
    py = sys.executable
    script = os.path.abspath(__file__)
    tr = '"%s" "%s" --sync' % (py, script)
    cmd = ["schtasks", "/create", "/tn", TASK_DAILY, "/tr", tr,
           "/sc", "daily", "/st", hhmm, "/f"]
    log("创建每日任务：%s 时间 %s" % (TASK_DAILY, hhmm))
    rc, out, err = run_schtasks(cmd)
    if rc == 0:
        log("已创建每日 %s 自动同步任务。" % hhmm)
        if out.strip():
            log(out.strip())
    else:
        log("创建失败：" + (out.strip() or err.strip()))


def show_tasks():
    if not ensure_windows():
        return
    for name in (TASK_DAILY, TASK_ONCE):
        rc, out, err = run_schtasks(["schtasks", "/query", "/tn", name])
        if rc == 0:
            print("任务存在：%s" % name)
            if out.strip():
                print(out.strip())
        else:
            print("无任务：%s" % name)
    c = input("是否删除这些定时任务？(y/n)：").strip().lower()
    if c == "y":
        for name in (TASK_DAILY, TASK_ONCE):
            run_schtasks(["schtasks", "/delete", "/tn", name, "/f"])
        log("已尝试删除定时任务。")


# ---------------- 菜单模式 ----------------
def menu_login(cfg):
    aid = input("请输入 Apple ID（邮箱）：").strip()
    if not aid:
        return
    sec = "account_" + sanitize_folder(aid)
    if not cfg.has_section(sec):
        cfg.add_section(sec)
    cfg[sec]["apple_id"] = aid
    default_folder = sanitize_folder(aid)
    folder = input("该账号的子文件夹名（留空则用 %s）：" % default_folder).strip()
    cfg[sec]["folder"] = folder if folder else default_folder
    save_config(cfg)
    acc = {"id": sec, "apple_id": aid, "folder": cfg[sec]["folder"]}
    do_login(acc, cfg)


def menu_manage(cfg):
    accs = get_accounts(cfg)
    if not accs:
        print("暂无账号。")
        return
    print("当前账号：")
    for i, a in enumerate(accs, 1):
        where = ("备份根目录" if not a["folder"] else "子文件夹 " + a["folder"])
        print("  %d. %s  ->  %s" % (i, a["apple_id"], where))
    print("  d. 删除某个账号配置（仅删配置，不删已下载照片）")
    print("  r. 返回")
    c = input("选择：").strip().lower()
    if c == "d":
        idx = input("输入要删除的号码：").strip()
        try:
            i = int(idx) - 1
            cfg.remove_section(accs[i]["id"])
            save_config(cfg)
            log("已删除账号配置：" + accs[i]["apple_id"])
        except Exception:
            print("输入无效。")


def menu_backup(cfg):
    s = cfg["settings"]
    print("当前备份根目录：" + s["backup_root"])
    new = input("输入新的备份根目录（留空不变）：").strip()
    if new:
        try:
            os.makedirs(new, exist_ok=True)
            s["backup_root"] = new
            save_config(cfg)
            log("备份根目录已改为：" + new)
        except Exception as e:
            print("创建目录失败：" + str(e))


def parse_time(s):
    try:
        if ":" not in s:
            return False, ""
        h, m = s.split(":")
        h = int(h)
        m = int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return True, "%02d:%02d" % (h, m)
    except Exception:
        pass
    return False, ""


def menu_full(cfg):
    accs = get_accounts(cfg)
    if not accs:
        print("请先添加账号。")
        return
    print("选择要全量下载的账号（输入号码，逗号分隔；直接回车=全部）：")
    for i, a in enumerate(accs, 1):
        print("  %d. %s" % (i, a["apple_id"]))
    sel = input("选择：").strip()
    if sel == "":
        chosen = accs
    else:
        idxs = [x.strip() for x in sel.split(",") if x.strip()]
        chosen = []
        for x in idxs:
            try:
                chosen.append(accs[int(x) - 1])
            except Exception:
                pass
        if not chosen:
            print("选择无效。")
            return
    ids = [a["apple_id"] for a in chosen]
    print("已选：%s" % "、".join(ids))
    when = input("何时开始？输入 N 立即开始，或输入时间如 23:30 定时开始：").strip().lower()
    if when in ("n", "now", ""):
        for acc in chosen:
            sync_one(acc, cfg, "full", interactive=True)
    else:
        ok, hhmm = parse_time(when)
        if not ok:
            print("时间格式不对，已取消。")
            return
        schedule_once(hhmm, ids)


def menu_sync_now(cfg):
    accs = get_accounts(cfg)
    if not accs:
        print("请先添加账号。")
        return
    print("立即增量同步所有账号（已下载满 %s 张即停）..." % INCREMENTAL_UNTIL)
    for acc in accs:
        sync_one(acc, cfg, "incremental", interactive=True)


def menu_schedule(cfg):
    when = input("每日自动同步的时间？（输入 HH:MM，如 21:00）：").strip()
    ok, hhmm = parse_time(when)
    if not ok:
        print("时间格式不对。")
        return
    schedule_daily(hhmm)


def main_menu():
    cfg = load_config()
    while True:
        print("\n========== iCloud 照片同步（多账号）==========")
        print("1. 登录 / 添加账号")
        print("2. 管理账号（查看 / 删除）")
        print("3. 设置备份地址")
        print("4. 全量下载（可选时间）")
        print("5. 立即同步一次（增量）")
        print("6. 设置每日自动同步（可选时间）")
        print("7. 查看 / 取消定时任务")
        print("8. 退出")
        c = input("请选择（1-8）：").strip()
        if c == "1":
            menu_login(cfg)
        elif c == "2":
            menu_manage(cfg)
        elif c == "3":
            menu_backup(cfg)
        elif c == "4":
            menu_full(cfg)
        elif c == "5":
            menu_sync_now(cfg)
        elif c == "6":
            menu_schedule(cfg)
        elif c == "7":
            show_tasks()
        elif c == "8":
            print("再见。")
            break
        else:
            print("请输入 1-8。")


def main():
    args = sys.argv[1:]
    if "--sync" in args:
        headless("incremental", None)
    elif "--full" in args:
        acc_arg = None
        if "--accounts" in args:
            j = args.index("--accounts")
            if j + 1 < len(args):
                acc_arg = args[j + 1]
        headless("full", acc_arg)
    else:
        main_menu()


if __name__ == "__main__":
    main()
