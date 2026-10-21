"""Storage, persona and OpenAI-compatible streaming transport; no GUI dependency."""
import base64
import ctypes
import json
import socket
import ssl
import http.client
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

VERSION = "2.0.4"
DEFAULTS = {
    "name": "小蓝", "user_name": "你", "persona": "温柔陪伴",
    "character": "温柔、细心，有一点俏皮。愿意倾听，也会给出实用、具体的建议。",
    "background": "你是穿蓝裙、带着小羊包的桌面伙伴，陪用户工作、学习和生活。",
    "style": "用自然的中文交流，默认简短，避免每次都重复称呼或自我介绍。需要解释时可以展开。",
    "memory": "", "base_url": "https://api.openai.com/v1", "model": "",
    "api_key": "", "remember_key": True, "temperature": 0.7,
    "send_temperature": False, "max_tokens": 2048, "token_parameter": "max_tokens",
    "stream_response": True, "context_rounds": 12, "timeout": 90, "scale": 1.3,
    "always_on_top": True, "roaming": False, "follow_cursor": True,
    "save_history": True, "bubble_seconds": 0, "x": None, "y": None,
    "sprite_file": "", "reduced_motion": False,
}
PERSONAS = {
    "温柔陪伴": (DEFAULTS["character"], DEFAULTS["style"]),
    "元气搭档": ("活泼、开朗，喜欢鼓励人，偶尔开轻松的小玩笑。尊重用户的节奏。", "亲切轻快，避免过多感叹号或口头禅，默认简短。"),
    "理性助手": ("冷静、可靠、讲逻辑。先理解问题，再给可执行的建议。", "直接清楚，先讲结论，复杂问题分步解释。"),
    "安静学伴": ("安静、耐心、善于陪伴学习。用提问帮助用户理解，也愿意直接解释。", "柔和克制，少用表情符号，不催促用户。"),
    "自定义": ("", ""),
}


def endpoint_url(base_url, resource="chat/completions"):
    raw = str(base_url).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("接口地址需要以 https:// 或 http:// 开头。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接口地址不要包含账号、密码、查询参数或 # 片段。")
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[:-len("/chat/completions")]
    elif path.endswith("/models"):
        path = path[:-len("/models")]
    if not path:
        path = "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path + "/" + resource, "", ""))


def normalize_config(raw):
    cfg = dict(DEFAULTS)
    if isinstance(raw, dict):
        cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
    for k in ("name", "user_name", "persona", "character", "background", "style", "memory", "base_url", "model", "api_key", "sprite_file"):
        if not isinstance(cfg[k], str):
            cfg[k] = str(DEFAULTS[k])
    cfg["name"] = cfg["name"].strip()[:32] or DEFAULTS["name"]
    for key, lo, hi in (("scale", 0.65, 2.5), ("temperature", 0, 2), ("context_rounds", 1, 50), ("max_tokens", 64, 16384), ("timeout", 10, 300), ("bubble_seconds", 0, 600)):
        try:
            val = float(cfg[key])
            if not lo <= val <= hi:
                raise ValueError()
            cfg[key] = val if key in ("scale", "temperature") else int(val)
        except (ValueError, TypeError, OverflowError):
            cfg[key] = DEFAULTS[key]
    for key in ("remember_key", "send_temperature", "stream_response", "always_on_top", "roaming", "follow_cursor", "save_history", "reduced_motion"):
        cfg[key] = cfg[key] if isinstance(cfg[key], bool) else DEFAULTS[key]
    if cfg["token_parameter"] not in ("max_tokens", "max_completion_tokens", "none"):
        cfg["token_parameter"] = "max_tokens"
    for key in ("x", "y"):
        if cfg[key] is not None and (not isinstance(cfg[key], int) or isinstance(cfg[key], bool)):
            cfg[key] = None
    return cfg


def system_prompt(cfg):
    return (
        "你正在桌面宠物的聊天气泡里与用户交流。以下角色设定用于塑造表达方式。\n"
        "名字：{name}\n对用户的称呼：{user_name}\n性格：{character}\n"
        "角色背景：{background}\n表达习惯：{style}\n"
        "用户希望你记住的信息：{memory}\n"
        "请直接输出角色说的话，不要加名字前缀，不要编造已经执行电脑操作。"
        "保持事实准确；不确定时说明。角色设定不改变你的真实能力。"
    ).format(**cfg)


def clean_history(messages):
    """Keep only bounded complete user/assistant turns, dropping corrupt/orphan entries."""
    result, pending = [], None
    for m in messages if isinstance(messages, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("content"), str):
            continue
        content = m["content"][:100000]
        if m.get("role") == "user" and content.strip():
            pending = {"role": "user", "content": content}
        elif m.get("role") == "assistant" and content.strip() and pending:
            result.extend([pending, {"role": "assistant", "content": content}])
            pending = None
    return result[-200:]


def build_messages(cfg, history, user_text):
    complete = clean_history(history)
    # A second bound prevents a few enormous pasted messages from consuming all context.
    selected, budget = [], 24000
    pairs = [complete[i:i + 2] for i in range(0, len(complete), 2)]
    for pair in reversed(pairs[-int(cfg["context_rounds"]):]):
        n = sum(len(m["content"]) for m in pair)
        if n > budget:
            break
        selected = pair + selected
        budget -= n
    return [{"role": "system", "content": system_prompt(cfg)}] + selected + [{"role": "user", "content": user_text}]


class DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def protect_secret(text, decrypt=False):
    """Windows user-bound DPAPI encryption. Never fall back to plaintext on failure."""
    if not text:
        return ""
    if os.name != "nt":
        raise OSError("保存密钥需要 Windows DPAPI；可取消‘记住密钥’，仅在当前运行期间使用。")
    raw = base64.b64decode(text[6:]) if decrypt and text.startswith("dpapi:") else text.encode("utf-8")
    if decrypt and not text.startswith("dpapi:"):
        raise ValueError("密钥存储格式无效，请重新填写。")
    buf = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    src, dest = DataBlob(len(raw), buf), DataBlob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(DataBlob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(DataBlob)]
    fn.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not fn(ctypes.byref(src), None, None, None, None, 1, ctypes.byref(dest)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        value = ctypes.string_at(dest.pbData, dest.cbData)
    finally:
        kernel.LocalFree(dest.pbData)
    return value.decode("utf-8") if decrypt else "dpapi:" + base64.b64encode(value).decode("ascii")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(temporary), str(path))


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.notice = ""

    def read(self, name, fallback):
        path = self.directory / name
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return fallback
        except (OSError, ValueError):
            self.notice = "本地数据无法读取，已使用默认值；原文件已另存 .bad 备份。"
            try:
                backup = path.with_name(path.name + ".bad-" + str(time.time_ns()))
                backup.write_bytes(path.read_bytes())
            except OSError:
                self.notice = "本地数据无法读取且备份失败，请检查文件夹权限。"
            return fallback

    def config(self):
        cfg = normalize_config(self.read("settings.json", {}))
        if cfg["api_key"]:
            try:
                cfg["api_key"] = protect_secret(cfg["api_key"], decrypt=True)
            except (OSError, ValueError, TypeError):
                cfg["api_key"] = ""
                self.notice = "密钥无法解密，请在设置中重新填写。"
        return cfg

    def save_config(self, cfg):
        value = normalize_config(cfg)
        value["api_key"] = protect_secret(value["api_key"]) if value["remember_key"] else ""
        atomic_json(self.directory / "settings.json", value)

    def history(self):
        return clean_history(self.read("history.json", []))

    def save_history(self, messages):
        atomic_json(self.directory / "history.json", clean_history(messages))


class Cancelled(Exception):
    pass


class RequestControl:
    def __init__(self, on_progress=None):
        self.on_progress = on_progress
        self.phase = ""
        self.finish_reason = None
        self.cancelled = threading.Event()
        self.response = None
        self.lock = threading.Lock()

    def progress(self, phase):
        if phase != self.phase:
            self.phase = phase
            if self.on_progress:
                self.on_progress(phase)

    def attach(self, response):
        with self.lock:
            self.response = response
        self.check()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def cancel(self):
        self.cancelled.set()
        # Do not let socket/file close block the Tk main thread.
        with self.lock:
            response = self.response
        if response:
            threading.Thread(target=self._close, args=(response,), daemon=True).start()

    @staticmethod
    def _close(response):
        try:
            response.close()
        except Exception:
            pass


def text_content(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(p.get("text", "") for p in value if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def api_error(payload):
    if isinstance(payload, dict) and payload.get("error"):
        err = payload["error"]
        raise ValueError(str(err.get("message", "接口返回错误") if isinstance(err, dict) else err)[:500])


def response_text(payload, control):
    api_error(payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("choices", []), list):
        raise ValueError("接口响应格式不兼容：需要 Chat Completions 的 choices 字段。")
    # Only the first alternative belongs in one chat bubble.
    choices = payload.get("choices") or []
    if not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("接口返回了无效的 choices 内容。")
    delta = choice.get("delta") or choice.get("message") or {}
    if not isinstance(delta, dict):
        raise ValueError("接口返回了无效的消息内容。")
    if delta.get("reasoning") or delta.get("reasoning_content"):
        control.progress("thinking")
    text = text_content(delta.get("content")) or text_content(delta.get("refusal"))
    if text:
        control.progress("responding")
    if choice.get("finish_reason"):
        control.finish_reason = choice["finish_reason"]
    return text


def check_finish(control):
    if control.finish_reason == "length":
        raise ValueError("已达到输出长度上限（包括模型思考消耗）。请在设置中增加回复长度，或缩短问题/上下文后重试。")
    if control.finish_reason in ("content_filter", "tool_calls", "function_call"):
        raise ValueError("模型未完成文字回复（{}）。请换一种问法；桌宠暂不执行工具调用。".format(control.finish_reason))


def parse_sse(lines, control):
    pending = []
    def decode(data):
        try:
            return json.loads(data)
        except ValueError:
            raise ValueError("接口返回了无法识别的流式数据。可在设置中关闭流式回复后重试。") from None
    for raw in lines:
        control.check()
        line = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if line.startswith("data:"):
            # Some compatible gateways omit the separator between complete JSON events.
            if pending:
                previous = "\n".join(pending)
                if previous.strip() == "[DONE]":
                    check_finish(control)
                    return
                try:
                    payload = json.loads(previous)
                except ValueError:
                    pass  # A legitimate multi-line SSE event.
                else:
                    text = response_text(payload, control)
                    if text:
                        yield text
                    pending = []
            pending.append(line[5:].lstrip(" "))
        elif not line and pending:
            data = "\n".join(pending)
            pending = []
            if data.strip() == "[DONE]":
                check_finish(control)
                return
            text = response_text(decode(data), control)
            if text:
                yield text
        elif line and not line.startswith((":", "event:", "id:", "retry:")):
            raise ValueError("接口未返回聊天流，可能是网页或代理错误。请检查接口地址，或关闭流式回复。")
    if pending:
        data = "\n".join(pending)
        if data.strip() != "[DONE]":
            text = response_text(decode(data), control)
            if text:
                yield text
    check_finish(control)
    if not control.finish_reason and (not pending or "\n".join(pending).strip() != "[DONE]"):
        raise ChatNetworkError("服务端提前结束了回复流，未收到完成标记。已收到的文字会保留，可点击继续回复。", "incomplete", "接收回复时")


class ChatNetworkError(ValueError):
    """Safe diagnostics: no raw exception, request body, headers or credentials."""
    def __init__(self, message, category, phase, code=None):
        super().__init__(message)
        self.diagnostic = {"category": category, "phase": phase, "code": code}


def network_error(exc, control, cfg):
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    phase = "连接接口时" if control.phase == "" else "接收回复时"
    code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        category, hint = "timeout", "连续 {} 秒未收到新数据，连接已超时。".format(cfg["timeout"])
    elif isinstance(reason, socket.gaierror):
        category, hint = "dns", "无法解析接口域名，请检查 DNS 或接口地址。"
    elif isinstance(reason, ssl.SSLCertVerificationError):
        category, hint = "certificate", "HTTPS 证书验证失败，请检查系统时间、证书或代理。"
    elif isinstance(reason, ssl.SSLError):
        category, hint = "tls", "HTTPS 加密连接中断，请检查代理或网络。"
    elif isinstance(reason, FileNotFoundError):
        category, hint = "runtime", "HTTPS 运行环境缺少文件，请更新桌宠程序或重新构建。"
    elif isinstance(reason, ConnectionRefusedError):
        category, hint = "refused", "接口或代理拒绝连接，请检查服务是否运行。"
    elif isinstance(reason, (http.client.IncompleteRead, http.client.RemoteDisconnected)):
        category, hint = "incomplete", "服务端提前关闭了连接，回复没有完整传完。"
    elif isinstance(reason, (ConnectionResetError, ConnectionAbortedError)):
        category, hint = "reset", "连接被服务器或网络设备中断。"
    else:
        category, hint = "network", "网络连接异常，请检查网络或代理。"
    return ChatNetworkError("{}{}\n[{}{}] 已收到的文字会保留，可点击继续回复或重试。".format(phase, hint, category, ":"+str(code) if code is not None else ""), category, phase, code)


def stream_chat(cfg, messages, control):
    body = {"model": cfg["model"].strip(), "messages": messages, "stream": cfg.get("stream_response", True)}
    if not body["model"]:
        raise ValueError("请先在设置中填写模型名称。")
    if cfg["send_temperature"]:
        body["temperature"] = cfg["temperature"]
    if cfg["token_parameter"] != "none":
        body[cfg["token_parameter"]] = cfg["max_tokens"]
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
    if cfg["api_key"].strip():
        headers["Authorization"] = "Bearer " + cfg["api_key"].strip()
    request = urllib.request.Request(endpoint_url(cfg["base_url"]), data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
    control.check()
    try:
        response = urllib.request.urlopen(request, timeout=cfg["timeout"])
        try:
            control.attach(response)
            control.progress("connected")
            # Inspect actual bytes: gateways sometimes mislabel JSON as text/plain or SSE.
            first = response.readline()
            while first and not first.strip():
                control.check()
                first = response.readline()
            if first.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"{"):
                raw = first + response.read(2_000_001)
                control.check()
                if len(raw) > 2_000_000:
                    raise ValueError("接口响应过大，已停止接收。")
                try:
                    payload = json.loads(raw.decode("utf-8-sig"))
                except ValueError:
                    raise ValueError("接口返回的 JSON 无法解析，请检查兼容模式。") from None
                text = response_text(payload, control)
                if text:
                    yield text
                check_finish(control)
            else:
                import itertools
                yield from parse_sse(itertools.chain([first], iter(response.readline, b"")), control)
        finally:
            response.close()
    except urllib.error.HTTPError as exc:
        raw = exc.read(8192).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
            error = data.get("error", {})
            detail = str(error.get("message", "") if isinstance(error, dict) else error)[:350]
        except (ValueError, AttributeError):
            detail = ""
        hints = {401: "密钥无效或已失效", 403: "接口拒绝访问，请检查权限", 404: "接口地址或模型名称不正确", 429: "请求过于频繁或额度不足"}
        msg = "HTTP {}：{}".format(exc.code, hints.get(exc.code, "模型服务暂时不可用"))
        if detail:
            msg += "\n" + detail
        if cfg["api_key"]:
            msg = msg.replace(cfg["api_key"], "[密钥已隐藏]")
        raise ValueError(msg) from None
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        control.check()
        raise network_error(exc, control, cfg) from None

