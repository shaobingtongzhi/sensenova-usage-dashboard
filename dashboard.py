#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SenseNova 用量查询 - 桌面应用
双击运行即弹出原生窗口，无需浏览器

用法:
  python dashboard.py       # 有控制台（调试用）
  pythonw dashboard.py      # 无控制台（日常使用）

或直接双击 启动.bat
"""

import base64
import json
import os
import sys
import time
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import requests
import webview
import auth_login


def _jwt_exp(token: str):
    """从 JWT payload 解码 exp 声明（Unix 时间戳），失败返回 None。"""
    if not token or token.count(".") < 2:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None

# ============================================================
# 常量
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _is_writable(d):
    """测试目录是否可写（用于决定配置文件落盘位置）。"""
    try:
        os.makedirs(d, exist_ok=True)
        tmp = os.path.join(d, ".write_test_" + str(os.getpid()))
        with open(tmp, "w") as f:
            f.write("ok")
        os.remove(tmp)
        return True
    except Exception:
        return False


def _resolve_base_dir():
    """
    配置基地址：
    - 源码模式：脚本所在目录
    - 打包模式（frozen）：优先可执行文件同级目录
      （Windows exe / Mac 从 dist 文件夹运行时该目录可写）
    - macOS 若将 .app 移到 /Applications，Contents/MacOS 不可写，
      则回退到用户级 ~/Library/Application Support/<应用名> 目录
    """
    if not getattr(sys, "frozen", False):
        return SCRIPT_DIR
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if _is_writable(exe_dir):
        return exe_dir
    if sys.platform == "darwin":
        app_support = os.path.expanduser("~/Library/Application Support/SenseNova用量查询")
        if _is_writable(app_support):
            return app_support
    return exe_dir


BASE_DIR = _resolve_base_dir()
ACCOUNTS_FILE = "accounts.json"            # 账号数据文件名（与 exe 同目录）
REQUEST_TIMEOUT = 30                        # API 请求超时（秒）
API_BASE = "https://platform.sensenova.cn"
POOL_USAGE_ENDPOINT = "/lite/console/v1/tokenplan/pool-usage"
SERVER_PORT = 5199
AUTO_REFRESH_SECONDS = 300

# 账号默认模板（编译进 exe，首次运行若旁边没有 accounts.json 则自动落盘）
# 注意：仅含空白壳，真实 JWT 由用户在窗口内填写，不内置进 exe
DEFAULT_ACCOUNTS = {
    "accounts": [
        {"username": "", "password": "", "jwt_token": ""}
    ]
}

# 运行时变量
_server_port = None
_server = None


def ensure_config_files():
    """首次运行（exe 旁边无 accounts.json 时）写入默认模板，实现真正开箱即用。"""
    af = os.path.join(BASE_DIR, ACCOUNTS_FILE)
    if not os.path.exists(af):
        try:
            with open(af, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_ACCOUNTS, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"写入默认 accounts.json 失败: {e}")


# ============================================================
# 账号文件定位（硬编码，不再依赖外部 config 文件）
# ============================================================
def get_accounts_file():
    return os.path.join(BASE_DIR, ACCOUNTS_FILE)


def load_accounts():
    path = get_accounts_file()
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("accounts", [])


def save_accounts(accounts):
    path = get_accounts_file()
    existing = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f2:
            try:
                existing = json.load(f2)
            except:
                pass
    existing["accounts"] = accounts
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)


def update_account(username, **fields):
    """按用户名更新字段（如登录后回写 jwt_token / password 等）。"""
    path = get_accounts_file()
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    accounts = data.get("accounts", [])
    for a in accounts:
        if a.get("username") == username:
            a.update(fields)
            break
    else:
        new = {"username": username}
        new.update(fields)
        accounts.append(new)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# API 查询
# ============================================================
def query_pool_usage(token, timeout=30):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "Referer": f"{API_BASE}/console",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    url = API_BASE + POOL_USAGE_ENDPOINT
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 数据解析
# ============================================================
def fmt_num(n):
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.1f}"


def ts_to_str(ts_str):
    if not ts_str or ts_str == "0":
        return ""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts_str)


def ts_to_date(ts_str):
    if not ts_str or ts_str == "0":
        return ""
    try:
        ts = int(ts_str)
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return ""


def _to_ts(ts_str):
    """将时间戳字符串转为 int，无效返回 0。"""
    if not ts_str or ts_str == "0":
        return 0
    try:
        return int(ts_str)
    except (ValueError, TypeError):
        return 0


def parse_pool_data(raw):
    results = []
    pools = raw.get("pools") or []
    if isinstance(pools, dict):
        pools = list(pools.values())

    for pool in pools:
        if not isinstance(pool, dict):
            continue

        pool_name = pool.get("name", "未知积分池")
        model_ids = pool.get("model_ids", [])
        model_display = ", ".join(model_ids[:3])
        if len(model_ids) > 3:
            model_display += f" 等{len(model_ids)}个"

        w5h = pool.get("window_5h", {})
        w7d = pool.get("window_7d", {})

        try:
            limit_5h = float(w5h.get("limit", "0"))
            used_5h = float(w5h.get("used", "0"))
            remain_5h = float(w5h.get("remaining", "0"))
        except (ValueError, TypeError):
            limit_5h = used_5h = remain_5h = 0.0

        pct_5h = round(used_5h / limit_5h * 100, 1) if limit_5h > 0 else 0.0

        try:
            limit_7d = float(w7d.get("limit", "0"))
            used_7d = float(w7d.get("used", "0"))
            remain_7d = float(w7d.get("remaining", "0"))
        except (ValueError, TypeError):
            limit_7d = used_7d = remain_7d = 0.0

        pct_7d = round(used_7d / limit_7d * 100, 1) if limit_7d > 0 else 0.0

        try:
            grant_f = float(pool.get("grant_balance", "0"))
        except (ValueError, TypeError):
            grant_f = 0.0

        pool_type = pool.get("pool_type", "")

        results.append({
            "pool_name": pool_name,
            "pool_type": pool_type,
            "model_display": model_display,
            "limit_5h": fmt_num(limit_5h),
            "used_5h": fmt_num(used_5h),
            "remain_5h": fmt_num(remain_5h),
            "usage_pct_5h": pct_5h,
            "reset_at": ts_to_str(w5h.get("reset_at", "")),
            "reset_at_ts": _to_ts(w5h.get("reset_at", "")),
            "reset_at_7d": ts_to_str(w7d.get("reset_at", "")),
            "reset_at_7d_ts": _to_ts(w7d.get("reset_at", "")),
            "limit_7d": fmt_num(limit_7d),
            "used_7d": fmt_num(used_7d),
            "remain_7d": fmt_num(remain_7d),
            "usage_pct_7d": pct_7d,
            "grant_balance": fmt_num(grant_f),
            "nearest_expiry": ts_to_date(pool.get("nearest_grant_expiry", "0")),
        })

    return results


def ensure_token(account, timeout):
    """确保 account 持有可用 JWT：无 token 或即将过期时，用账号密码自动登录。
    返回 (token, updated, error)，updated=True 表示 token 已刷新需写回配置，
    error 非 None 时表示登录失败原因（透传到界面）。"""
    token = (account.get("jwt_token") or "").strip()

    # ---- 1) 直接解码 JWT exp 判断是否过期/即将过期 ----
    need_refresh = not token
    jwt_exp = _jwt_exp(token)
    if jwt_exp:
        # 提前 5 分钟刷新，避免边界 race
        if jwt_exp - time.time() < 300:
            need_refresh = True
    else:
        # 无法解码 exp（格式异常），保守地尝试刷新
        if token:
            need_refresh = True

    if not need_refresh:
        return token, False, None

    # ---- 2) 用账号密码自动登录 ----
    uname = (account.get("username") or "").strip()
    pwd = (account.get("password") or "").strip()
    if not uname or not pwd:
        return token, False, "JWT 已过期且无账号密码，无法自动登录"

    try:
        res = auth_login.login(uname, pwd, timeout)
    except Exception as e:
        return token, False, f"自动登录失败: {str(e)[:150]}"

    new_token = res.get("access_token", "")
    if not new_token:
        return token, False, "自动登录未返回 token"
    account["jwt_token"] = new_token
    account["token_acquired_at"] = time.time()
    account["token_expires_in"] = res.get("expires_in") or 10800
    return new_token, True, None


def query_all_accounts():
    timeout = REQUEST_TIMEOUT
    accounts = load_accounts()

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "accounts": []
    }
    updated = []

    for account in accounts:
        name = (account.get("username") or "未知").strip()
        token, was_updated, login_err = ensure_token(account, timeout)
        if was_updated:
            updated.append(account)

        if login_err:
            result["accounts"].append({
                "username": name, "error": login_err, "items": []
            })
            continue

        if not token:
            result["accounts"].append({
                "username": name, "error": "未配置 JWT 且无账号密码，无法自动登录", "items": []
            })
            continue

        try:
            raw = query_pool_usage(token, timeout)
            items = parse_pool_data(raw)
            result["accounts"].append({
                "username": name, "error": None, "items": items
            })
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            # 401 多半是 token 失效，尝试用账号密码重新登录一次
            if status == 401 and account.get("username") and account.get("password"):
                try:
                    res = auth_login.login(account["username"], account["password"], timeout)
                    token = res.get("access_token", "")
                    account["jwt_token"] = token
                    account["token_acquired_at"] = time.time()
                    account["token_expires_in"] = res.get("expires_in") or 10800
                    updated.append(account)
                    raw = query_pool_usage(token, timeout)
                    items = parse_pool_data(raw)
                    result["accounts"].append({"username": name, "error": None, "items": items})
                    continue
                except Exception as e2:
                    result["accounts"].append({
                        "username": name,
                        "error": f"JWT 过期后自动重登失败: {str(e2)[:120]}",
                        "items": []
                    })
                    continue
            if status == 401:
                err = "JWT 已过期，请用账号密码重新登录"
            elif status == 403:
                err = "权限不足"
            else:
                err = f"HTTP {status}"
            result["accounts"].append({"username": name, "error": err, "items": []})
        except Exception as e:
            result["accounts"].append({"username": name, "error": str(e)[:100], "items": []})

    # 自动登录刷新出的 token 写回配置文件，下次启动仍有效
    if updated:
        save_accounts(accounts)

    return result


# ============================================================
# HTML 页面
# ============================================================
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SenseNova 用量查询</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, "Microsoft YaHei", "Segoe UI", sans-serif; background: #f5f5f5; color: #333; overflow-x: hidden; }
.header { background: #fff; padding: 8px 12px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); position: sticky; top: 0; z-index: 100; display: flex; justify-content: space-between; align-items: center; }
.header h1 { font-size: 15px; }
.header-right { display: flex; align-items: center; gap: 8px; }
.last-update { font-size: 10px; color: #aaa; }
.refresh-info { font-size: 10px; color: #888; min-width: 0; text-align: right; }
.btn { padding: 5px 14px; border: 1px solid #d9d9d9; border-radius: 6px; background: #fff; cursor: pointer; font-size: 12px; transition: all 0.2s; }
.btn:hover:not(:disabled) { border-color: #0958d9; color: #0958d9; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary { background: #0958d9; border-color: #0958d9; color: #fff; }
.btn-primary:hover:not(:disabled) { background: #0040b0; }
.btn-sm { padding: 3px 10px; font-size: 12px; }
.container { max-width: 100%; margin: 0; padding: 10px; }
.summary { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.summary-card { flex: 1; min-width: 110px; background: #fff; border-radius: 8px; padding: 14px 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }
.summary-card .label { font-size: 11px; color: #888; }
.summary-card .value { font-size: 22px; font-weight: 700; margin-top: 2px; }
.account-card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 14px; overflow: hidden; }
.account-header { padding: 12px 18px; border-bottom: 1px solid #f0f0f0; display: flex; justify-content: space-between; align-items: center; }
.account-header .name { font-size: 15px; font-weight: 600; }
.badge { font-size: 11px; padding: 2px 8px; border-radius: 10px; }
.badge-ok { background: #e6f7e6; color: #52c41a; }
.badge-err { background: #fff1f0; color: #f5222d; }
.badge-warn { background: #fffbe6; color: #ad6800; }
table { width: 100%; border-collapse: collapse; }
th { text-align: left; padding: 6px 10px; background: #f9f9f9; font-size: 11px; color: #666; border-bottom: 1px solid #eee; white-space: nowrap; }
td { padding: 6px 10px; font-size: 12px; border-bottom: 1px solid #f0f0f0; }
tr:hover td { background: #fafafa; }
.bar { display: inline-block; width: 50px; height: 6px; background: #eee; border-radius: 3px; overflow: hidden; vertical-align: middle; margin-right: 4px; }
.bar-fill { height: 100%; border-radius: 3px; }
.green { background: #52c41a; }
.yellow { background: #faad14; }
.red { background: #f5222d; }
.tag { font-size: 10px; padding: 1px 5px; border-radius: 3px; margin-right: 4px; }
.tag-default { background: #e6f4ff; color: #0958d9; }
.tag-dedicated { background: #f6ffed; color: #389e0d; }
.loading { text-align: center; padding: 60px; color: #999; }
.spinner { display: inline-block; width: 24px; height: 24px; border: 3px solid #eee; border-top-color: #0958d9; border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.models { font-size: 10px; color: #888; max-width: 160px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tip { background: #fffbe6; border: 1px solid #ffe58f; border-radius: 6px; padding: 8px 14px; font-size: 11px; color: #ad6800; margin-bottom: 14px; }
.config-panel { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 16px; overflow: hidden; }
.config-header { padding: 12px 18px; background: #fafafa; border-bottom: 1px solid #eee; display: flex; justify-content: space-between; align-items: center; cursor: pointer; user-select: none; }
.config-header .title { font-size: 15px; font-weight: 600; }
.config-header .hint { font-size: 11px; color: #aaa; }
.config-body { padding: 16px; }
.config-row { display: flex; gap: 6px; align-items: center; margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid #f0f0f0; }
.config-row:last-child { border-bottom: none; margin-bottom: 0; padding-bottom: 0; }
.config-row .col-name { display: none; }
.config-row .col-token { display: none; }
.config-row .col-actions { display: flex; gap: 4px; flex-shrink: 0; }
.config-row input { width: 100%; padding: 5px 8px; border: 1px solid #d9d9d9; border-radius: 4px; font-size: 12px; }
.config-row input:focus { outline: none; border-color: #0958d9; box-shadow: 0 0 0 2px rgba(9,88,217,0.1); }
.config-row .col-user { flex: 1.3; min-width: 100px; }
.config-row .col-pass { flex: 1; min-width: 100px; }
.config-actions { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
.save-status { font-size: 11px; color: #52c41a; margin-left: 8px; opacity: 0; transition: opacity 0.3s; }
.save-status.show { opacity: 1; }
.collapse-icon { font-size: 13px; color: #999; transition: transform 0.2s; }
.collapse-icon.collapsed { transform: rotate(-90deg); }
.howto { font-size: 11px; color: #888; line-height: 1.7; margin-bottom: 10px; background: #f6f8fa; padding: 10px 14px; border-radius: 6px; }
.howto b { color: #333; }

/* 积分池卡片（账号卡内纵向堆叠，四周留内边距） */
.pool-card { background: #fff; border: 1px solid #eee; border-radius: 6px; padding: 10px 12px; margin: 10px 12px; }
.pool-head { margin-bottom: 8px; }
.pool-name { font-size: 13px; font-weight: 600; }
.pool-block { padding: 5px 0; border-top: 1px dashed #f0f0f0; }
.pool-block:first-of-type { border-top: none; }
.pool-stat { display: flex; justify-content: space-between; align-items: baseline; font-size: 11px; padding: 2px 0; }
.pool-stat .k { color: #888; }
.pool-stat .v { font-weight: 600; font-size: 12px; }
.pool-bar { height: 6px; background: #eee; border-radius: 3px; overflow: hidden; margin: 3px 0 2px; }
.pool-bar-fill { display: block; height: 100%; border-radius: 3px; }

/* 进度球 */
.gauge-section { display: flex; gap: 8px; justify-content: center; margin-bottom: 16px; }
.gauge-wrap { text-align: center; flex: 1; }
.gauge { position: relative; width: 130px; height: 130px; margin: 0 auto; }
.gauge svg { transform: rotate(-90deg); }
.gauge .bg-ring { fill: none; stroke: #eee; stroke-width: 10; }
.gauge .fg-ring { fill: none; stroke-width: 10; stroke-linecap: round; transition: stroke-dashoffset 0.8s ease, stroke 0.3s; }
.gauge .center { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; }
.gauge .pct { font-size: 22px; font-weight: 700; line-height: 1.1; }
.gauge .pct .unit { font-size: 12px; font-weight: 400; }
.gauge .label { font-size: 10px; color: #888; margin-top: 1px; }
.gauge-title { font-size: 12px; font-weight: 600; margin-bottom: 4px; }
.gauge-detail { font-size: 10px; color: #888; margin-top: 4px; line-height: 1.5; }
.gauge-next-refresh { font-size: 10px; color: #0958d9; margin-top: 6px; line-height: 1.6; }
.gauge-next-refresh .nr-time { font-weight: 600; }
.gauge-next-refresh .nr-countdown { display: block; font-size: 10px; color: #888; }

/* 折叠账号明细 */
.account-collapse { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); margin-bottom: 14px; overflow: hidden; }
.account-collapse-header { padding: 10px 16px; background: #fafafa; border: 1px solid #eee; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; cursor: pointer; user-select: none; }
.account-collapse-header .title { font-size: 14px; font-weight: 600; }
.account-collapse-header .hint { font-size: 11px; color: #aaa; }
.account-collapse-body { display: none; padding-top: 8px; }
.account-collapse-body.expanded { display: block; }

/* ===== 布局：竖屏单列（概览 → 明细 → 配置置底） / 横屏双栏 ===== */
.layout { display: flex; flex-direction: column; gap: 14px; }
.layout-left, .layout-right { display: contents; }
.layout .config-panel, .layout .account-collapse { margin-bottom: 0; }
.gauge-card { background: #fff; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); padding: 14px 16px 16px; order: 1; }
.ov-header { font-size: 15px; font-weight: 600; margin-bottom: 10px; }
.gauge-section { margin-bottom: 0; }
.account-collapse { order: 2; }
.config-panel { order: 3; }

/* 积分池显隐开关（眼睛图标） */
.pool-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.pool-eye { background: none; border: none; cursor: pointer; color: #888; padding: 2px 4px; display: flex; align-items: center; border-radius: 4px; flex-shrink: 0; }
.pool-eye:hover { color: #0958d9; background: #f0f5ff; }

/* 横屏（≥640px）：左窄栏（双环纵排 + 账号配置） + 右栏账号明细（账号并排成列） */
@media (min-width: 640px) {
  .layout { flex-direction: row; align-items: flex-start; gap: 16px; }
  .layout-left { display: flex; flex-direction: column; gap: 16px; width: 232px; flex-shrink: 0; }
  .layout-right { display: block; flex: 1; min-width: 0; }
  .gauge-section { flex-direction: column; align-items: center; gap: 18px; }
  .gauge-wrap { flex: 0 0 auto; width: 100%; }
  /* 账号卡片并排成列（宽度不够时自动折行），账号内积分池保持纵向堆叠 */
  #accounts { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 14px; align-items: start; }
  .account-card { margin-bottom: 0; }
  .config-row { flex-wrap: wrap; row-gap: 6px; }
  .config-row .col-user, .config-row .col-pass { flex: 1 1 calc(50% - 3px); min-width: 0; }
  .config-row .col-actions { margin-left: auto; }
  .config-actions { flex-wrap: wrap; }
  .config-actions .btn { flex: 1 1 0; }
  .config-actions .save-status { flex-basis: 100%; margin-left: 0; text-align: center; }
}
</style>
</head>
<body>
<div class="header">
  <h1>SenseNova 用量</h1>
  <div class="header-right">
    <span class="refresh-info" id="refreshInfo">加载中...</span>
    <button class="btn" id="refreshBtn" onclick="fetchUsage()">刷新</button>
  </div>
</div>
<div class="container">
  <div id="loading" class="loading"><div class="spinner"></div><p style="margin-top:10px">正在查询用量...</p></div>
  <div id="content" style="display:none">
    <div class="layout">
      <div class="layout-left">
        <div class="gauge-card">
          <div class="ov-header">用量概览</div>
          <div class="gauge-section" id="gauges"></div>
        </div>
        <div class="config-panel">
          <div class="config-header" onclick="toggleConfig()">
            <span class="title">账号配置</span>
            <span class="hint">展开/收起</span>
            <span class="collapse-icon" id="configIcon">▼</span>
          </div>
          <div class="config-body" id="configBody">
            <div class="howto">
              填写「用户名」「密码」后点 <b>登录</b>，自动换取并保存 JWT；约 3 小时过期后会用保存的密码自动重新登录，无需再管。
            </div>
            <div id="accountRows"></div>
            <div class="config-actions">
              <button class="btn btn-sm" onclick="addAccountRow()">+ 添加账号</button>
              <button class="btn btn-sm btn-primary" onclick="saveConfig()">保存配置</button>
              <span class="save-status" id="saveStatus">已保存</span>
            </div>
          </div>
        </div>
      </div>
      <div class="layout-right">
        <div class="account-collapse">
          <div class="account-collapse-header" onclick="toggleAccounts()">
            <span class="title">账号明细</span>
            <span class="hint">展开/收起</span>
            <span class="collapse-icon" id="accountsIcon">▼</span>
          </div>
          <div class="account-collapse-body" id="accountsBody">
            <div id="accounts" style="padding: 8px 0 0;"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
var REFRESH_SEC = 300;
var countdown = REFRESH_SEC;

function toggleConfig() {
  var body = document.getElementById('configBody');
  var icon = document.getElementById('configIcon');
  if (body.style.display === 'none') { body.style.display = 'block'; icon.classList.remove('collapsed'); }
  else { body.style.display = 'none'; icon.classList.add('collapsed'); }
}

function toggleAccounts() {
  var body = document.getElementById('accountsBody');
  var icon = document.getElementById('accountsIcon');
  if (body.classList.contains('expanded')) { body.classList.remove('expanded'); icon.classList.add('collapsed'); }
  else { body.classList.add('expanded'); icon.classList.remove('collapsed'); }
}

function eyeSvg(hidden) {
  return '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"></path>' +
    '<circle cx="12" cy="12" r="3"></circle>' +
    (hidden ? '<line x1="4" y1="4" x2="20" y2="20"></line>' : '') +
    '</svg>';
}

function togglePool(btn, key) {
  var body = btn.closest('.pool-card').querySelector('.pool-body');
  var show = (body.style.display === 'none');
  body.style.display = show ? '' : 'none';
  btn.innerHTML = eyeSvg(!show);
  btn.title = show ? '隐藏该积分池' : '显示该积分池';
  try { localStorage.setItem('snPoolHidden:' + key, show ? '0' : '1'); } catch(e) {}
}

function escAttr(s) { return escHtml(String(s == null ? '' : s)).replace(/'/g, '&#39;'); }

function fmt(n) {
  n = parseFloat(n) || 0;
  if (n >= 1000000) return (n/1000000).toFixed(1) + 'M';
  if (n >= 1000) return (n/1000).toFixed(1) + 'K';
  return Math.round(n).toString();
}

function buildGauge(id, pct, usedStr, totalStr, remainStr, color, title, detail, resetTs) {
  var r = 54;
  var c = 2 * Math.PI * r;
  var offset = c * (1 - Math.min(pct, 100) / 100);
  var refreshHtml = '';
  if (resetTs && resetTs > 0) {
    var dt = new Date(resetTs * 1000);
    var dateStr = (dt.getMonth()+1) + '月' + dt.getDate() + '日 ' +
      (dt.getHours()<10?'0':'') + dt.getHours() + ':' +
      (dt.getMinutes()<10?'0':'') + dt.getMinutes();
    var now = new Date();
    var diffMs = dt - now;
    var countdownStr = '';
    if (diffMs > 0) {
      var diffMin = Math.floor(diffMs / 60000);
      if (diffMin >= 60) {
        var h = Math.floor(diffMin / 60);
        var m = diffMin % 60;
        countdownStr = h + '小时' + (m > 0 ? m + '分' : '') + '后';
      } else {
        countdownStr = diffMin + '分钟后';
      }
    } else {
      countdownStr = '已到刷新时间';
    }
    refreshHtml = '<div class="gauge-next-refresh">' +
      '<span class="nr-time">下次刷新: ' + dateStr + '</span>' +
      '<span class="nr-countdown">（' + countdownStr + '）</span>' +
    '</div>';
  }
  return '<div class="gauge-wrap">' +
    '<div class="gauge-title">' + title + '</div>' +
    '<div class="gauge">' +
      '<svg width="130" height="130">' +
        '<circle class="bg-ring" cx="65" cy="65" r="' + r + '"></circle>' +
        '<circle class="fg-ring" cx="65" cy="65" r="' + r + '" stroke="' + color + '" stroke-dasharray="' + c + '" stroke-dashoffset="' + c + '" id="' + id + '"></circle>' +
      '</svg>' +
      '<div class="center">' +
        '<div class="pct">' + pct.toFixed(1) + '<span class="unit">%</span></div>' +
        '<div class="label">已用 ' + usedStr + ' / ' + totalStr + '</div>' +
        '<div class="label">剩余 ' + remainStr + '</div>' +
      '</div>' +
    '</div>' +
    '<div class="gauge-detail">' + detail + '</div>' +
    refreshHtml +
  '</div>';
}

function colorForPct(pct) {
  if (pct < 60) return '#52c41a';
  if (pct < 85) return '#faad14';
  return '#f5222d';
}

function addAccountRow(username, password, token) {
  username = username || ''; password = password || ''; token = token || '';
  var container = document.getElementById('accountRows');
  var div = document.createElement('div');
  div.className = 'config-row';
  div.innerHTML = '<div class="col-user"><input type="text" placeholder="用户名" value="' + escHtml(username) + '"></div>' +
    '<div class="col-pass"><input type="password" placeholder="密码" value="' + escHtml(password) + '"></div>' +
    '<div class="col-token"><input type="hidden" value="' + escHtml(token) + '"></div>' +
    '<div class="col-actions"><button class="btn btn-sm btn-primary" onclick="loginAccount(this)">登录</button><button class="btn btn-sm" onclick="removeRow(this)" style="color:#f5222d">删</button></div>';
  container.appendChild(div);
}

function removeRow(btn) { btn.closest('.config-row').remove(); }

function escHtml(s) { if (!s) return ''; return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function collectAccounts() {
  var rows = document.querySelectorAll('#accountRows .config-row');
  var accounts = [];
  rows.forEach(function(row) {
    var username = row.querySelector('.col-user input').value.trim();
    var password = row.querySelector('.col-pass input').value;
    var token = row.querySelector('.col-token input').value.trim();
    if (username) accounts.push({username: username, password: password, jwt_token: token});
  });
  return accounts;
}

async function loginAccount(btn) {
  var row = btn.closest('.config-row');
  var username = row.querySelector('.col-user input').value.trim();
  var password = row.querySelector('.col-pass input').value;
  if (!username || !password) { alert('请先填写用户名和密码'); return; }
  btn.disabled = true; btn.textContent = '登录中...';
  try {
    var resp = await fetch('/api/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({username:username, password:password})});
    var data = await resp.json();
    if (data.success) {
      row.querySelector('.col-token input').value = data.token;
      btn.textContent = '已登录'; btn.style.color = '#52c41a';
      await saveConfig();
    } else { btn.textContent = '失败'; btn.style.color = '#f5222d'; alert('登录失败: ' + (data.error || '未知')); }
  } catch(e) { btn.textContent = '失败'; btn.style.color = '#f5222d'; }
  finally { setTimeout(function() { btn.disabled = false; if (btn.textContent === '登录中...') { btn.textContent = '登录'; btn.style.color = ''; } }, 3000); }
}

async function saveConfig() {
  var accounts = collectAccounts();
  var resp = await fetch('/api/accounts', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({accounts: accounts})});
  var data = await resp.json();
  if (data.success) { var s = document.getElementById('saveStatus'); s.classList.add('show'); setTimeout(function() { s.classList.remove('show'); }, 2000); fetchUsage(); }
  else { alert('保存失败: ' + (data.error || '')); }
}

async function loadConfigRows() {
  var resp = await fetch('/api/accounts');
  var data = await resp.json();
  var container = document.getElementById('accountRows');
  container.innerHTML = '';
  if (data.accounts && data.accounts.length > 0) data.accounts.forEach(function(a) { addAccountRow(a.username||'', a.password||'', a.jwt_token||''); });
  else addAccountRow();
}

async function fetchUsage() {
  var btn = document.getElementById('refreshBtn');
  btn.disabled = true; btn.textContent = '查询中...';
  document.getElementById('refreshInfo').textContent = '查询中...';
  try {
    var resp = await fetch('/api/usage');
    var data = await resp.json();
    renderData(data);
    countdown = REFRESH_SEC;
  } catch(e) {
    document.getElementById('loading').innerHTML = '<p style="color:#f5222d">查询失败: ' + e.message + '</p>';
    document.getElementById('loading').style.display = 'block';
    document.getElementById('content').style.display = 'none';
  } finally { btn.disabled = false; btn.textContent = '刷新'; }
}

function renderData(data) {
  document.getElementById('loading').style.display = 'none';
  document.getElementById('content').style.display = 'block';
  var accounts = data.accounts;

  // 汇总：所有账号通用积分池（pool_type=default）的 5h / 7d 数据之和
  var total5h = 0, used5h = 0, remain5h = 0;
  var total7d = 0, used7d = 0, remain7d = 0;
  var acctCount = 0, okCount = 0, errCount = 0;
  var earliest5hReset = 0;
  var earliest7dReset = 0;

  accounts.forEach(function(a) {
    if (a.error) { errCount++; return; }
    okCount++;
    acctCount++;
    a.items.forEach(function(item) {
      if (item.pool_type !== 'default') return;
      total5h += parseFloat(item.limit_5h.replace(/,/g,'')) || 0;
      used5h += parseFloat(item.used_5h.replace(/,/g,'')) || 0;
      remain5h += parseFloat(item.remain_5h.replace(/,/g,'')) || 0;
      total7d += parseFloat(item.limit_7d.replace(/,/g,'')) || 0;
      used7d += parseFloat(item.used_7d.replace(/,/g,'')) || 0;
      remain7d += parseFloat(item.remain_7d.replace(/,/g,'')) || 0;
      // 取所有账号中最小的 reset_at 时间戳
      var t5 = item.reset_at_ts || 0;
      if (t5 > 0 && (earliest5hReset === 0 || t5 < earliest5hReset)) earliest5hReset = t5;
      var t7 = item.reset_at_7d_ts || 0;
      if (t7 > 0 && (earliest7dReset === 0 || t7 < earliest7dReset)) earliest7dReset = t7;
    });
  });

  var pct5h = total5h > 0 ? (used5h / total5h * 100) : 0;
  var pct7d = total7d > 0 ? (used7d / total7d * 100) : 0;
  var col5h = colorForPct(pct5h);
  var col7d = colorForPct(pct7d);

  // 渲染进度球（每个球下方显示自己的下次刷新时间）
  var gh = '';
  gh += buildGauge('ring5h', pct5h, fmt(used5h), fmt(total5h), fmt(remain5h), col5h, '5小时窗口', okCount + ' 个账号合计', earliest5hReset);
  gh += buildGauge('ring7d', pct7d, fmt(used7d), fmt(total7d), fmt(remain7d), col7d, '7天窗口', okCount + ' 个账号合计', earliest7dReset);
  document.getElementById('gauges').innerHTML = gh;

  // 动画填充进度环
  setTimeout(function() {
    var r5 = document.getElementById('ring5h');
    var r7 = document.getElementById('ring7d');
    if (r5) {
      var c = 2 * Math.PI * 54;
      r5.setAttribute('stroke-dashoffset', c * (1 - Math.min(pct5h,100)/100));
    }
    if (r7) {
      var c2 = 2 * Math.PI * 54;
      r7.setAttribute('stroke-dashoffset', c2 * (1 - Math.min(pct7d,100)/100));
    }
  }, 100);

  // 渲染折叠的账号明细（卡片式，竖向布局，无需横向滚动）
  var h = '';
  if (errCount > 0) h += '<div class="tip">部分账号 JWT 可能已过期，点击"账号配置"展开更新 Token</div>';
  accounts.forEach(function(a) {
    if (a.error) {
      var cls = (a.error.indexOf('过期')>=0 || a.error.indexOf('未配置')>=0) ? 'badge-warn' : 'badge-err';
      h += '<div class="account-card"><div class="account-header"><span class="name">' + escHtml(a.username || '未知') + '</span><span class="badge ' + cls + '">' + escHtml(a.error) + '</span></div></div>';
      return;
    }
    if (!a.items.length) { h += '<div class="account-card"><div class="account-header"><span class="name">' + escHtml(a.username || '未知') + '</span><span class="badge badge-ok">无资源包</span></div></div>'; return; }
    h += '<div class="account-card"><div class="account-header"><span class="name">' + escHtml(a.username || '未知') + '</span><span class="badge badge-ok">' + a.items.length + ' 个积分池</span></div>';
    a.items.forEach(function(item) {
      var pct = item.usage_pct_5h;
      var cls = pct<60?'green':(pct<85?'yellow':'red');
      var pct7 = item.usage_pct_7d;
      var cls7 = pct7<60?'green':(pct7<85?'yellow':'red');
      var tagCls = item.pool_type==='default'?'tag-default':'tag-dedicated';
      var tagText = item.pool_type==='default'?'通用':'专属';
      var grantText = item.grant_balance;
      if (item.nearest_expiry) grantText += ' (至' + item.nearest_expiry + ')';
      var poolKey = (a.username||'') + '|' + (item.pool_type||'') + '|' + item.pool_name;
      var poolHidden = false;
      try { poolHidden = localStorage.getItem('snPoolHidden:' + poolKey) === '1'; } catch(e) {}
      h += '<div class="pool-card">';
      h += '<div class="pool-head"><span class="pool-name"><span class="tag '+tagCls+'">'+tagText+'</span>'+escHtml(item.pool_name)+'</span>' +
        '<button class="pool-eye" title="' + (poolHidden ? '显示该积分池' : '隐藏该积分池') + '" onclick="togglePool(this, \'' + escAttr(poolKey) + '\')">' + eyeSvg(poolHidden) + '</button></div>';
      h += '<div class="pool-body"' + (poolHidden ? ' style="display:none;"' : '') + '>';
      h += '<div class="pool-block">';
      h += '<div class="pool-stat"><span class="k">5小时用量</span><span class="v">'+item.used_5h+' / '+item.limit_5h+'</span></div>';
      h += '<div class="pool-bar"><span class="pool-bar-fill '+cls+'" style="width:'+Math.min(pct,100)+'%"></span></div>';
      h += '<div class="pool-stat"><span class="k">剩余 / 使用率</span><span class="v">'+item.remain_5h+' · '+pct+'%</span></div>';
      h += '</div>';
      h += '<div class="pool-block">';
      h += '<div class="pool-stat"><span class="k">7天用量</span><span class="v">'+item.used_7d+' / '+item.limit_7d+'</span></div>';
      h += '<div class="pool-bar"><span class="pool-bar-fill '+cls7+'" style="width:'+Math.min(pct7,100)+'%"></span></div>';
      h += '<div class="pool-stat"><span class="k">剩余 / 使用率</span><span class="v">'+item.remain_7d+' · '+pct7+'%</span></div>';
      h += '</div>';
      h += '<div class="pool-stat"><span class="k">赠送余额</span><span class="v" style="font-size:11px;color:#888;font-weight:400">'+grantText+'</span></div>';
      h += '<div class="pool-stat"><span class="k">刷新时间 5h / 7d</span><span class="v" style="font-size:11px;color:#888;font-weight:400">'+item.reset_at+' / '+item.reset_at_7d+'</span></div>';
      h += '</div>';
      h += '</div>';
    });
    h += '</div>';
  });
  document.getElementById('accounts').innerHTML = h;
}

function card(label, value, color) { return '<div class="summary-card"><div class="label">'+label+'</div><div class="value" style="'+(color?'color:'+color:'')+'">'+value+'</div></div>'; }

function tick() {
  countdown--;
  if (countdown <= 0) { fetchUsage(); return; }
  var m = Math.floor(countdown/60); var s = countdown%60;
  document.getElementById('refreshInfo').textContent = m+':'+(s<10?'0':'')+s+' 后自动刷新';
}

loadConfigRows().then(function() { fetchUsage(); });
setInterval(tick, 1000);
</script>
</body>
</html>
"""


# ============================================================
# HTTP 服务器
# ============================================================
class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif self.path.startswith("/api/usage"):
            try:
                result = query_all_accounts()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/accounts"):
            accounts = load_accounts()
            safe = [{"username": a.get("username", ""),
                     "password": a.get("password", ""), "jwt_token": a.get("jwt_token", "")} for a in accounts]
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({"accounts": safe}, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_error(404)

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        if self.path.startswith("/api/accounts"):
            try:
                data = json.loads(body)
                accounts = data.get("accounts", [])
                save_accounts(accounts)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": True}, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"success": False, "error": str(e)}, ensure_ascii=False).encode("utf-8"))

        elif self.path.startswith("/api/login"):
            try:
                data = json.loads(body)
                username = (data.get("username") or "").strip()
                password = data.get("password") or ""
                if not username or not password:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(
                        {"success": False, "error": "请填写用户名和密码"}, ensure_ascii=False).encode("utf-8"))
                    return
                timeout = REQUEST_TIMEOUT
                res = auth_login.login(username, password, timeout)
                token = res.get("access_token", "")
                if not token:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(json.dumps(
                        {"success": False, "error": "登录成功但未返回 token"}, ensure_ascii=False).encode("utf-8"))
                    return
                update_account(username, jwt_token=token, password=password,
                              token_acquired_at=time.time(),
                              token_expires_in=res.get("expires_in") or 10800)
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"success": True, "token": token, "expires_in": res.get("expires_in")},
                    ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"success": False, "error": str(e)[:200]}, ensure_ascii=False).encode("utf-8"))

        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


# ============================================================
# 主流程
# ============================================================
def start_server():
    """启动 HTTP 服务器（后台线程）"""
    global _server, _server_port
    _server = ThreadingHTTPServer(("127.0.0.1", SERVER_PORT), DashboardHandler)
    _server_port = _server.server_address[1]
    _server.serve_forever()


def main():
    # 首次运行若旁边没有配置文件，写入默认模板（实现开箱即用）
    ensure_config_files()

    # 启动后台 HTTP 服务
    server_thread = threading.Thread(target=start_server, daemon=True)
    server_thread.start()

    # 等服务就绪
    while _server_port is None:
        time.sleep(0.1)

    url = f"http://127.0.0.1:{_server_port}"
    print(f"Server ready at {url}")

    # 创建原生窗口
    window = webview.create_window(
        title="SenseNova 用量查询",
        url=url,
        width=400,
        height=720,
        min_size=(400, 500),
        text_select=True,
    )

    # 启动窗口（阻塞，关闭窗口即退出程序）
    webview.start()

    # 窗口关闭后退出
    if _server:
        _server.shutdown()
    sys.exit(0)


if __name__ == "__main__":
    main()
