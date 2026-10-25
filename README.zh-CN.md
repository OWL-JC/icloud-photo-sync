# iCloud 照片同步（iCloud Photo Sync）

一个简单的、带菜单的工具，用第三方下载器 [`icloudpd`](https://github.com/boredazfcuk/icloud_photos_downloader) 把 iCloud 照片库同步到本地文件夹。

支持 **多个 Apple ID**（每个账号一个独立子文件夹）、**可配置的备份地址**，以及 **定时同步**（Windows 任务计划程序，或 Linux/macOS 的 `cron`）。

> 说明：交互菜单为中文；工具本身可在任何能运行 `icloudpd` 的系统上使用。
> 英文版请见 [README.md](README.md)。

## 功能

- 把 iPhone / iCloud 照片同步到本地磁盘，按 `年/月` 归档。
- 多账号，每个账号在备份根目录下拥有独立子文件夹。
- 备份根目录与 icloudpd 路径均可通过 `config.ini` 配置。
- 两种无需人工介入的后台模式（供定时任务调用）：
  - `python sync_all.py --sync` —— 增量同步（连续 50 张已下载即停）。
  - `python sync_all.py --full` —— 全量下载（可加 `--accounts a,b,c` 指定账号）。
- **不保存密码**：登录时一次性输入密码 + 双重验证码，icloudpd 会保存会话凭证，之后同步无需密码。

## 为什么不用苹果官方 iCloud for Windows 客户端？

官方客户端通过 `gsa.apple.com` 认证，在某些网络下会被精确拦截（其他苹果域名正常，唯独它 TLS 握手被重置）。icloudpd 走的是另一套网页 API 认证通道，在官方客户端不通的环境下依然可用。本工具只是对 icloudpd 的一层封装。

## 环境要求

- Python 3.7+
- 已安装 [`icloudpd`](https://github.com/boredazfcuk/icloud_photos_downloader) 并在 PATH 中可用（或在 `config.ini` 里填完整路径）。
- 已开启 iCloud 照片的 Apple ID，且登录时能完成双重验证（密码 + 手机/设备验证码）。

## 安装

1. 安装 icloudpd（参考其文档），用 `icloudpd --help` 验证。
2. 克隆本仓库并进入目录：
   ```bash
   git clone https://github.com/OWL-JC/icloud-photo-sync.git
   cd icloud-photo-sync
   ```
3. （可选）复制配置样例并按需修改：
   ```bash
   cp config.ini.example config.ini
   ```
   首次运行 `sync_all.py` 也会自动生成一份默认 `config.ini`。

## 使用

运行交互菜单：

```bash
python sync_all.py
```

```
1. 登录 / 添加账号        # 输入 Apple ID，按提示输入密码和验证码
2. 管理账号（查看 / 删除）
3. 设置备份地址          # 更改照片存放位置
4. 全量下载（可选时间）  # 立即全量下载，或定时开始
5. 立即同步一次（增量）  # 手动跑一次增量同步
6. 设置每日自动同步      # 按选定时间创建每日计划任务
7. 查看 / 取消定时任务
8. 退出
```

## 后台（定时）模式

计划任务实际执行的就是下面这些命令，无需人工输入：

```bash
python sync_all.py --sync                                # 所有账号增量同步
python sync_all.py --full                                # 所有账号全量下载
python sync_all.py --full --accounts you@example.com     # 只同步指定账号
```

## 配置说明（config.ini）

| 键 | 含义 |
|---|---|
| `backup_root` | 照片备份根目录；每个账号会在其下建独立子文件夹 |
| `cookie_dir` | icloudpd 保存登录凭证的目录，请勿手动改删 |
| `tool` | icloudpd 可执行文件路径（已在 PATH 中可直接写 `icloudpd`） |
| `domain` | iCloud 区域；**中国大陆填 `cn`**，其他地区留空 |
| `[account_x]` | 每个账号一小节：`apple_id` 与 `folder`（子文件夹名；留空 = 备份根目录） |

## 定时同步

- **Windows**：用菜单第 `6` 项创建每日任务计划程序任务（第 `4` 项可定时一次性全量下载）。
- **Linux / macOS**：用 `cron`。例如每天 09:30 增量同步：
  ```cron
  30 9 * * * /usr/bin/python3 /path/to/icloud-photo-sync/sync_all.py --sync
  ```

## 持续集成 / GitHub Actions

仓库自带两个示例工作流（`.github/workflows/`）：

- `ci.yml`：推送或 PR 时自动做语法检查与冒烟测试（多版本 Python、Ubuntu/Windows 矩阵）。
- `scheduled-sync.yml`：**自托管 runner** 上的每日同步示例，默认只手动触发；启用方法见文件内注释（GitHub 托管的 runner 无法访问你的照片和凭证，所以必须用自托管 runner）。

## 注意事项（避坑）

- **退出码不可信**：icloudpd 参数错误时也可能返回 `0`。本工具以"文件数是否真的增加"判断成败。
- **凭证会过期**（通常几周到几个月）。同步显示"无新增"时，用菜单第 `1` 项重新登录。
- **`--folder-structure` 必须是单个花括号块**：`"{:%Y/%m}"`，不能写成 `"{:%Y}/{:%m}"`。
- **"手机删除、电脑跟着删"（`--auto-delete`）尚未实现**，计划在后续版本加入。本工具永远不会删除磁盘上已有的照片。
- 登录过程可能 1–3 分钟没有任何输出，这是正常现象，不要关闭窗口。

## 许可证

[MIT](LICENSE)
