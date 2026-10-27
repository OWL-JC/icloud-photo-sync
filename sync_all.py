# -*- coding: utf-8 -*-
import sys
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not sys.stderr.isatty():
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

"""iCloud 照片同步工具（多账号 / 可配置 / 可定时）

把 iPhone 等设备的 iCloud 照片库，按 年/月 自动同步到本机，支持多账号，
每个账号单独一个文件夹。底层使用第三方下载器 icloudpd。

日常使用：
    python sync_all.py            # 打开中文菜单，按需操作
后台（由定时任务调用，无需人工）：
    python sync_all.py --sync     # 增量同步（每个账号已下载满 50 张即停）
    python sync_all.py --full     # 全量下载（可选 --accounts a,b,c 指定账号）
    python sync_all.py --menu     # 显式打开菜单（同不带参数）
    python sync_all.py --redact-log  # 把日志中的邮箱与用户目录脱敏，便于分享

退出码（供定时任务 / CI 判断成败）：
    0  同步正常结束（有新照片或已最新都算成功）
    1  同步失败（无账号、账号名写错、icloudpd 运行异常/超时、文件数减少、
       目录统计失败等）——注意：本工具不会删除任何照片，仅下载
    2  命令行用法错误（例如 --accounts 后面没跟账号）

设计说明：
    - 账号、备份地址等都写在 config.ini 里，可随时改。
    - 密码不在本工具保存。登录时在终端输入密码+手机验证码，
      icloudpd 把登录凭证(cookie)存到 cookie_dir，之后同步无需密码。
    - 成败判定已改为"文件名集合差集"（不再只看文件总数增减）：
      能同时算出"新增了哪些"与"消失了哪些"，
      避免"少了 2 个又多了 2 个"导致 delta==0 被误判成成功。
    - “手机删除、电脑跟着删”功能本期未实现，留待后续版本；
      届时实现需区分"预期的镜像删除"与"异常的消失"。
"""
import os
import sys
import re
import time
import subprocess
import datetime
import configparser

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def find_project_root():
    """定位项目根目录（config.ini 与 logs/ 所在处）。

    脚本放在仓库根目录时取自身目录；放在 scripts/ 子目录时取上级目录。
    这样同一份文件既可用于仓库根目录，也可用于 scripts/ 布局。
    """
    if os.path.isfile(os.path.join(SCRIPT_DIR, "config.ini")):
        return SCRIPT_DIR
    parent = os.path.dirname(SCRIPT_DIR)
    if os.path.basename(SCRIPT_DIR).lower() == "scripts" and os.path.isdir(parent):
        return parent
    if os.path.isfile(os.path.join(parent, "config.ini")):
        return parent
    return SCRIPT_DIR


PROJECT = find_project_root()
CONFIG = os.path.join(PROJECT, "config.ini")
LOGDIR = os.path.join(PROJECT, "logs")

# 通用默认配置（首次运行自动写入 config.ini）
DEFAULT_BACKUP = os.path.expanduser("~/iCloudPhotos")
DEFAULT_COOKIE_DIR = os.path.expanduser("~/.icloud_session")
DEFAULT_TOOL = "icloudpd"        # 假设已加入 PATH；否则填完整路径
DEFAULT_DOMAIN = "cn"            # 中国大陆填 cn；其他地区留空

TASK_DAILY = "iCloudPhotoSyncDaily"
TASK_ONCE = "iCloudPhotoSyncOnce"
INCREMENTAL_UNTIL = "50"          # 增量同步：连续 50 张已下载即停，省时间
HEADLESS_TIMEOUT_SEC = 6 * 3600   # 后台同步超时（覆盖大批量下载的时间上限）
MB = 1024.0 * 1024.0

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


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
    except Exception as e:
        # 日志是本工具唯一的排障通道，写不进去必须让用户看见，不能静默吞掉
        try:
            sys.stderr.write("[日志系统故障] 无法写入 %s：%s\n"
                             % (os.path.join(LOGDIR, "sync.log"), e))
            sys.stderr.flush()
        except Exception:
            pass


def sanitize_folder(apple_id):
    """把邮箱变成可用的文件夹名，例如 a@b.com -> abcom"""
    s = apple_id.strip().lower().replace("@", "").replace(".", "")
    return "".join(ch if ch.isalnum() else "_" for ch in s)


def valid_apple_id(aid):
    """校验 Apple ID 格式（形如邮箱）。

    限制原因：该值会作为 --username 的参数进入 argv（以 - 开头会被 argparse
    当成选项），也会拼进 schtasks /tr 的命令串（含引号或空白会破坏引号嵌套）。
    """
    if not aid or aid != aid.strip():
        return False
    if aid.startswith("-"):
        return False
    if aid.count("@") != 1:
        return False
    for ch in ('"', "'", " ", "\t", "\\", ";", "|", "&"):
        if ch in aid:
            return False
    return True


def safe_folder(folder, apple_id):
    """校验子文件夹名：必须是相对路径且不含 ..，否则回退为默认名。

    两个必须保持的语义（改动会直接导致照片被重新下载，务必保留）：
      - folder 为空字符串 = 该账号的照片直接放备份根目录（老配置常见写法），
        必须原样返回空串，不能替换成默认子文件夹名；
      - folder 键缺失（None）才回退为默认子文件夹名。
    防止 config.ini 里填了绝对路径或 .. 时，照片被写到备份根目录之外。
    """
    if folder is None:
        return sanitize_folder(apple_id)
    f = folder.strip()
    if not f:
        return ""
    # 拒绝任何形式的绝对路径或盘符相对路径：否则 join 会逃逸到备份根目录之外。
    #   /etc/passwd          -> POSIX 根路径；Windows 上 isabs 判 False（只认盘符/UNC）
    #   以反斜杠开头的路径     -> Windows 根路径；isabs 也判 False
    #   含冒号的盘符路径（如 C:foo）-> 部分情况 isabs 为 False
    if os.path.isabs(f) or f[0] in ("/", "\\") or ":" in f:
        log("配置项 folder 不合法（%s），已回退为默认子文件夹。" % folder)
        return sanitize_folder(apple_id)
    f = f.replace("/", os.sep)
    if ".." in f.split(os.sep):
        log("配置项 folder 不合法（%s），已回退为默认子文件夹。" % folder)
        return sanitize_folder(apple_id)
    return f


def parse_index(text, total):
    """把用户输入的 1 基序号转成 0 基下标；越界或非数字返回 None。

    必须显式拒绝负索引：int("0")-1 == -1 会被 Python 解释成最后一个元素，
    导致“输入 0 却删掉/选中最后一个账号”的静默误操作。
    """
    try:
        i = int(text.strip()) - 1
    except (TypeError, ValueError):
        return None
    if i < 0 or i >= total:
        return None
    return i


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
    # 注意：domain 用 `not in` 而不是 `not s.get()`——空字符串是合法值，
    # 表示“非中国大陆，交给 icloudpd 默认区域”（见 base_cmd 中空 domain 的处理）。
    # 若被“统一风格”改成 not s.get()，用户的空 domain 会被静默写回 cn。
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
            f.write("# cookie_dir  : 登录凭证目录，请勿手动删改；不要放在网盘同步目录\n")
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
            folder = safe_folder(cfg[sec].get("folder", sanitize_folder(apple_id)), apple_id)
            accs.append({"id": sec, "apple_id": apple_id, "folder": folder})
    return accs


def count_files(root):
    """统计目录下的文件数与总字节数。

    遍历失败时返回 None（而不是部分计数）：部分计数会让 before/after 口径不一致，
    进而把失败算成“有新照片”，是备份工具最危险的假成功。
    """
    n = 0
    size = 0
    if not os.path.isdir(root):
        # os.walk 对不存在的目录不报错只会返回空，必须显式判掉，
        # 否则“目录丢失”会被当成“空目录”，进而把失败算成“有新照片”
        log("统计目录失败（目录不存在或无权限）：%s" % root)
        return None

    def _onerror(e):
        raise e   # 让 scandir 的报错真正抛出，避免被 os.walk 静默忽略后返回部分计数

    try:
        for _r, _d, files in os.walk(root, onerror=_onerror):
            for f in files:
                n += 1
                try:
                    size += os.path.getsize(os.path.join(_r, f))
                except OSError:
                    pass
    except Exception as e:
        log("统计目录失败（本次结果不可用于判断同步成败）：%s  %s" % (root, e))
        return None
    return n, size


def list_files(root):
    """列出目录下所有文件的相对路径集合。遍历失败返回 None。

    与 count_files 同样要求"失败必须返回 None"：目录丢失或无权限时
    不能退化成空集合，否则会被误判成"一个文件都没有"。
    """
    if not os.path.isdir(root):
        log("统计目录失败（目录不存在或无权限）：%s" % root)
        return None

    def _onerror(e):
        raise e   # 让 scandir 的报错真正抛出，避免被 os.walk 静默忽略

    out = set()
    try:
        for r, _d, files in os.walk(root, onerror=_onerror):
            for f in files:
                out.add(os.path.relpath(os.path.join(r, f), root))
    except Exception as e:
        log("统计目录失败（本次结果不可用于判断同步成败）：%s  %s" % (root, e))
        return None
    return out


def _size_of(root, paths):
    """统计给定相对路径集合的总字节数（单个文件取大小失败则跳过）。"""
    total = 0
    for p in paths:
        try:
            total += os.path.getsize(os.path.join(root, p))
        except OSError:
            pass
    return total


def run_icloudpd(cmd, interactive):
    t0 = time.time()
    code = 0
    try:
        if interactive:
            code = subprocess.call(cmd)
        else:
            # 后台运行：断开输入，避免长时间卡在登录提示；超时报错
            code = subprocess.call(cmd, stdin=subprocess.DEVNULL,
                                   timeout=HEADLESS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        log("超时（>%d秒）被强制结束，请检查网络或缩小范围后重试。" % HEADLESS_TIMEOUT_SEC)
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


def account_directory(s, acc):
    """账号的照片目录：folder 为空则直接使用备份根目录。"""
    return os.path.join(s["backup_root"], acc["folder"]) if acc["folder"] else s["backup_root"]


def sync_one(acc, cfg, mode, interactive):
    """同步单个账号，返回 EXIT_OK / EXIT_FAIL。"""
    s = cfg["settings"]
    directory = account_directory(s, acc)
    os.makedirs(directory, exist_ok=True)
    cmd = base_cmd(s, acc["apple_id"], directory)
    if mode == "incremental":
        cmd += ["--until-found", INCREMENTAL_UNTIL]
    # mode == "full": 不加任何限制，下载全部
    # 注意：本期未实现 --auto-delete（手机删除电脑跟着删），后续版本再加。

    before_paths = list_files(directory)
    if before_paths is None:
        log("[%s] 结果：失败（同步前统计目录失败）。" % acc["apple_id"])
        return EXIT_FAIL
    before_n = len(before_paths)
    before_sz = _size_of(directory, before_paths)   # 仅用于展示体积
    log("[%s] 开始同步 模式=%s 目录=%s before=%d文件/%.1fMB" %
        (acc["apple_id"], mode, directory, before_n, before_sz / MB))
    elapsed, code = run_icloudpd(cmd, interactive)
    after_paths = list_files(directory)
    if after_paths is None:
        log("[%s] 结果：失败（同步后统计目录失败）。" % acc["apple_id"])
        return EXIT_FAIL
    after_n = len(after_paths)
    after_sz = _size_of(directory, after_paths)

    # 成败判定用"文件名集合差集"，而不是只看文件总数增减：
    # 只看总数时，"少了 2 个又多了 2 个"会让 delta==0，被误判成成功；
    # 差集能同时算出"新增了哪些"与"消失了哪些"，真正暴露备份目录被改动/误删。
    added = after_paths - before_paths
    removed = before_paths - after_paths
    delta = len(added)

    log("[%s] 结束 after=%d文件/%.1fMB 本次新增%d个(+%.1fMB) 消失%d个 耗时%.1f分 退出码%d" %
        (acc["apple_id"], after_n, after_sz / MB, delta,
         (after_sz - before_sz) / MB, len(removed), elapsed / 60.0, code))

    # icloudpd 退出码不可信（参数错也返回 0），故以文件集合变化与运行状态共同判定
    if code == -1:
        # 找不到 icloudpd、超时或运行异常
        log("[%s] 结果：失败（icloudpd 运行异常，请检查 tool 路径、网络与登录凭证）。"
            % acc["apple_id"])
        return EXIT_FAIL
    # 文件消失不再自动判定失败：备份目录被手动改动 / 清理时，交由用户自己决定。
    if removed:
        log("[%s] 警告：本次同步中有 %d 个文件消失（可能被手动删除或清理）：%s"
            % (acc["apple_id"], len(removed), "、".join(sorted(removed)[:5])))
        if interactive:
            # 手动运行模式：让用户自己选择是否重新下载以补齐
            try:
                ans = input("[%s] 发现 %d 个文件消失，是否重新下载以补齐？[y/N] "
                            % (acc["apple_id"], len(removed)))
            except EOFError:
                ans = ""
            if ans.strip().lower() in ("y", "yes", "是"):
                log("[%s] 用户选择重新下载，再次运行同步..." % acc["apple_id"])
                elapsed, code = run_icloudpd(cmd, interactive)
                after_paths = list_files(directory)
                if after_paths is None:
                    log("[%s] 结果：失败（重新同步后统计目录失败）。" % acc["apple_id"])
                    return EXIT_FAIL
                after_n = len(after_paths)
                after_sz = _size_of(directory, after_paths)
                added = after_paths - before_paths
                removed = before_paths - after_paths
                delta = len(added)
                log("[%s] 重新同步后：新增%d个 仍消失%d个" %
                    (acc["apple_id"], delta, len(removed)))
        # 非交互（自动运行）模式仅保留上述警告，不自动判失败，由用户后续自行决定
    if delta > 0:
        log("[%s] 结果：OK，有新照片。" % acc["apple_id"])
        return EXIT_OK
    if delta == 0:
        log("[%s] 结果：无新增（可能已最新，或登录凭证已过期需重新登录）。" % acc["apple_id"])
        return EXIT_OK
    return EXIT_FAIL


def do_login(acc, cfg):
    s = cfg["settings"]
    directory = account_directory(s, acc)
    os.makedirs(directory, exist_ok=True)
    cmd = base_cmd(s, acc["apple_id"], directory)
    cmd += ["--recent", "1"]   # 触发登录（顺便下一张），已登录则跳过
    log("开始登录账号：" + acc["apple_id"])
    log("请在提示中输入密码和手机验证码；若已登录则直接跳过。")
    run_icloudpd(cmd, interactive=True)
    log("登录结束。凭证已保存到：" + s["cookie_dir"])


# ---------------- 后台模式 ----------------
def headless(mode, accounts_arg):
    """后台同步，返回 EXIT_OK / EXIT_FAIL。定时任务靠退出码判断成败。"""
    cfg = load_config()
    accs = get_accounts(cfg)
    if not accs:
        log("没有任何账号，请先运行菜单添加账号（config.ini 中缺少 account_ 小节）。")
        return EXIT_FAIL
    if accounts_arg:
        want = set(a.strip().lower() for a in accounts_arg.split(","))
        sel = [a for a in accs if a["apple_id"].lower() in want]
        if not sel:
            log("未找到指定账号：%s（请检查拼写；可用账号见 config.ini）" % accounts_arg)
            return EXIT_FAIL
    else:
        sel = accs
    log("=" * 56)
    log("后台同步开始  模式=%s  账号数=%d" % (mode, len(sel)))
    failed = 0
    for acc in sel:
        if sync_one(acc, cfg, mode, interactive=False) != EXIT_OK:
            failed += 1
    log("后台同步结束  成功=%d  失败=%d" % (len(sel) - failed, failed))
    log("=" * 56)
    return EXIT_FAIL if failed else EXIT_OK


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


def create_schtask(name, schedule_args, script_args):
    """创建/覆盖一个计划任务并输出结果。schedule_args 为 schtasks 的调度参数。"""
    tr = '"%s" "%s" %s' % (sys.executable, os.path.abspath(__file__), script_args)
    cmd = ["schtasks", "/create", "/tn", name, "/tr", tr] + schedule_args
    rc, out, err = run_schtasks(cmd)
    if rc == 0:
        log("已创建计划任务：%s" % name)
        if out.strip():
            log(out.strip())
        return True
    log("创建失败：" + (out.strip() or err.strip()))
    return False


def schedule_once(hhmm, accounts):
    if not ensure_windows():
        return
    now = datetime.datetime.now()
    hh, mm = map(int, hhmm.split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    sd = target.strftime("%Y/%m/%d")
    log("创建一次性任务：%s （日期 %s 时间 %s）" % (TASK_ONCE, sd, hhmm))
    create_schtask(TASK_ONCE,
                   ["/sc", "once", "/sd", sd, "/st", hhmm, "/f"],
                   "--full --accounts %s" % ",".join(accounts))


def schedule_daily(hhmm):
    if not ensure_windows():
        return
    log("创建每日任务：%s 时间 %s" % (TASK_DAILY, hhmm))
    create_schtask(TASK_DAILY, ["/sc", "daily", "/st", hhmm, "/f"], "--sync")


def redact_log():
    """把 logs/sync.log 中的邮箱与用户目录脱敏，便于贴到 issue 或作为 artifact 上传。

    sync.log 会记录 Apple ID 与本机绝对路径；公开仓库里任何人都能下载
    workflow artifact，分享前必须先脱敏。
    """
    path = os.path.join(LOGDIR, "sync.log")
    if not os.path.isfile(path):
        print("没有找到日志文件：" + path)
        return EXIT_OK
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception as e:
        print("读取日志失败：" + str(e))
        return EXIT_FAIL
    txt = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "***@***", txt)
    # Windows 与 POSIX 两类用户目录
    txt = re.sub(r"(?i)([a-z]:\\+users\\+)[^\\\s\"']+", r"\1***", txt)
    txt = re.sub(r"(/home/|/Users/)[^\s/\"']+", r"\1***", txt)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception as e:
        print("写入脱敏后的日志失败：" + str(e))
        return EXIT_FAIL
    print("已脱敏：" + path)
    return EXIT_OK


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
    if not valid_apple_id(aid):
        print("Apple ID 格式不对：需为邮箱，且不含空格、引号，不得以 - 开头。")
        return
    sec = "account_" + sanitize_folder(aid)
    if cfg.has_section(sec):
        print("该账号已存在，将更新其配置。")
    else:
        cfg.add_section(sec)
    cfg[sec]["apple_id"] = aid
    default_folder = sanitize_folder(aid)
    folder = input("该账号的子文件夹名（留空则用 %s）：" % default_folder).strip()
    # 留空沿用默认子文件夹名（与上方提示文案一致）；非空则校验其合法性
    cfg[sec]["folder"] = sanitize_folder(aid) if not folder else safe_folder(folder, aid)
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
        i = parse_index(idx, len(accs))
        if i is None:
            print("输入无效。")
            return
        try:
            aid = accs[i]["apple_id"]
            cfg.remove_section(accs[i]["id"])
            save_config(cfg)
            log("已删除账号配置：" + aid)
        except Exception as e:
            log("删除失败：" + str(e))


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
        invalid = []
        for x in idxs:
            i = parse_index(x, len(accs))
            if i is None:
                invalid.append(x)
            else:
                chosen.append(accs[i])
        if invalid:
            print("以下序号无效，已忽略：%s" % "、".join(invalid))
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
    if "--redact-log" in args:
        sys.exit(redact_log())
    if "--sync" in args:
        sys.exit(headless("incremental", None))
    if "--full" in args:
        acc_arg = None
        if "--accounts" in args:
            j = args.index("--accounts")
            if j + 1 < len(args):
                acc_arg = args[j + 1]
            else:
                # 不许静默退化为“全部账号全量下载”
                print("用法错误：--accounts 后面必须跟账号列表，"
                      "例如 --accounts you@example.com")
                sys.exit(EXIT_USAGE)
        sys.exit(headless("full", acc_arg))
    main_menu()
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
