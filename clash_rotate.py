"""通过 Clash/mihomo external-controller API 在注册任务间轮换代理节点。"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_lock = threading.Lock()
_state = {"started": False}
# 节点失败反馈黑名单: node -> blacklist_until (unix ts)
_node_blacklist = {}
NODE_FAILURE_COOLDOWN_SEC = 900

# 连通探测: 通过 mihomo delay API 让节点真实访问目标站，排除"挂着但实际不通"的节点。
# 注意 accounts.x.ai 对 delay 探测会间歇 503（即使节点实际可用），故做成降级链:
# 先探测注册站本身，失败再降级到通用连通探测（gstatic 204）。
PROBE_URL = "https://accounts.x.ai"
PROBE_FALLBACK_URL = "https://www.gstatic.com/generate_204"
PROBE_TIMEOUT_MS = 4000
# 探测结果缓存: node -> (alive, expire_ts)；活的缓存 90s，死的缓存 240s
_probe_cache = {}
_probe_cache_lock = threading.Lock()


class ClashRotateError(RuntimeError):
    pass


def _desktop_headers(secret):
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _request(url, secret, method="GET", payload=None, timeout=6):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = _desktop_headers(secret)
    if url.startswith("unix://"):
        # unix:///path/to/verge-mihomo.sock/v1/... —— Clash Verge Rev 的官方控制通道
        # （新版客户端默认只开 unix socket，TCP external-controller 可能被移除）
        import socket as _socket
        rest = url[len("unix://"):]  # /run/.../verge-mihomo.sock/proxies
        end = rest.find(".sock")
        if end > 0:
            end += len(".sock")
            sock_path, path = rest[:end], rest[end:] or "/"
        else:
            sock_path, path = rest, "/"
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(sock_path)
        try:
            payload_bytes = body or b""
            head_lines = [
                "%s %s HTTP/1.1" % (method, path),
                "Host: localhost",
                "Content-Length: %d" % len(payload_bytes),
                "Connection: close",
            ]
            head_lines.extend("%s: %s" % item for item in headers.items())
            request_text = "\r\n".join(head_lines) + "\r\n\r\n"
            sock.sendall(request_text.encode("latin1") + payload_bytes)
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            raw = b"".join(chunks)
        finally:
            sock.close()
        if b"\r\n\r\n" not in raw:
            raise ClashRotateError("unix-socket API 无响应")
        head, _, payload_out = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin1", "ignore")
        status = int(status_line.split()[1]) if len(status_line.split()) > 1 else 0
        # 处理 Transfer-Encoding: chunked（mihomo 对大响应默认分块）
        if b"chunked" in head.lower():
            decoded = b""
            cursor = 0
            while cursor < len(payload_out):
                line_end = payload_out.find(b"\r\n", cursor)
                if line_end < 0:
                    break
                try:
                    size = int(payload_out[cursor:line_end].split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    break
                decoded += payload_out[line_end + 2: line_end + 2 + size]
                cursor = line_end + 2 + size + 2
            payload_out = decoded
        parsed = json.loads((payload_out or b"{}").decode("utf-8") or "{}")
        if status in (200, 204):
            return parsed
        raise ClashRotateError("unix-socket API HTTP %s: %s" % (status, str(parsed)[:120]))
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads((resp.read() or b"{}").decode("utf-8") or "{}")


def _is_node(api_group_names, member):
    """成员是具体节点（而非指向别的策略组的引用）才会参与轮换。"""
    return member not in api_group_names and member not in ("DIRECT", "REJECT")


_GROUP_TYPES = {"selector", "urltest", "fallback"}


def list_rotate_nodes(settings):
    allowed = [n.strip() for n in str(settings.get("clash_rotate_nodes", "") or "").split(",") if n.strip()]
    if allowed:
        return allowed
    api, secret, group = settings["clash_api_url"], settings["clash_api_secret"], settings["clash_proxy_group"]
    data = _request(api + "/proxies", secret)
    proxies = data.get("proxies") or {}
    group_detail = proxies.get(group)
    if not group_detail:
        raise ClashRotateError(f"Clash 中未找到策略组: {group}")
    nodes = [
        member
        for member in group_detail.get("all", [])
        if str((proxies.get(member) or {}).get("type", "")).lower() not in _GROUP_TYPES
        and member not in ("DIRECT", "REJECT", "PASS", "COMPATIBLE")
        and "自动" not in member and "故障" not in member
    ]
    if not nodes:
        raise ClashRotateError(f"策略组 {group} 中没有可参与轮换的具体节点")
    return nodes


def blacklist_node(node):
    """注册在节点上失败后冷却该节点（由注册引擎反馈调用）。"""
    if not node:
        return
    with _lock:
        _node_blacklist[node] = time.time() + NODE_FAILURE_COOLDOWN_SEC


def _delay_probe(api, secret, node, url):
    path = "/proxies/%s/delay?timeout=%d&url=%s" % (
        urllib.parse.quote(node, safe=""), PROBE_TIMEOUT_MS, urllib.parse.quote(url, safe=""))
    try:
        data = _request(api + path, secret, timeout=PROBE_TIMEOUT_MS / 1000 + 3)
        return int((data or {}).get("delay") or 0) > 0
    except Exception:
        return False


def probe_node(settings, node, log_callback=None):
    """探测节点连通性（注册站优先，通用连通性兜底；结果带缓存）。"""
    if not node:
        return False
    now = time.time()
    with _probe_cache_lock:
        cached = _probe_cache.get(node)
        if cached and cached[1] > now:
            return cached[0]
    api, secret = settings["clash_api_url"], settings["clash_api_secret"]
    alive = _delay_probe(api, secret, node, PROBE_URL)
    if not alive:
        # x.ai 探测报错不代表节点挂（它经常对 prober 503）——只要求节点能正常上网
        alive = _delay_probe(api, secret, node, PROBE_FALLBACK_URL)
    if not alive and log_callback:
        log_callback(f"[Debug] 节点探测均失败: {node[:36]}")
    with _probe_cache_lock:
        _probe_cache[node] = (alive, now + (90 if alive else 240))
    return alive


def discover_all_nodes(settings):
    """收集当前 Clash 配置中所有可轮换的具体节点（跨全部策略组）。

    用于:1) 探测兜底（组内全死时看看别的组） 2) 未配置白名单时的自动发现
    （把第二个订阅合并进运行配置后，其节点会自动进入候选池）。
    """
    api, secret = settings["clash_api_url"], settings["clash_api_secret"]
    data = _request(api + "/proxies", secret)
    proxies = data.get("proxies") or {}
    group_names = {
        name for name, p in proxies.items()
        if str(p.get("type") or "").lower() in _GROUP_TYPES or name == "GLOBAL"
    }
    nodes = []
    for member, p in proxies.items():
        if member in group_names or member in ("DIRECT", "REJECT", "PASS", "COMPATIBLE"):
            continue
        if str(p.get("type") or "").lower() in _GROUP_TYPES:
            continue
        if "自动" in member or "故障" in member:
            continue
        nodes.append(member)
    return nodes


def pick_alive_node(settings, candidates, log_callback=None):
    """按顺序探测候选节点，返回第一个连通的；全部不通返回 None（结果带缓存）。"""
    for node in candidates:
        if probe_node(settings, node, log_callback=log_callback):
            if log_callback:
                log_callback(f"[*] 节点探测连通: {node[:40]}")
            return node
    return None


def emergency_switch(settings, dead_node=None, probe_url=None, log_callback=None, include_other_groups=True):
    """连接级失败连环出现时调用的紧急切换：
    1. 拉黑挂掉的节点  2. 探测候选组内其它节点并切过去
    3. 组内全死时跨组发现并探测  4. 全部不通返回 ("all_dead", None)

    返回 ("switched", node) 或 ("all_dead", None)。
    """
    if dead_node:
        with _lock:
            _node_blacklist[dead_node] = time.time() + NODE_FAILURE_COOLDOWN_SEC
    try:
        candidates = _available_nodes(list_rotate_nodes(settings))
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] 紧急切换失败（候选列表获取失败）: {exc}")
        return "all_dead", None
    alive = pick_alive_node(settings, candidates, log_callback=log_callback)
    if alive is None and include_other_groups:
        # 组内候选全挂 → 跨全部策略组发现更多节点（覆盖合并进来的第二个订阅）
        try:
            extra = [n for n in discover_all_nodes(settings) if n not in candidates and n != dead_node]
            if extra:
                if log_callback:
                    log_callback(f"[*] 组内节点均不通，跨组发现 {len(extra)} 个候选（含其它订阅），逐一探测…")
                alive = pick_alive_node(settings, _available_nodes(extra), log_callback=log_callback)
        except Exception as exc:
            if log_callback:
                log_callback(f"[Debug] 跨组发现失败: {exc}")
    if alive is None:
        if log_callback:
            log_callback("[!] 所有候选节点探测均不通（可能整条线路/光猫/出口故障）")
        return "all_dead", None
    try:
        group = settings["clash_proxy_group"]
        prev = current_group_node(settings)
        api, secret = settings["clash_api_url"], settings["clash_api_secret"]
        _request(
            api + f"/proxies/{urllib.parse.quote(group, safe='')}",
            secret,
            method="PUT",
            payload={"name": alive},
        )
        _state["started"] = True
        _clear_probe_cache()
        if log_callback:
            log_callback(f"[*] 紧急切换节点: {prev} -> {alive}（探测连通 delay 正常）")
        return "switched", alive
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] 紧急切换写入失败: {exc}")
        return "all_dead", None


def _clear_probe_cache():
    with _probe_cache_lock:
        _probe_cache.clear()


def clear_probe_cache():
    """公开入口：配置变更/手动干预后清空探测缓存。"""
    _clear_probe_cache()


def _available_nodes(candidates):
    now = time.time()
    usable = [n for n in candidates if _node_blacklist.get(n, 0) <= now]
    if usable:
        return usable
    # 全部被黑名单时清空（长冷却后允许全部复活重试）
    _node_blacklist.clear()
    return candidates


def rotate_next(settings, log_callback=None, probe=True):
    """把策略组切到下一个具体节点，返回 (旧节点, 新节点)。失败返回 (None, None)。

    probe=True 时只有真实连通（delay API 通过）的节点才会上车；
    全部候选探测不通返回 (None, None) 并记录 all_dead 状态（was_all_dead() 可查）。
    """
    try:
        with _lock:
            nodes = _available_nodes(list_rotate_nodes(settings))
            api, secret, group = settings["clash_api_url"], settings["clash_api_secret"], settings["clash_proxy_group"]
            detail = _request(f"{api}/proxies/{urllib.parse.quote(group, safe='')}", secret)
            current = str(detail.get("now") or "")
            if current in nodes:
                index = nodes.index(current)
                ordered = nodes[index + 1:] + nodes[:index + 1]
                prev = current
            else:
                ordered = [n for n in nodes if n != current] or nodes
                prev = current
            if probe:
                probe_dead = [n for n in ordered if not probe_node(settings, n, log_callback=log_callback)]
                candidates = [n for n in ordered if n not in probe_dead]
                if not candidates:
                    _last_rotate["all_dead"] = True
                    if log_callback:
                        log_callback("[!] 轮换探测: 候选节点均不通，保持不动等待失败反馈处理")
                    return None, None
                ordered = candidates
            new_node = ordered[0]
            _request(
                api + f"/proxies/{urllib.parse.quote(group, safe='')}",
                secret,
                method="PUT",
                payload={"name": new_node},
            )
            _state["started"] = True
            _last_rotate["all_dead"] = False
            return prev, new_node
    except Exception as exc:
        if log_callback:
            log_callback(f"[!] Clash 节点轮换失败（跳过本次）: {exc}")
        return None, None


def was_all_dead():
    with _lock:
        return bool(_last_rotate.get("all_dead"))


_last_rotate = {"all_dead": False}


def current_group_node(settings):
    try:
        group = settings["clash_proxy_group"]
        detail = _request(
            settings["clash_api_url"] + f"/proxies/{urllib.parse.quote(group, safe='')}",
            settings["clash_api_secret"],
        )
        return str(detail.get("now") or "")
    except Exception:
        return ""
