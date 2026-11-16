# Hosts 加速工具

> **声明**：本项目是独立开源工具，与 GitHub, Inc. 没有任何关联，也未获其授权或背书。
> "GitHub" 是 GitHub, Inc. 的注册商标，本文档中仅作描述性使用（说明工具适用于哪些域名）。
> 本工具只修改**本机**的 hosts 文件，不代理、不转发、不加密任何流量。

自动为一批 GitHub 相关域名挑选**当前最快且证书有效**的节点，写进系统 hosts，改善解析慢、图裂、拉取卡顿的问题。

## 下载

| 方式 | 适合谁 | 说明 |
|---|---|---|
| **Releases 直链** | 首选 | [下载最新版 `HostsSpeedup.exe`](https://github.com/klp2913820/hosts-speedup/releases/latest)，单文件、免安装、启动自动请求管理员权限 |
| **国内加速镜像** | 打不开 Releases 时 | 在原始链接前面加一个第三方代理前缀，见下方 |
| **源码运行** | 想自己看代码 / 自己打包 | 只依赖 Python 标准库，无第三方包 |

**镜像写法**：把 GitHub 原始链接整段接在前缀后面。

```
https://ghfast.top/https://github.com/klp2913820/hosts-speedup/releases/download/v1.1.1/HostsSpeedup.exe
```

> ⚠️ 上面是**第三方**反向代理，与本项目无关、随时可能失效，也可能记录请求内容。
> 只用来下公开的发布文件，**不要在里面输入任何账号密码**。失效了就换一个前缀，或直接走源码运行。

**源码运行**（需要 Python 3.10+，实测 3.14）：

```powershell
git clone https://github.com/klp2913820/hosts-speedup.git
cd hosts-speedup
python hosts_speedup.py
```

**校验下载的文件**（可选的保险做法）：

```powershell
Get-FileHash .\HostsSpeedup.exe -Algorithm SHA256
```

> 本项目没有代码签名证书，Windows SmartScreen 首次运行会提示"未知发布者"，需要点「更多信息 → 仍要运行」。

## 原理

```
候选 IP 收集 → 并行测速 → TLS 证书校验 → （文件类域名）真实下载测速 → 选最优 → 写 hosts → 刷新 DNS
```

1. **候选 IP**：并发查询 5 个 DNS-over-HTTPS 服务（阿里 DNS、DNSPod、腾讯云、Cloudflare、Google），
   再从内置种子 IP 池补充。种子池是 GitHub 公布网段里长期存在的节点，连不通的会被自动过滤。
2. **测速**：每个候选 IP 做 3 次 TCP 443 连接，取中位延迟。
3. **证书校验**：用该 IP 做一次 TLS 握手并校验证书 SAN 是否匹配目标域名。
   **这一步是安全底线**——只有证书对得上的 IP 才算有效，能排除污染 IP 和中间人劫持。
4. **下载测速（深度模式，默认开启）**：`raw.githubusercontent.com`、`codeload.github.com`
   这类真正下大文件的域名，取延迟最好的 4 个候选，用 `Range: bytes=0-524287` 实测 512KB 吞吐，
   按 MB/s 排序。其余域名本来都是小请求，仍按延迟排序。
5. **选优**：证书有效优先；有下载速度的按速度降序，否则按延迟升序。
6. **写 hosts**：只替换自己标记的区块，其余内容原样保留；旧版本的孤儿区块会自动清掉，
   区块外指向同一域名的记录会被注释停用（避免过期 IP 抢先命中）；首次写入自动备份；
   沿用原文件编码（UTF-8 / GBK 自动探测），不会破坏已有中文注释。
7. **刷新 DNS**：`ipconfig /flushdns`。

> 为什么区分两种指标：**延迟**决定 API 调用、页面首屏快不快；**带宽**决定拉 zip / 下大文件快不快。
> 同一批 IP 上两者的排名经常不一致（实测延迟差 1～2ms 时，下载速度能差 3 倍）。

## 文件说明

| 文件 | 作用 |
|---|---|
| `hosts_speedup.py` | 主程序（图形界面 + 命令行） |
| `启动.bat` | 双击启动图形界面，自动请求管理员权限 |
| `静默更新.bat` | 命令行静默更新，适合做计划任务 |
| `build_exe.bat` | 一键打包成单文件 `dist\HostsSpeedup.exe` |
| `LICENSE` | MIT 许可证 |
| `README.md` | 本文档 |

## 使用

### 图形界面（推荐）

双击 `启动.bat` → UAC 点"是" → 选一种操作：

| 按钮 | 作用 |
|---|---|
| **一键测速并更新** | 测速完成后立刻写入 hosts（一次跑完） |
| **仅测速** | 只看结果，不碰 hosts（不需要管理员权限） |
| **更新 hosts** | 用**当前表格里的结果**直接写入，**不会重新测速**；测速完成或选完模型后可用 |
| **恢复备份** | 用首次修改前的备份覆盖 hosts |
| **打开 hosts** | 用记事本打开 hosts 查看 |

表格列：域名、最优 IP、延迟、下载速度、证书是否有效、候选数。

- **深度测速**（默认勾选）：文件类域名实测 512KB 下载，按 MB/s 排序；取消则纯延迟模式，约 40 秒。
- **每个域名写入 2 个 IP**：同时写主备两个，主 IP 失效时系统自动尝试备用。

典型流程：点「仅测速」看结果 → 觉得合适 → 点「更新 hosts」直接写入。

### 命令行

```powershell
# 只测速，不改 hosts（不需要管理员）
python hosts_speedup.py --test

# 测速并更新 hosts（需要管理员）
python hosts_speedup.py --update

# 关闭深度测速，只按延迟排（更快、更省流量）
python hosts_speedup.py --update --no-deep

# 从备份恢复
python hosts_speedup.py --restore
```

调试用参数（把结果写到指定文件，不碰系统 hosts，也不需要管理员）：

```powershell
python hosts_speedup.py --update --hosts C:\temp\hosts-test.txt
```

计划任务里建议加 `--quiet`，不弹窗、不打印，只写日志：

```powershell
HostsSpeedup.exe --update --quiet
```

运行日志固定在 `%LOCALAPPDATA%\HostsSpeedup\last_run.log`，出问题先看这里。

### 定期自动更新

IP 会随 CDN 调度变化，建议一周跑一次。用「任务计划程序」创建任务：

- 程序：`静默更新.bat`
- 触发器：每周，或登录时
- 勾选「使用最高权限运行」

## hosts 文件长什么样

工具只在文件末尾维护一个带标记的区块，随时可以整段删掉：

```
# >>> hosts-speedup >>>
# updated: 2026-11-16 22:44:35
140.82.121.4     github.com
20.205.243.168   api.github.com
...
# <<< hosts-speedup <<<
```

备份文件（首次修改前生成，另存一份带时间戳的副本）：

```
C:\Windows\System32\drivers\etc\hosts.bak
C:\Windows\System32\drivers\etc\hosts.20261116-224435
```

### 其他工具留下的同名记录会被自动停用

hosts 里同一个域名如果有多条记录，**过期的那条可能抢先命中**，表现就是"昨天还能开，今天突然打不开"。
常见来源：别的加速工具、本工具的旧版本、手动加过的固定 IP。

写入时程序会把这些区块外的同名记录注释掉（原始内容保留，可手动恢复）：

```
# [hosts-speedup 已停用] 140.82.112.3  github.com
```

同时会清理旧版本（`# >>> github-hosts-tool >>>`）遗留的孤儿区块。
想恢复某一行，删掉行首的 `# [hosts-speedup 已停用] ` 即可。

## 支持的域名

`github.com`、`api.github.com`、`codeload.github.com`、`raw.githubusercontent.com`、
`gist.githubusercontent.com`、`objects.githubusercontent.com`、`avatars.githubusercontent.com`、
`camo.githubusercontent.com`、`user-images.githubusercontent.com`、`github.githubassets.com`、
`collector.github.com`

增删域名改 `hosts_speedup.py` 顶部的 `DOMAINS` 列表即可。

> 老域名 `github.global.ssl.fastly.net` 已移除：它的证书不包含该主机名，写进 hosts 反而触发 TLS 报错。

## 隐私与联网行为

程序运行时只会做三件事：

1. 向 5 个 DNS-over-HTTPS 服务查询上述域名的 A 记录；
2. 与 GitHub 各 CDN 节点建立 TCP/TLS 连接（测速与证书校验，含少量测试下载）；
3. 调用系统命令 `ipconfig /flushdns`。

**没有任何遥测、统计上报或账号体系**，不读取也不上传本地文件、会话内容。
可选的社区 hosts 列表默认关闭；开启时（`--remote-list URL`）才会额外请求你指定的地址。

## 已知限制

- **只处理 IPv4**：hosts 里写 IPv6 在老网络环境中容易出问题，AAAA 记录污染也更严重。
- **下载测速只覆盖两个域名**：`objects.githubusercontent.com`（release 附件）和
  `gist.githubusercontent.com` 的 URL 是签名/随机生成的，没有固定可复用的测试资源，这两个仍按延迟排序。
- **深度模式更慢更费流量**：整轮约 1.5～2 分钟，每次多下载几 MB；频繁运行可能触发 GitHub 侧限速。
- **CDN 会变**：IP 会轮换，需要隔一段时间重跑。
- **可能被安全软件拦截**：部分杀毒软件保护 hosts 文件，写入失败时程序会明确提示。
- **不能突破网络限制**：hosts 只决定"解析到哪个 IP"，解决不了线路本身被限速或被阻断；
  它不代理、不隧道、不修改任何协议。
- **不影响 Git 走代理**：如果配了 `git config --global http.proxy`，代理优先级通常高于 hosts。

## 突然打不开了怎么办

按顺序试，通常第 1、2 步就能解决：

1. **重启浏览器**（浏览器有自己的 DNS 缓存，`ipconfig /flushdns` 清不掉它），或用无痕窗口试。
2. **命令行验证到底是网络问题还是浏览器问题**：
   ```powershell
   curl.exe -I --max-time 10 https://github.com/
   ```
   返回 `HTTP/1.1 200` 说明网络没问题，问题在浏览器；超时则说明当前写的 IP 失效了。
3. **换一个 IP**：重新跑一次测速（IP 会随 CDN 调度变化）。
4. **回滚**：界面点「恢复备份」，或者手动删掉两个标记之间的内容。
5. 还是不行就说明是线路本身的问题，hosts 解决不了。

## 还原方法

1. 界面点「恢复备份」；
2. 命令行 `python hosts_speedup.py --restore`；
3. 手动编辑 `C:\Windows\System32\drivers\etc\hosts`，删掉两个标记之间的内容。

## 打包

```bat
build_exe.bat
```

安装 PyInstaller 并打包 `dist\HostsSpeedup.exe`（单文件、无控制台窗口、启动自动提权）。

> PyInstaller 采用 GPL + bootloader exception，允许分发打包后的程序；Python 本身使用 PSF 许可证。

发布时把 `dist\HostsSpeedup.exe` 作为附件传到 GitHub Releases（tag 与 `APP_VERSION` 保持一致），
并在发布说明里附上 SHA256：

```powershell
Get-FileHash dist\HostsSpeedup.exe -Algorithm SHA256
```

## 许可

[MIT](LICENSE)。程序按"现状"提供，不对修改 hosts 造成的任何后果承担责任；
建议在修改前确认你有权调整这台机器的系统配置。
