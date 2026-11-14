"""提供共享的 HTTP 请求、代理处理和 Chromium 启动参数。"""
import os
import threading
import urllib.parse

from DrissionPage import ChromiumOptions
from curl_cffi import requests
from curl_cffi import CurlOpt

CURL_IPRESOLVE_V4 = 1
"""强制 curl 走 IPv4：临时邮箱等直连目标存在 IPv6 破路时兜底；经代理的连接不受影响。"""

DRIESSION_DATA_MARKER = "/tmp/DrissionPage"


def sweep_orphan_browsers(log_callback=None):
    """清理被 kill 的引擎遗留的浏览器进程，返回被清掉的浏览器主进程 pid 列表。

    识别特征：chromium 主进程 cmdline 含 --remote-debugging-port 且 user-data-dir
    位于 /tmp/DrissionPage（仅本工具创建的数据目录），且其父进程不是存活的
    项目引擎（grok_register_ttk / supervisor）。只会杀本工具启动的浏览器，
    不会碰用户自己的 Chrome（无该数据目录）。
    """
    import psutil

    killed = []
    try:
        procs = list(psutil.process_iter(["pid", "cmdline"]))
    except Exception:
        return killed

    def _engine_alive(pid):
        try:
            p = psutil.Process(pid)
            name = (p.name() or "").lower()
            if "python" not in name:
                return False
            cmd = " ".join(p.cmdline() or [])
            return "grok_register_ttk" in cmd or "supervisor.py" in cmd
        except Exception:
            return False

    for proc in procs:
        try:
            info = proc.info or {}
            cmd = " ".join(info.get("cmdline") or [])
            if DRIESSION_DATA_MARKER not in cmd:
                continue
            if "--remote-debugging-port" not in cmd:
                # 子进程会随主进程一起被 kill tree，跳过
                continue
            parent_pid = proc.ppid()
            if parent_pid and _engine_alive(parent_pid):
                continue  # 当前引擎正在使用，勿动
            victims = [proc] + proc.children(recursive=True)
            for victim in victims:
                try:
                    victim.kill()
                except Exception:
                    pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            killed.append(info.get("pid"))
        except psutil.NoSuchProcess:
            continue
        except Exception:
            continue
    if killed and log_callback:
        log_callback(f"[*] 已清理 {len(killed)} 个遗留浏览器 (pid: {killed})")
    return killed

_OSSL_SESSION = None
_OSSL_LOCK = threading.Lock()


def _is_openssl_fallback_worthy(exc, request_kwargs):
    """curl TLS 握手失败时切换到 OpenSSL 客户端兜底。

    curl_cffi 基于 BoringSSL；个别站点（如临时邮箱源）会间歇性拒绝其握手，
    而 OpenSSL 客户端同一时刻可正常访问。仅对直连请求兜底，避免掩盖代理故障。
    """
    if request_kwargs.get("proxies"):
        return False
    text = str(exc)
    return "TLS connect error" in text or "unexpected eof" in text.lower()


def _openssl_request(method, url, request_kwargs):
    global _OSSL_SESSION
    with _OSSL_LOCK:
        if _OSSL_SESSION is None:
            import requests as ssl_requests

            _OSSL_SESSION = ssl_requests.Session()
            _OSSL_SESSION.trust_env = False
    kwargs = dict(request_kwargs)
    kwargs.pop("curl_options", None)
    kwargs["timeout"] = max(float(kwargs.get("timeout") or 0), 30.0)
    return _OSSL_SESSION.request(method, url, **kwargs)
from cpa_xai.proxyutil import (
    LocalAuthProxyBridge,
    prepare_chromium_proxy,
    proxy_for_chromium,
)
from proxy_bridge import (
    parse_proxy_url as _parse_proxy_url,
    proxy_has_auth as _proxy_has_auth,
    safe_proxy_port as _safe_proxy_port,
)
from proxy_pool import (
    ProxyTransportError,
    current_proxy_lease,
    managed_proxy_active,
    safe_proxy_error_text,
)

_config = {}
_extension_path = ""


def _legacy_extension_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "turnstilePatch")


def _resolve_extension_path(explicit=None):
    if explicit is not None:
        candidate = str(explicit or "").strip()
        return candidate if candidate and os.path.isdir(candidate) else ""
    configured = str(_extension_path or "").strip()
    if configured and os.path.isdir(configured):
        return configured
    legacy = _legacy_extension_path()
    return legacy if os.path.isdir(legacy) else ""


def configure_runtime(config_ref, extension_path=""):
    global _config, _extension_path
    _config = config_ref
    _extension_path = str(extension_path or "")


def get_configured_proxy():
    lease = current_proxy_lease()
    if lease is not None:
        return str(lease.proxy_url or "").strip()
    mode = str(_config.get("proxy_mode", "auto") or "auto").strip().lower()
    if mode == "direct" or mode in ("single", "pool"):
        return ""
    return str(_config.get("proxy", "") or "").strip()


def get_proxies():
    proxy = get_configured_proxy()
    return {"http": proxy, "https": proxy} if proxy else {}


def _strip_proxy_auth(proxy):
    raw = str(proxy or "").strip()
    parsed = _parse_proxy_url(raw)
    if not parsed or not parsed.hostname:
        return raw
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    port = _safe_proxy_port(parsed)
    netloc = "%s:%s" % (host, port) if port else host
    stripped = urllib.parse.urlunsplit((parsed.scheme or "http", netloc, parsed.path, parsed.query, parsed.fragment))
    return stripped.split("://", 1)[1] if "://" not in raw else stripped


def _proxy_endpoint_terms(proxy=None):
    parsed = _parse_proxy_url(proxy or get_configured_proxy())
    if not parsed or not parsed.hostname:
        return []
    terms = [parsed.hostname]
    port = _safe_proxy_port(parsed)
    if port:
        terms.extend(["%s:%s" % (parsed.hostname, port), "port %s" % port])
    return [item.lower() for item in terms if item]


def is_proxy_connection_error(exc):
    if not get_configured_proxy():
        return False
    err = str(exc or "").lower()
    if not err:
        return False
    if any(item in err for item in ("proxy", "tunnel", "socks")):
        return True
    markers = (
        "could not connect", "failed to connect", "connection refused",
        "connection reset", "connect error", "timed out", "timeout",
    )
    if any(item in err for item in markers):
        terms = _proxy_endpoint_terms()
        return not terms or any(term in err for term in terms)
    return False


def page_has_proxy_error(page_obj):
    try:
        url = str(getattr(page_obj, "url", "") or "")
        title = str(page_obj.run_js("return document.title || ''") or "")
        body = str(page_obj.run_js("return document.body ? document.body.innerText.slice(0, 2000) : ''") or "")
    except Exception:
        return False
    text = "%s\n%s\n%s" % (url, title, body)
    text = text.lower()
    failed = any(marker in text for marker in (
        "err_proxy", "proxy connection failed", "proxy server",
        "proxy authentication", "tunnel connection failed",
        "无法连接到代理服务器", "代理服务器",
        # 通用浏览器级连接失败（节点不通也会走到这里）
        "err_connection", "err_timed", "err_internet_disconnected",
        "err_name_not_resolved", "err_address_unreachable",
        "err_empty_response", "err_tunnel", "err_network",
        "can't be reached", "cannot be reached", "isn't reachable",
        "无法访问此网站", "网站无法访问", "无法访问此页面",
    ))
    # 配置了代理（managed 或 legacy auto+proxy）时视为代理传输失败，交由引擎按租约策略处理
    if failed and (managed_proxy_active() or bool(get_configured_proxy())):
        raise ProxyTransportError("Chromium 页面显示连接失败/代理错误: %s" % (title or url))
    return failed


def prepare_browser_proxy(use_proxy=True, log_callback=None):
    # Managed registration leases must never silently switch to another IP in
    # the middle of an account. Legacy auto mode keeps the historical direct
    # fallback behavior when callers explicitly pass use_proxy=False.
    if managed_proxy_active():
        use_proxy = True
    proxy = get_configured_proxy()
    if not use_proxy or not proxy:
        return "", None
    logger = None
    if log_callback:
        logger = lambda message: log_callback("[*] 已为 Chromium启动本地认证代理桥: %s" % message.split(": ", 1)[-1]) if "started authenticated proxy bridge" in message else log_callback(message)
    return prepare_chromium_proxy(proxy, log=logger)


def apply_browser_proxy_option(options, proxy):
    if not proxy:
        return
    if hasattr(options, "set_proxy"):
        try:
            options.set_proxy(proxy)
            return
        except Exception:
            pass
    if not hasattr(options, "set_argument"):
        raise AttributeError("当前 DrissionPage ChromiumOptions 不支持设置浏览器代理")
    try:
        options.set_argument("--proxy-server=%s" % proxy)
    except TypeError:
        options.set_argument("--proxy-server", proxy)


def _configure_browser_environment(options):
    browser_path = str(os.environ.get("CHROME_PATH") or "").strip()
    if browser_path:
        browser_path = os.path.abspath(os.path.expanduser(browser_path))
        if not os.path.isfile(browser_path):
            raise FileNotFoundError("CHROME_PATH does not exist: %s" % browser_path)
        options.set_browser_path(browser_path)

    if os.environ.get("GROK_REGISTER_CHROMIUM_HEADLESS") == "1":
        options.headless(True)
    if os.environ.get("GROK_REGISTER_CHROMIUM_NO_SANDBOX") == "1":
        options.set_argument("--no-sandbox")
    if os.environ.get("GROK_REGISTER_CHROMIUM_DISABLE_DEV_SHM_USAGE") == "1":
        options.set_argument("--disable-dev-shm-usage")
    return options


def create_browser_options(browser_proxy="", extension_path=None):
    options = ChromiumOptions()
    options.auto_port()
    options.set_timeouts(base=1)
    _configure_browser_environment(options)
    apply_browser_proxy_option(options, browser_proxy)
    effective_extension = _resolve_extension_path(extension_path)
    if effective_extension:
        options.add_extension(effective_extension)
    return options


def _build_request_kwargs(**kwargs):
    request_kwargs = dict(kwargs)
    proxies = request_kwargs.pop("proxies", None)
    if proxies is None:
        proxies = get_proxies()
    if proxies:
        request_kwargs["proxies"] = proxies
    curl_options = request_kwargs.pop("curl_options", None) or {}
    curl_options.setdefault(CurlOpt.IPRESOLVE, CURL_IPRESOLVE_V4)
    request_kwargs["curl_options"] = curl_options
    request_kwargs.setdefault("timeout", 15)
    return request_kwargs


def http_get(url, **kwargs):
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.get(url, **request_kwargs)
    except Exception as exc:
        if _is_openssl_fallback_worthy(exc, request_kwargs):
            return _openssl_request("GET", url, request_kwargs)
        if is_proxy_connection_error(exc):
            if managed_proxy_active():
                raise ProxyTransportError(safe_proxy_error_text(exc)) from exc
            direct = dict(request_kwargs)
            direct.pop("proxies", None)
            return requests.get(url, **direct)
        raise


def http_post(url, **kwargs):
    replay_safe = bool(kwargs.pop("replay_safe", False))
    request_kwargs = _build_request_kwargs(**kwargs)
    try:
        return requests.post(url, **request_kwargs)
    except Exception as exc:
        if _is_openssl_fallback_worthy(exc, request_kwargs):
            return _openssl_request("POST", url, request_kwargs)
        if is_proxy_connection_error(exc):
            if managed_proxy_active() or not replay_safe:
                raise ProxyTransportError(safe_proxy_error_text(exc)) from exc
            direct = dict(request_kwargs)
            direct.pop("proxies", None)
            return requests.post(url, **direct)
        raise
