#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub Hosts 加速工具 (Windows)

原理
----
1. 通过多个 DNS-over-HTTPS 服务查询 GitHub 相关域名的候选 IP
2. 对每个候选 IP 做 TCP 443 连接测速，并做 TLS 证书校验（防止被劫持/污染）
3. 选出"证书有效 + 延迟最低"的 IP，写入系统 hosts 文件
4. 刷新 DNS 缓存

用法
----
    python github_hosts.py              # 打开图形界面（需要管理员权限，会自动提权）
    python github_hosts.py --test       # 只测速，不修改 hosts
    python github_hosts.py --update     # 静默测速并更新 hosts（适合计划任务）
    python github_hosts.py --restore    # 从备份恢复 hosts
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import ssl
import statistics
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

APP_NAME = "Hosts 加速工具"
APP_VERSION = "1.1.2"
APP_DESC = "为 GitHub 等域名的访问挑选最快节点并写入系统 hosts"
DISCLAIMER = "开源工具，与 GitHub, Inc. 无任何关联；GitHub 是其注册商标。"

HOSTS_PATH = r"C:\Windows\System32\drivers\etc\hosts"
HOSTS_BACKUP = HOSTS_PATH + ".bak"
MARK_BEGIN = "# >>> hosts-speedup >>>"
MARK_END = "# <<< hosts-speedup <<<"

# 历史版本的标记（改名前的 github-hosts-tool）。
# 写入时要把这些旧块一起清掉，否则旧块会留在 hosts 里变成"孤儿"，里面的过期 IP 会先被命中。
LEGACY_MARKS = [
    ("# >>> github-hosts-tool >>>", "# <<< github-hosts-tool <<<"),
]

# 需要加速的域名
DOMAINS = [
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "objects.githubusercontent.com",
    "avatars.githubusercontent.com",
    "camo.githubusercontent.com",
    "user-images.githubusercontent.com",
    "github.githubassets.com",
    "collector.github.com",
]

# 种子 IP：GitHub 公布网段里长期存在的节点，用来补足 DoH 只返回 1~2 个 IP 的情况。
# 连不通或证书不符的会被自动过滤，加错也不影响结果。
SEED_IPS = {
    "github.com": [
        "140.82.112.3", "140.82.113.3", "140.82.114.3",
        "140.82.116.3", "140.82.121.3", "20.205.243.166",
    ],
    "api.github.com": [
        "140.82.112.6", "140.82.113.6", "140.82.121.6", "20.205.243.168",
    ],
    "codeload.github.com": [
        "140.82.112.9", "140.82.113.9", "140.82.121.9", "20.205.243.165",
    ],
    "raw.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "gist.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "objects.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "avatars.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "camo.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "user-images.githubusercontent.com": [f"185.199.10{i}.133" for i in range(8, 12)],
    "github.githubassets.com": [f"185.199.10{i}.215" for i in range(8, 12)],
    "collector.github.com": ["140.82.112.22", "140.82.113.22", "20.205.243.167"],
}

# DNS-over-HTTPS 服务（国内可用的排前面）
DOH_PROVIDERS = [
    ("阿里 DNS", "https://dns.alidns.com/resolve?name={name}&type=A"),
    ("DNSPod", "https://doh.pub/dns-query?name={name}&type=A"),
    ("腾讯云", "https://1.12.12.12/resolve?name={name}&type=A"),
    ("Cloudflare", "https://cloudflare-dns.com/dns-query?name={name}&type=A"),
    ("Google", "https://dns.google/resolve?name={name}&type=A"),
]

# 可选的远端 IP 列表：默认关闭。
# 上游社区列表（如 GitHub520）通常没有明确的许可证，默认既不依赖也不分发其数据。
# 需要时用 --remote-list URL 显式开启（可重复指定）。
REMOTE_LISTS: list[str] = []

TCP_PORT = 443
PROBE_TIMEOUT = 1.5       # 单次连接超时（秒）
PROBE_SAMPLES = 3         # 每个 IP 采样次数
DOH_TIMEOUT = 5           # DoH 查询超时（秒）
MAX_WORKERS = 32

# 深度测速：这些域名按真实下载速度排序，其余仍按延迟排序。
# 值是用于测速的路径，必须是该域名下真实存在、且资源较大的文件。
FILE_DOWNLOAD_TESTS = {
    # 内核 MAINTAINERS 文件，约 800KB 纯文本
    "raw.githubusercontent.com": "/torvalds/linux/master/MAINTAINERS",
    # git 源码 tarball，几十 MB，够测吞吐
    "codeload.github.com": "/git/git/tar.gz/refs/tags/v2.43.0",
}

DOWNLOAD_CAP = 512 * 1024   # 每次最多下载 512KB
DOWNLOAD_BUDGET = 3.0       # 单次下载最长耗时（秒）
DOWNLOAD_TOP_N = 4          # 每个域名只对延迟最好的 N 个候选做下载测试

# 默认只写"证书有效"的 IP；设为 True 则连证书异常的也写（不建议）
ALLOW_UNVERIFIED = False

_TEXT_ENCODING = "utf-8"   # hosts 文件编码（读取时自动探测，写入时沿用）
_QUIET = False             # --quiet：不弹窗，只写日志


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Probe:
    ip: str
    latency: float          # 中位延迟 ms
    best: float             # 最低延迟 ms
    verified: bool          # TLS 证书是否匹配域名
    ok_samples: int
    speed: float | None = None   # 实测下载速度 MB/s（仅深度测速的文件类域名）
    serves: bool = False         # 该节点是否真的能返回这个域名的内容（HTTP 状态行）

    @property
    def score(self) -> float:
        return self.latency


@dataclass
class DomainResult:
    domain: str
    candidates: int = 0
    best: Probe | None = None
    backups: list[Probe] = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """以管理员身份重新启动自己，返回是否成功发起提权。"""
    try:
        import ctypes
        if getattr(sys, "frozen", False):
            exe, args = sys.executable, sys.argv[1:]
        else:
            exe = sys.executable
            args = [os.path.abspath(__file__)] + sys.argv[1:]
        params = " ".join(f'"{a}"' if " " in a else a for a in args)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
        return rc > 32
    except Exception:
        return False


LOG_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "HostsSpeedup")
LOG_FILE = os.path.join(LOG_DIR, "last_run.log")


def log_default(msg: str) -> None:
    text = str(msg)
    try:
        if sys.stdout is not None:
            print(text, flush=True)
    except Exception:
        pass
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except Exception:
        pass


def notify(text: str) -> None:
    """无控制台的 exe（双击运行 / 计划任务）里用弹窗给出结果，避免"什么都没发生"。"""
    if _QUIET or not getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, f"{APP_NAME} v{APP_VERSION}", 0x40)
    except Exception:
        pass



# --------------------------------------------------------------------------- #
# 候选 IP 收集
# --------------------------------------------------------------------------- #
def query_doh(domain: str, url_tpl: str) -> list[str]:
    req = urllib.request.Request(
        url_tpl.format(name=domain),
        headers={"accept": "application/dns-json", "user-agent": f"HostsSpeedup/{APP_VERSION}"},
    )
    with urllib.request.urlopen(req, timeout=DOH_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    ips = []
    for ans in data.get("Answer", []) or []:
        if ans.get("type") not in (1, None):   # 只要 A 记录
            continue
        val = str(ans.get("data", "")).strip()
        try:
            ip = ipaddress.IPv4Address(val)
        except ValueError:
            continue
        if ip.is_global and not ip.is_multicast:
            ips.append(str(ip))
    return ips


def query_remote_lists(log=log_default) -> dict[str, list[str]]:
    """从社区维护的 hosts 列表补充候选 IP。失败不影响主流程。"""
    found: dict[str, list[str]] = {}
    for url in REMOTE_LISTS:
        try:
            req = urllib.request.Request(url, headers={"user-agent": f"HostsSpeedup/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=DOH_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as exc:
            log(f"  · 远端列表不可用（忽略）：{url.split('/')[2]} ({exc.__class__.__name__})")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                ip = ipaddress.IPv4Address(parts[0])
            except ValueError:
                continue
            if not ip.is_global:
                continue
            for host in parts[1:]:
                host = host.lower().rstrip(".")
                if host in DOMAINS:
                    found.setdefault(host, []).append(str(ip))
    return found


def collect_candidates(log=log_default) -> dict[str, list[str]]:
    log("① 收集候选 IP（DNS-over-HTTPS）…")
    remote = query_remote_lists(log)
    result: dict[str, list[str]] = {}

    jobs = [(d, name, tpl) for d in DOMAINS for name, tpl in DOH_PROVIDERS]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(query_doh, d, tpl): (d, name) for d, name, tpl in jobs}
        for fut in futures:
            d, name = futures[fut]
            try:
                ips = fut.result()
            except Exception:
                ips = []
            if ips:
                result.setdefault(d, []).extend(ips)

    for d, ips in remote.items():
        result.setdefault(d, []).extend(ips)

    for d, ips in SEED_IPS.items():
        if d in DOMAINS:
            result.setdefault(d, []).extend(ips)

    # 去重并保持稳定顺序
    for d in list(result):
        seen, uniq = set(), []
        for ip in result[d]:
            if ip not in seen:
                seen.add(ip)
                uniq.append(ip)
        result[d] = uniq

    total = sum(len(v) for v in result.values())
    log(f"   共 {len(result)}/{len(DOMAINS)} 个域名、{total} 个候选 IP")
    return result


# --------------------------------------------------------------------------- #
# 测速与证书校验
# --------------------------------------------------------------------------- #
def tls_ok(ip: str, domain: str, timeout: float = 2.5) -> bool:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, TCP_PORT), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain):
                return True
    except (ssl.SSLError, OSError):
        return False


def serves_http(ip: str, domain: str, timeout: float = 4.0) -> bool:
    """验证这个节点真的能对应该域名返回内容：TLS 握手后发一个请求，收到 HTTP 状态行即算可用。
    只做 TLS 握手不够——有些节点能握手，但早就不能提供服务了（过期边缘、被限速、劫持残留）。"""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((ip, TCP_PORT), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as s:
                s.settimeout(timeout)
                s.sendall(
                    f"HEAD / HTTP/1.1\r\nHost: {domain}\r\n"
                    f"User-Agent: HostsSpeedup/{APP_VERSION}\r\nConnection: close\r\n\r\n".encode()
                )
                head = s.recv(64)
    except (ssl.SSLError, OSError):
        return False
    if not head.startswith(b"HTTP/"):
        return False
    status = head[9:12]
    return status not in (b"500", b"502", b"503", b"504")


def measure_download(ip: str, domain: str, path: str,
                     cap: int = DOWNLOAD_CAP, budget: float = DOWNLOAD_BUDGET) -> float | None:
    """直连指定 IP（SNI 用真实域名）下载一小段，返回 MB/s。失败返回 None。"""
    ctx = ssl.create_default_context()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {domain}\r\n"
        f"Range: bytes=0-{cap - 1}\r\n"
        f"User-Agent: HostsSpeedup/{APP_VERSION}\r\n"
        "Connection: close\r\n\r\n"
    )
    start = time.perf_counter()
    header_end = -1
    buf = b""
    body = 0
    first_byte_at = None
    try:
        with socket.create_connection((ip, TCP_PORT), timeout=3.0) as raw:
            with ctx.wrap_socket(raw, server_hostname=domain) as sock:
                sock.settimeout(2.5)
                sock.sendall(req.encode())
                while body < cap:
                    if time.perf_counter() - start > budget:
                        break
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    if header_end < 0:
                        buf += chunk
                        idx = buf.find(b"\r\n\r\n")
                        if idx >= 0:
                            head = buf[:idx].decode("latin-1", "replace")
                            if not (head.startswith("HTTP/1.1 200") or head.startswith("HTTP/1.1 206")
                                    or head.startswith("HTTP/1.0 200")):
                                return None
                            header_end = idx + 4
                            body = len(buf) - header_end
                            buf = b""
                            first_byte_at = time.perf_counter() - start
                    else:
                        body += len(chunk)
    except (OSError, ssl.SSLError):
        return None

    if body < 64 * 1024 or first_byte_at is None:
        return None
    transfer = max(time.perf_counter() - start - first_byte_at, 0.05)
    return body / transfer / 1024 / 1024


def probe(ip: str, domain: str) -> Probe | None:
    latencies = []
    for _ in range(PROBE_SAMPLES):
        start = time.perf_counter()
        try:
            with socket.create_connection((ip, TCP_PORT), timeout=PROBE_TIMEOUT):
                latencies.append((time.perf_counter() - start) * 1000.0)
        except OSError:
            continue
    if not latencies:
        return None
    verified = tls_ok(ip, domain)
    return Probe(
        ip=ip,
        latency=statistics.median(latencies),
        best=min(latencies),
        verified=verified,
        ok_samples=len(latencies),
        serves=serves_http(ip, domain) if verified else False,
    )


def rank_key(p: Probe):
    """证书有效 + 能真正返回内容优先；有下载速度的按速度降序，其余按延迟升序。"""
    has_speed = p.speed is not None
    return (not p.verified, not p.serves, 0 if has_speed else 1, -(p.speed or 0.0), p.latency)


def pick_best(domain: str, ips: list[str], deep: bool = True, log=log_default) -> DomainResult:
    res = DomainResult(domain=domain, candidates=len(ips))
    if not ips:
        res.error = "没有候选 IP"
        return res

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(ips))) as pool:
        probes = [p for p in pool.map(lambda ip: probe(ip, domain), ips) if p]

    if not probes:
        res.error = "全部连接失败"
        return res

    probes.sort(key=lambda p: (not p.verified, not p.serves, p.latency))

    # 深度测速：只对文件类域名的前 N 个候选做真实下载
    test_path = FILE_DOWNLOAD_TESTS.get(domain)
    if deep and test_path:
        targets = [p for p in probes if p.verified and p.serves][:DOWNLOAD_TOP_N]
        if targets:
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                speeds = list(pool.map(lambda p: measure_download(p.ip, domain, test_path), targets))
            for p, spd in zip(targets, speeds):
                p.speed = spd
            likely = max((s for s in speeds if s), default=None)
            log(f"   · {domain} 下载测速：{len(targets)} 个候选"
                + (f"，最快 {likely:.2f} MB/s" if likely else "，全部失败，回退按延迟排序"))

    probes.sort(key=rank_key)
    res.best = probes[0]
    res.backups = [p for p in probes[1:] if p.verified][:3]
    return res


def run_tests(log=log_default, domains=None, deep: bool = True) -> list[DomainResult]:
    domains = domains or DOMAINS
    cands = collect_candidates(log)
    log("② 多线路测速 + TLS 证书校验" + ("（文件类域名额外做真实下载测速）" if deep else "") + "…")

    results: list[DomainResult] = []
    for i, d in enumerate(domains, 1):
        r = pick_best(d, cands.get(d, []), deep=deep, log=log)
        results.append(r)
        if r.best:
            flag = "证书有效" if r.best.verified else "⚠ 证书异常"
            speed = f"{r.best.speed:6.2f} MB/s" if r.best.speed else "        —    "
            log(f"   [{i}/{len(domains)}] {d:<38} {r.best.ip:<16} "
                f"{r.best.latency:6.1f} ms  {speed}  {flag}")
        else:
            log(f"   [{i}/{len(domains)}] {d:<38} 失败：{r.error}")
    return results


# --------------------------------------------------------------------------- #
# hosts 文件读写
# --------------------------------------------------------------------------- #
def set_hosts_path(path: str) -> None:
    """切换目标 hosts 文件（测试时指向临时文件，避免动系统文件）。"""
    global HOSTS_PATH, HOSTS_BACKUP
    HOSTS_PATH = os.path.abspath(path)
    HOSTS_BACKUP = HOSTS_PATH + ".bak"


def read_text(path: str) -> str:
    global _TEXT_ENCODING
    if not os.path.exists(path):
        _TEXT_ENCODING = "utf-8"
        return ""
    with open(path, "rb") as fh:
        raw = fh.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            _TEXT_ENCODING = "utf-8" if enc == "utf-8-sig" else enc
            return text
        except UnicodeDecodeError:
            continue
    _TEXT_ENCODING = "utf-8"
    return raw.decode("utf-8", "surrogateescape")


def write_text(path: str, text: str) -> None:
    # 尽量沿用原文件编码，避免把 GBK 注释改成 UTF-8 引起乱码
    try:
        data = text.encode(_TEXT_ENCODING)
    except (UnicodeEncodeError, LookupError):
        data = text.encode("utf-8", "surrogateescape")

    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def backup_hosts(log=log_default) -> None:
    if os.path.exists(HOSTS_BACKUP):
        return
    content = read_text(HOSTS_PATH)
    if not content.strip():
        log("   · 目标 hosts 不存在或为空，跳过备份")
        return
    write_text(HOSTS_BACKUP, content)
    # 额外留一份带时间戳的，方便回溯
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        write_text(HOSTS_PATH + f".{stamp}", content)
    except OSError:
        pass
    log(f"   已备份原始 hosts → {os.path.basename(HOSTS_BACKUP)}")


def build_block(results: list[DomainResult], per_domain: int = 1) -> str:
    lines = [
        MARK_BEGIN,
        f"# updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  by {APP_NAME} v{APP_VERSION}",
        "# 删除本区块即可还原；也可用工具里的“恢复备份”功能",
    ]
    for r in results:
        if not r.best:
            continue
        lines.append(f"{r.best.ip:<16} {r.domain}")
        for extra in r.backups[: max(0, per_domain - 1)]:
            lines.append(f"{extra.ip:<16} {r.domain}")
    lines.append(MARK_END)
    return "\r\n".join(lines) + "\r\n"


def strip_block(content: str) -> str:
    """删掉本程序的区块，以及历史版本（改名前）遗留的孤儿块。"""
    for begin, end in [(MARK_BEGIN, MARK_END)] + LEGACY_MARKS:
        pattern = re.compile(
            r"\r?\n?" + re.escape(begin) + r".*?" + re.escape(end) + r"[ \t]*\r?\n?",
            re.DOTALL,
        )
        content = pattern.sub("\n", content)
    return content


CONFLICT_PREFIX = "# [hosts-speedup 已停用] "


def comment_out_conflicts(content: str) -> tuple[str, list[str]]:
    """把区块外指向本工具域名的记录注释掉（原内容保留，方便手动恢复）。
    不这么做的话，别的工具或旧版本写下的过期 IP 可能抢先命中，导致"刚还能上，现在打不开"。"""
    domains = {d.lower() for d in DOMAINS}
    out: list[str] = []
    disabled: list[str] = []
    for line in content.splitlines(True):
        body = line.split("#", 1)[0].strip()
        parts = body.split()
        if body and len(parts) >= 2 and parts[1].lower() in domains:
            eol = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "\r\n")
            out.append(CONFLICT_PREFIX + body + eol)
            disabled.append(body)
        else:
            out.append(line)
    return "".join(out), disabled


# 写入后是否用系统解析真连一遍（发现连不上就自动换备用节点）
VERIFY_AFTER_WRITE = True

_HTTP_OK = {200, 204, 206, 301, 302, 303, 307, 308, 403, 404, 429}


def probe_serves(domain: str, timeout: float = 8.0) -> tuple[bool, str]:
    """走系统解析（此时已含 hosts 记录）真实请求一次，判断能不能连上。"""
    url = f"https://{domain}/"
    req = urllib.request.Request(url, headers={"user-agent": f"HostsSpeedup/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in _HTTP_OK, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return exc.code in _HTTP_OK, f"HTTP {exc.code}"
    except Exception as exc:  # 超时 / 连接被重置 / TLS 失败
        return False, type(exc).__name__


def verify_written(results: list[DomainResult], log=log_default) -> list[str]:
    """写入后自检：这些域名现在到底能不能访问。返回失败的域名列表。"""
    flush_dns(log)
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(lambda r: probe_serves(r.domain), results))
    bad = []
    for r, (ok, detail) in zip(results, outcomes):
        if not ok:
            log(f"   ⚠ 自检失败：{r.domain:<38} {detail}")
            bad.append(r.domain)
    if not bad:
        log(f"   ✓ 自检通过：{len(results)} 个域名均可访问")
    return bad


def update_hosts(results: list[DomainResult], per_domain: int = 1, log=log_default) -> bool:
    def usable_now() -> list[DomainResult]:
        return [r for r in results if r.best and r.best.verified and r.best.serves]

    usable = usable_now()
    if not usable and ALLOW_UNVERIFIED:
        usable = [r for r in results if r.best]

    skipped = [r for r in results
               if r.best and r not in usable]
    if skipped:
        names = "、".join(r.domain for r in skipped)
        log(f"   · 跳过 {len(skipped)} 个没有可用节点的域名：{names}")
    if not usable:
        log("   ✗ 没有验证通过的节点，hosts 未修改")
        return False

    log("③ 写入 hosts…")
    backup_hosts(log)

    original = read_text(HOSTS_PATH)
    if any(begin in original for begin, _ in LEGACY_MARKS):
        log("   · 已清理旧版本遗留的标记块")
    cleaned = strip_block(original)
    cleaned, disabled = comment_out_conflicts(cleaned)
    if disabled:
        log(f"   · 已停用 {len(disabled)} 条其他程序写入的同类记录（保留为注释，可手动恢复）：")
        for line in disabled[:5]:
            log(f"      {line}")
        if len(disabled) > 5:
            log(f"      ……另有 {len(disabled) - 5} 条")

    def compose(entries: list[DomainResult]) -> str:
        block = build_block(entries, per_domain)
        if cleaned.strip():
            base = cleaned if cleaned.endswith(("\n", "\r\n")) else cleaned.rstrip("\r\n") + "\r\n"
            return base + "\r\n" + block
        return block

    try:
        write_text(HOSTS_PATH, compose(usable))
    except PermissionError:
        log("   ✗ 没有写入权限，请以管理员身份运行")
        return False
    except OSError as exc:
        log(f"   ✗ 写入失败：{exc}")
        return False

    # 回读校验
    if MARK_BEGIN not in read_text(HOSTS_PATH):
        log("   ✗ 写入后校验失败（可能被杀毒软件拦截）")
        return False

    log(f"   ✓ 已写入 {len(usable)} 个域名")

    # 写后自检 + 自动换备用
    if VERIFY_AFTER_WRITE:
        bad = verify_written(usable, log)
        if bad:
            log("   · 尝试切换到备用节点…")
            fixed = 0
            for r in usable:
                if r.domain not in bad:
                    continue
                for alt in r.backups:
                    if alt.serves or serves_http(alt.ip, r.domain):
                        alt.serves = True
                        r.best = alt
                        fixed += 1
                        log(f"     {r.domain} → {alt.ip}")
                        break
            if fixed:
                try:
                    write_text(HOSTS_PATH, compose(usable))
                    still_bad = verify_written(usable, log)
                except OSError as exc:
                    log(f"   ✗ 备用节点写入失败：{exc}")
                    still_bad = bad
                if still_bad:
                    log(f"   ⚠ {len(still_bad)} 个域名仍不可用，建议稍后重跑一次测速")
            else:
                log("   ⚠ 没有可替换的备用节点，建议稍后重跑一次测速")
    return True


def flush_dns(log=log_default) -> None:
    try:
        subprocess.run(
            ["ipconfig", "/flushdns"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log("   ✓ 已刷新 DNS 缓存")
    except Exception as exc:
        log(f"   · 刷新 DNS 缓存失败：{exc}")


def restore_hosts(log=log_default) -> bool:
    if not os.path.exists(HOSTS_BACKUP):
        log("   · 没有找到备份文件，无法恢复")
        return False
    try:
        write_text(HOSTS_PATH, read_text(HOSTS_BACKUP))
    except OSError as exc:
        log(f"   ✗ 恢复失败：{exc}")
        return False
    log("   ✓ 已从备份恢复 hosts")
    flush_dns(log)
    return True


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
def run_gui(selftest: bool = False) -> int:
    import queue
    import tkinter as tk
    from tkinter import messagebox, ttk

    if not selftest and not os.environ.get("GH_NO_ELEVATE") and not is_admin():
        if relaunch_as_admin():
            return 0
        # 提权失败也允许查看测速结果（只是不能写 hosts）
    ui_q: queue.Queue = queue.Queue()
    admin = is_admin()

    root = tk.Tk()
    root.title(f"{APP_NAME} v{APP_VERSION}")
    root.geometry("920x620")
    root.minsize(820, 540)
    try:
        from tkinter import font as tkfont
        for name in ("TkDefaultFont", "TkTextFont", "TkFixedFont"):
            tkfont.nametofont(name).configure(family="Microsoft YaHei UI", size=10)
    except Exception:
        pass

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    header = ttk.Frame(root, padding=(14, 12, 14, 6))
    header.pack(fill="x")
    ttk.Label(header, text=APP_NAME, font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
    ttk.Label(
        header,
        text=APP_DESC + "（自动备份，可一键恢复）",
        foreground="#666",
    ).pack(anchor="w", pady=(2, 0))
    ttk.Label(header, text=DISCLAIMER, foreground="#999").pack(anchor="w")

    bar = ttk.Frame(root, padding=(14, 4, 14, 8))
    bar.pack(fill="x")

    cols = ("domain", "ip", "latency", "speed", "tls", "cand")
    table = ttk.Treeview(root, columns=cols, show="headings", height=12)
    for cid, title, width, anchor in (
        ("domain", "域名", 230, "w"),
        ("ip", "最优 IP", 130, "center"),
        ("latency", "延迟", 85, "center"),
        ("speed", "下载速度", 100, "center"),
        ("tls", "证书", 70, "center"),
        ("cand", "候选数", 60, "center"),
    ):
        table.heading(cid, text=title)
        table.column(cid, width=width, anchor=anchor, stretch=(cid == "domain"))
    table.tag_configure("bad", foreground="#c0392b")
    table.tag_configure("ok", foreground="#1e7e34")
    table.pack(fill="both", expand=True, padx=14)

    bottom = ttk.Frame(root, padding=(14, 8, 14, 4))
    bottom.pack(fill="both", expand=False)
    status = ttk.Label(bottom, text="就绪", foreground="#444")
    status.pack(anchor="w")

    logbox = tk.Text(root, height=9, wrap="none", bg="#1e1e1e", fg="#d4d4d4",
                     insertbackground="#d4d4d4", relief="flat", font=("Consolas", 9))
    logbox.pack(fill="both", expand=False, padx=14, pady=(6, 10))

    state = {"running": False, "results": []}
    per_domain = tk.BooleanVar(value=False)
    deep = tk.BooleanVar(value=True)

    def push_log(msg: str) -> None:
        ui_q.put(("log", msg))

    def push_row(res: DomainResult) -> None:
        ui_q.put(("row", res))

    def set_status(text: str) -> None:
        ui_q.put(("status", text))

    def worker(action: str) -> None:
        try:
            if action == "apply":
                set_status("正在把当前测速结果写入 hosts…")
                ok = update_hosts(state["results"], 2 if per_domain.get() else 1, push_log)
                set_status("已写入 hosts，DNS 缓存已刷新" if ok else "写入 hosts 失败，详见日志")
            elif action in ("test", "update"):
                set_status("正在测速，请稍候…")
                results = run_tests(push_log, deep=deep.get())
                state["results"] = results
                for r in results:
                    push_row(r)
                good = [r for r in results if r.best]
                if action == "update":
                    if not admin:
                        push_log("   ✗ 当前不是管理员，无法写入 hosts")
                        set_status("写入失败：需要管理员权限")
                    else:
                        ok = update_hosts(results, 2 if per_domain.get() else 1, push_log)
                        set_status(f"完成：{len(good)} 个域名可用" if ok else "写入 hosts 失败")
                else:
                    set_status(f"测速完成：{len(good)}/{len(results)} 个域名可用，可点「更新 hosts」直接写入")
            elif action == "restore":
                set_status("正在恢复…")
                ok = restore_hosts(push_log) if admin else False
                if not admin:
                    push_log("   ✗ 当前不是管理员，无法恢复 hosts")
                set_status("已恢复" if ok else "恢复失败")
        except Exception as exc:  # 兜底，避免线程静默死亡
            push_log(f"   ✗ 出错：{exc.__class__.__name__}: {exc}")
            set_status("出错，详见日志")
        finally:
            ui_q.put(("done", action))

    def start(action: str) -> None:
        if state["running"]:
            return
        state["running"] = True
        for b in buttons:
            b.state(["disabled"])
        if action != "apply":          # 复用测速结果时不要清空表格和日志
            for item in table.get_children():
                table.delete(item)
            logbox.delete("1.0", "end")
        threading.Thread(target=worker, args=(action,), daemon=True).start()

    def on_test() -> None:
        start("test")

    def on_update() -> None:
        start("update")

    def on_apply() -> None:
        if not state["results"]:
            messagebox.showinfo("提示", "请先点「仅测速」得到结果，再更新 hosts")
            return
        if not admin:
            messagebox.showwarning("权限不足", "需要管理员权限才能修改 hosts，请重新以管理员身份运行")
            return
        if not messagebox.askyesno("确认", "把当前表格里的结果写入 hosts 吗？（不会重新测速）"):
            return
        start("apply")

    def on_restore() -> None:
        if not messagebox.askyesno("确认", "要用备份文件覆盖当前的 hosts 吗？"):
            return
        start("restore")

    def on_open_hosts() -> None:
        try:
            os.startfile(HOSTS_PATH)  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("打不开", str(exc))

    btn_apply = ttk.Button(bar, text="更新 hosts", command=on_apply, state="disabled")
    buttons = [
        ttk.Button(bar, text="一键测速并更新", command=on_update),
        ttk.Button(bar, text="仅测速", command=on_test),
        btn_apply,
        ttk.Button(bar, text="恢复备份", command=on_restore),
        ttk.Button(bar, text="打开 hosts", command=on_open_hosts),
    ]
    for b in buttons:
        b.pack(side="left", padx=(0, 8))
    ttk.Checkbutton(bar, text="每个域名写入 2 个 IP（主+备）", variable=per_domain).pack(side="left", padx=12)
    ttk.Checkbutton(bar, text="深度测速（文件类域名实测下载）", variable=deep).pack(side="left", padx=4)
    if not admin:
        ttk.Label(bar, text="⚠ 非管理员：只能测速", foreground="#c0392b").pack(side="right")

    def pump() -> None:
        try:
            while True:
                kind, payload = ui_q.get_nowait()
                if kind == "log":
                    logbox.insert("end", payload + "\n")
                    logbox.see("end")
                elif kind == "row":
                    r: DomainResult = payload
                    if r.best:
                        table.insert("", "end", values=(
                            r.domain, r.best.ip, f"{r.best.latency:.1f} ms",
                            f"{r.best.speed:.2f} MB/s" if r.best.speed else "—",
                            "有效" if r.best.verified else "异常", r.candidates,
                        ), tags=("ok" if r.best.verified else "bad",))
                    else:
                        table.insert("", "end", values=(r.domain, "—", r.error, "—", "—", r.candidates),
                                     tags=("bad",))
                elif kind == "status":
                    status.config(text=payload)
                elif kind == "done":
                    state["running"] = False
                    for b in buttons:
                        b.state(["!disabled"])
                    # 没有可用结果 / 非管理员时，"更新 hosts" 保持禁用
                    ready = admin and any(r.best and r.best.verified for r in state["results"])
                    if not ready:
                        btn_apply.state(["disabled"])
        except queue.Empty:
            pass
        root.after(120, pump)

    root.after(120, pump)
    if selftest:
        root.after(900, root.destroy)
    root.mainloop()
    return 0


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} v{APP_VERSION}")
    g = parser.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true", help="只测速，不修改 hosts")
    g.add_argument("--update", action="store_true", help="测速并更新 hosts（静默）")
    g.add_argument("--restore", action="store_true", help="从备份恢复 hosts")
    g.add_argument("--no-gui", action="store_true", help="不启动图形界面")
    parser.add_argument("--hosts", metavar="PATH", help="指定 hosts 文件路径（调试用，不动系统文件）")
    parser.add_argument("--selftest", action="store_true", help="界面自检后自动退出")
    parser.add_argument("--quiet", action="store_true", help="不弹窗、不打印，只写日志（适合计划任务）")
    parser.add_argument("--no-deep", action="store_true", help="关闭深度测速（只测延迟，约 40 秒跑完）")
    parser.add_argument("--remote-list", action="append", metavar="URL",
                        help="额外从社区维护的 hosts 列表补充候选 IP（可重复指定，默认不用）")
    args = parser.parse_args(argv)

    global _QUIET
    _QUIET = bool(args.quiet)

    try:
        return _dispatch(args)
    except KeyboardInterrupt:
        return 130
    except Exception:
        detail = traceback.format_exc()
        log_default("未处理的异常：\n" + detail)
        last = detail.strip().splitlines()[-1]
        notify(f"运行出错：{last}\n\n完整日志：{LOG_FILE}")
        return 1


def _dispatch(args) -> int:
    if args.hosts:
        set_hosts_path(args.hosts)
    if getattr(args, "remote_list", None):
        REMOTE_LISTS.extend(args.remote_list)

    if args.selftest:
        return run_gui(selftest=True)

    if args.restore:
        if args.hosts is None and not is_admin() and not relaunch_as_admin():
            log_default("需要管理员权限才能恢复 hosts")
            return 1
        ok = restore_hosts()
        notify("已从备份恢复 hosts" if ok else "恢复失败，详情见日志")
        return 0 if ok else 1

    if args.test or args.update:
        results = run_tests(deep=not args.no_deep)
        verified = [r for r in results if r.best and r.best.verified]
        usable = verified or [r for r in results if r.best]
        log_default(f"\n证书有效：{len(verified)}/{len(results)}，可用：{len(usable)}/{len(results)}")
        if args.update:
            if args.hosts is None and not is_admin():
                log_default("需要管理员权限才能写 hosts（请用管理员运行）")
                notify("需要管理员权限才能修改 hosts。\n请右键以管理员身份运行。")
                return 1
            ok = update_hosts(results)
            if ok:
                notify(f"已更新 hosts：{len(verified)} 个域名")
            else:
                notify(f"写入 hosts 失败，详情见日志：\n{LOG_FILE}")
            return 0 if ok else 1
        return 0 if verified else 1

    if args.no_gui:
        parser_help = f"{APP_NAME} v{APP_VERSION}\n用法：--test | --update | --restore | --selftest"
        log_default(parser_help)
        return 0

    try:
        return run_gui()
    except Exception as exc:
        log_default(f"图形界面启动失败：{exc}")
        log_default("可改用命令行：python github_hosts.py --update")
        return 1



if __name__ == "__main__":
    sys.exit(main())
