"""Animated Windows desktop companion with an attached streaming chat bubble."""
import argparse
import ctypes
import hashlib
import json
import math
import os
import queue
import random
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageSequence

from pet_core import (VERSION, DEFAULTS, PERSONAS, Store, RequestControl, Cancelled,
                      normalize_config, build_messages, endpoint_url, stream_chat, ChatNetworkError, atomic_json)

CHROMA = "#ff00fe"
INK, MUTED, BLUE, LINE, SURFACE = "#25334b", "#74829a", "#4f71db", "#e3e9f3", "#f4f7fc"
FONT = ("Microsoft YaHei UI", 10)
STATES = ["idle", "running-right", "running-left", "waving", "jumping", "failed", "waiting", "running", "review"]
COUNTS = [6, 8, 8, 4, 5, 8, 6, 6, 6]
DURATIONS = [[280, 110, 110, 140, 140, 320], [120]*7+[220], [120]*7+[220],
             [140]*3+[280], [140]*4+[280], [140]*7+[240], [150]*5+[260],
             [120]*5+[220], [150]*5+[280]]


def app_directory():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def resource(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def dpi_awareness():
    if os.name == "nt":
        user = ctypes.windll.user32
        user.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        user.GetAncestor.restype = ctypes.c_void_p
        user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user.SetWindowPos.restype = ctypes.c_bool
        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except (AttributeError, OSError):
                pass


def work_area(x=None, y=None):
    if os.name == "nt":
        from ctypes import wintypes
        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        user = ctypes.windll.user32
        user.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user.MonitorFromPoint.restype = wintypes.HANDLE
        user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        point = wintypes.POINT(int(x or 0), int(y or 0))
        monitor = user.MonitorFromPoint(point, 2)
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if user.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcWork
            return r.left, r.top, r.right, r.bottom
    return 0, 0, 1920, 1040


def place_window(window, x, y, width, height):
    x, y, width, height = map(int, (x, y, width, height))
    if os.name == "nt":
        window.geometry("{}x{}".format(width, height))
        window.update_idletasks()
        handle = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)
        ctypes.windll.user32.SetWindowPos(handle, 0, x, y, width, height, 0x0010 | 0x0004)
    else:
        window.geometry("{}x{}+{}+{}".format(width, height, x, y))


def rounded(canvas, box, radius=18, **kwargs):
    x1, y1, x2, y2 = box
    points = [x1+radius,y1,x2-radius,y1,x2,y1,x2,y1+radius,x2,y2-radius,x2,y2,
              x2-radius,y2,x1+radius,y2,x1,y2,x1,y2-radius,x1,y1+radius,x1,y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **kwargs)


class SpriteAtlas:
    def __init__(self, path, scale, master):
        self.master, self.path = master, Path(path)
        with Image.open(self.path) as opened:
            if opened.size not in ((1536, 2288), (1536, 1872)):
                raise ValueError("精灵图应为 1536×2288（11行）或 1536×1872（9行）。")
            if "A" not in opened.getbands():
                raise ValueError("精灵图需要透明背景。")
            self.source = opened.convert("RGBA")
        self.has_look = self.source.height == 2288
        self.set_scale(scale)

    def set_scale(self, scale):
        self.width, self.height = round(192 * scale), round(208 * scale)
        self.photos = {}
        for row in range(11 if self.has_look else 9):
            count = COUNTS[row] if row < 9 else 8
            for col in range(count):
                rgba = self.source.crop((col*192, row*208, (col+1)*192, (row+1)*208))
                rgba = rgba.resize((self.width, self.height), Image.Resampling.LANCZOS)
                # Tk's Windows transparent-color API is binary. Threshold alpha, never
                # white, so the white sheep bag/eyes remain intact and no magenta fringe remains.
                mask = rgba.getchannel("A").point(lambda a: 255 if a >= 110 else 0)
                image = Image.new("RGB", rgba.size, CHROMA)
                image.paste(rgba.convert("RGB"), (0, 0), mask)
                self.photos[row, col] = ImageTk.PhotoImage(image, master=self.master)


class Bubble:
    WIDTH, HEIGHT = 260, 86
    INPUT_WIDTH, INPUT_HEIGHT = 280, 96

    def __init__(self, app):
        self.app = app
        self.window = self.make_window()
        self.composer = self.make_window()
        self.canvas = tk.Canvas(self.window, width=self.WIDTH, height=self.HEIGHT,
                                bg=CHROMA, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg="white")
        self.body_item = self.canvas.create_window(16, 10, window=self.body, width=self.WIDTH-32,
                                                   height=self.HEIGHT-34, anchor="nw")
        header = tk.Frame(self.body, bg="white")
        header.pack(fill="x")
        self.name = tk.Label(header, text=app.cfg["name"], fg=BLUE, bg="white", anchor="w", font=(FONT[0], 9, "bold"))
        self.name.pack(side="left")
        for label, command in (("×", self.hide), ("复制", self.copy)):
            button = tk.Label(header, text=label, bg="white", fg=MUTED, padx=5, cursor="hand2", font=(FONT[0], 8))
            button.pack(side="right")
            button.bind("<Button-1>", lambda event, fn=command: fn())
        response_frame = tk.Frame(self.body, bg="white")
        response_frame.pack(fill="both", expand=True, pady=(3, 0))
        self.output = tk.Text(response_frame, bg="white", fg=INK, relief="flat", bd=0,
                              wrap="char", font=FONT, padx=0, pady=0, height=1,
                              cursor="arrow", state="disabled", selectbackground="#d8e4ff")
        self.scroll = ttk.Scrollbar(response_frame, command=self.output.yview, style="Vertical.TScrollbar")
        self.output.configure(yscrollcommand=self.scroll.set)
        self.output.pack(side="left", fill="both", expand=True)
        # The input is a separate small strip below the character.
        self.input_canvas = tk.Canvas(self.composer, bg=CHROMA, highlightthickness=0,
                                       width=self.INPUT_WIDTH, height=self.INPUT_HEIGHT)
        self.input_canvas.pack(fill="both", expand=True)
        rounded(self.input_canvas, (1, 1, self.INPUT_WIDTH-2, self.INPUT_HEIGHT-2), radius=16,
                fill="white", outline=LINE)
        panel = tk.Frame(self.input_canvas, bg="white")
        self.input_canvas.create_window(10, 8, window=panel, width=self.INPUT_WIDTH-20,
                                        height=self.INPUT_HEIGHT-18, anchor="nw")
        self.input = tk.Text(panel, bg=SURFACE, fg=INK, relief="flat", bd=0, height=1,
                             font=FONT, wrap="word", padx=8, pady=5, insertbackground=BLUE)
        self.input.pack(fill="both", expand=True)
        self.input.bind("<Return>", self.on_enter)
        self.input.bind("<Shift-Return>", lambda event: None)
        self.input.bind("<Control-Return>", self.on_enter)
        actions = tk.Frame(panel, bg="white")
        actions.pack(fill="x", pady=(3, 0))
        self.status = tk.Label(actions, text="Enter 发送", fg=MUTED, bg="white", font=(FONT[0], 8), anchor="w")
        self.send_button = ttk.Button(actions, text="发送", width=5, command=app.send)
        self.send_button.pack(side="right")
        self.stop_button = ttk.Button(actions, text="停止", width=5, command=app.stop)
        self.retry_button = ttk.Button(actions, text="重试", width=7, command=app.retry)
        self.status.pack(side="left", fill="x", expand=True)
        # Kept as a non-visible view model for the current question.
        self.user_line = tk.Label(panel)
        self._font = tkfont.Font(font=FONT)
        self._text = ""
        self.visible = False
        self.last_activity = time.monotonic()
        self._resize_job = None
        for window in (self.window, self.composer):
            window.bind("<Escape>", lambda e: self.hide())
        self.paint(True)

    def make_window(self):
        window = tk.Toplevel(self.app.root)
        window.withdraw()
        window.overrideredirect(True)
        window.configure(bg=CHROMA)
        window.attributes("-topmost", self.app.cfg["always_on_top"])
        if os.name == "nt":
            window.attributes("-transparentcolor", CHROMA)
        return window

    def paint(self, on_right):
        self.canvas.delete("shape")
        bottom = self.HEIGHT-17
        rounded(self.canvas, (2, 2, self.WIDTH-3, bottom), radius=17,
                fill="white", outline=LINE, tags="shape")
        tail = self.WIDTH-44 if on_right else 44
        self.canvas.create_polygon(tail-9, bottom-1, tail+9, bottom-1, tail, self.HEIGHT-2,
                                   fill="white", outline="white", tags="shape")
        self.canvas.tag_lower("shape")

    def fit_content(self):
        self._resize_job = None
        if not self.window.winfo_exists():
            return
        # Bound the reply to seven visible lines; longer replies remain scrollable.
        counts = self.output.count("1.0", "end-1c", "displaylines")
        lines = (counts[0] if counts else 0)+1
        visible_lines = min(7, max(1, lines))
        height = 55 + visible_lines*self._font.metrics("linespace")
        self.HEIGHT = height
        self.canvas.configure(height=height)
        self.canvas.itemconfigure(self.body_item, height=height-34)
        if lines > 7:
            self.scroll.pack(side="right", fill="y", before=self.output)
        else:
            self.scroll.pack_forget()
        if self.visible:
            self.reposition()

    def reposition(self):
        x, y, w, h = self.app.x, self.app.y, self.app.atlas.width, self.app.atlas.height
        left, top, right, bottom = work_area(x+w//2, y+h//2)
        bx = x + w//2 - self.WIDTH + 44
        on_right = True
        if bx < left+8:
            bx = x+w//2-44
            on_right = False
        bx = max(left+8, min(bx, right-self.WIDTH-8))
        by = y + int(h*.18) - self.HEIGHT
        if by < top+8:
            by = top+8
            # If above the head is unavailable, put the bubble beside the character.
            bx = x+w-12 if x+w+self.WIDTH < right-8 else x-self.WIDTH+12
            bx = max(left+8, min(bx, right-self.WIDTH-8))
        by = max(top+8, min(by, bottom-self.HEIGHT-8))
        place_window(self.window, bx, by, self.WIDTH, self.HEIGHT)
        ix = max(left+8, min(x+w//2-self.INPUT_WIDTH//2, right-self.INPUT_WIDTH-8))
        iy = max(top+8, min(y+h+4, bottom-self.INPUT_HEIGHT-8))
        place_window(self.composer, ix, iy, self.INPUT_WIDTH, self.INPUT_HEIGHT)
        self.paint(on_right)

    def show(self, focus=False):
        self.visible = True
        self.last_activity = time.monotonic()
        self.name.configure(text=self.app.cfg["name"])
        self.window.deiconify()
        self.composer.deiconify()
        # Reserve space for the input when the pet was parked against the taskbar.
        self.app.move(self.app.x, self.app.y)
        self.fit_content()
        self.window.lift()
        self.composer.lift()
        if focus:
            self.input.focus_force()

    def hide(self):
        self.visible = False
        self.window.withdraw()
        self.composer.withdraw()

    def copy(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self._text)
        self.status.configure(text="已复制气泡文字")

    def set_text(self, text):
        if text == self._text:
            return
        stick_to_end = self.output.yview()[1] >= .98
        self.output.configure(state="normal")
        if text.startswith(self._text):
            self.output.insert("end", text[len(self._text):])
        else:
            self.output.delete("1.0", "end")
            self.output.insert("1.0", text)
            stick_to_end = True
        self.output.configure(state="disabled")
        if stick_to_end:
            self.output.see("end")
        self._text = text
        if self._resize_job is None:
            self._resize_job = self.app.root.after_idle(self.fit_content)
        self.last_activity = time.monotonic()

    def busy(self, active):
        self.send_button.configure(state="disabled" if active else "normal")
        if active:
            self.retry_button.pack_forget()
            self.stop_button.pack(side="right", padx=(0, 4))
        else:
            self.stop_button.pack_forget()

    def on_enter(self, event):
        if event.state & 0x1:  # Shift remains normal Text insertion.
            return None
        self.app.send()
        return "break"


class DesktopPet:
    def __init__(self, data_dir=None):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("蓝裙小羊 · 动态桌宠")
        try:
            self.root.iconbitmap(str(resource("assets/pet.ico")))
        except tk.TclError:
            pass
        self.root.overrideredirect(True)
        self.root.configure(bg=CHROMA)
        self.root.report_callback_exception = self.callback_error
        self.store = Store(data_dir or app_directory()/"data")
        self.cfg = self.store.config()
        self.history = self.store.history() if self.cfg["save_history"] else []
        self.errors, self.events = [], queue.Queue()
        self.closed = False
        self.interacted = False
        self.active_id, self.control = None, None
        self.pending_user, self.buffer, self.last_failed = "", "", ""
        self.workers = []
        self.settings_window = self.history_window = None
        self.context_menu = None
        self.menu_variables = {}
        self.dragging, self.drag_moved = False, False
        self.temp_state, self.temp_until = "idle", 0
        self.state, self.frame, self.next_frame = "idle", 0, 0
        self.roam_until, self.roam_next, self.direction = 0, time.monotonic()+25, 1
        self.last_tick = time.monotonic()
        self.last_pointer, self.pointer_until, self.look_index = None, 0, None
        self.root.option_add("*Font", FONT)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", padding=(8, 5), font=FONT)
        style.configure("TButton", background=BLUE, foreground="white", borderwidth=0, focuscolor=BLUE)
        style.map("TButton", background=[("active", "#3e5fc3"), ("disabled", "#d6deee")], foreground=[("disabled", MUTED)])
        style.configure("TNotebook", background=SURFACE, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(15, 9), font=FONT)
        style.configure("TNotebook.Tab", background="#e8eef8", foreground=MUTED)
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("TEntry", fieldbackground="white", foreground=INK, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE, padding=4)
        style.configure("TCombobox", fieldbackground="white", background="white", foreground=INK, bordercolor=LINE, arrowsize=14)
        style.configure("TFrame", background=SURFACE)
        style.configure("TLabel", background=SURFACE, foreground=INK)
        style.configure("TCheckbutton", background=SURFACE, foreground=INK)
        sprite = Path(self.cfg["sprite_file"]) if self.cfg["sprite_file"] else resource("assets/spritesheet.png")
        try:
            self.atlas = SpriteAtlas(sprite, self.cfg["scale"], self.root)
        except (OSError, ValueError):
            self.cfg["sprite_file"] = ""
            self.atlas = SpriteAtlas(resource("assets/spritesheet.png"), self.cfg["scale"], self.root)
            self.store.notice = "自定义精灵图无法读取，已恢复默认角色。"
        self.root.attributes("-topmost", self.cfg["always_on_top"])
        if os.name == "nt":
            self.root.attributes("-transparentcolor", CHROMA)
        self.label = tk.Label(self.root, image=self.atlas.photos[0,0], bg=CHROMA, bd=0, highlightthickness=0)
        self.label.pack()
        self.label.bind("<Button-1>", self.press)
        self.label.bind("<B1-Motion>", self.drag)
        self.label.bind("<ButtonRelease-1>", self.release)
        self.label.bind("<Double-Button-1>", lambda e: self.bubble.show(True))
        self.label.bind("<Button-3>", self.menu)
        left, top, right, bottom = work_area()
        self.x = self.cfg["x"] if self.cfg["x"] is not None else right-self.atlas.width-65
        self.y = self.cfg["y"] if self.cfg["y"] is not None else bottom-self.atlas.height-14
        self.bubble = Bubble(self)
        self.move(self.x, self.y)
        self.root.deiconify()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tick_job = self.root.after(30, self.tick)
        self.poll_job = self.root.after(50, self.poll)
        self.welcome_job = self.root.after(450, self.welcome)

    def welcome(self):
        if self.closed or self.interacted:
            return
        text = "你好，我是{}。双击我就能聊天，右键可以切换动作和打开设置。".format(self.cfg["name"])
        if not self.cfg["model"]:
            text += "\n先右键打开设置，填写接口、模型和密钥，再设置我的性格吧。"
        if self.store.notice:
            text += "\n" + self.store.notice
        self.bubble.set_text(text)
        self.bubble.user_line.configure(text="蓝裙与小羊包，今天也陪着你")
        self.bubble.show()
        self.play("waving")

    def callback_error(self, exc_type, exc, tb):
        error = "".join(traceback.format_exception(exc_type, exc, tb))
        if hasattr(self, "errors"):
            self.errors.append(error)
        # No request headers, credentials or prompt contents are written to logs.
        try:
            with (self.store.directory/"error.log").open("a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + error + "\n")
        except OSError:
            pass
        if hasattr(self, "bubble"):
            self.bubble.status.configure(text="遇到界面错误，请查看 data/error.log")

    def move(self, x, y):
        w, h = self.atlas.width, self.atlas.height
        left, top, right, bottom = work_area(x+w//2, y+h//2)
        input_space = self.bubble.INPUT_HEIGHT+12 if hasattr(self, "bubble") and self.bubble.visible else 0
        self.x, self.y = max(left, min(int(x), right-w)), max(top, min(int(y), bottom-h-input_space))
        place_window(self.root, self.x, self.y, w, h)
        if hasattr(self, "bubble") and self.bubble.visible:
            self.bubble.reposition()

    def press(self, event):
        self.dragging, self.drag_moved = True, False
        self.drag_origin = (event.x_root, event.y_root, self.x, self.y)
        self.drag_direction = 1
        self.roam_until = 0

    def drag(self, event):
        px, py, wx, wy = self.drag_origin
        dx, dy = event.x_root-px, event.y_root-py
        if abs(dx)+abs(dy) > 5:
            self.drag_moved = True
            self.drag_direction = 1 if dx >= 0 else -1
            self.move(wx+dx, wy+dy)

    def release(self, event):
        self.dragging = False
        if self.drag_moved:
            self.persist()

    def persist(self):
        self.cfg["x"], self.cfg["y"] = self.x, self.y
        try:
            self.store.save_config(self.cfg)
        except (OSError, ValueError) as exc:
            self.bubble.status.configure(text="设置暂未保存：" + str(exc)[:70])

    def play(self, state):
        if state not in STATES:
            return
        self.temp_state = state
        self.temp_until = time.monotonic() + sum(DURATIONS[STATES.index(state)])/1000
        self.frame, self.next_frame = 0, 0

    def tick(self):
        if self.closed:
            return
        now = time.monotonic()
        dt = min(now-self.last_tick, .1)
        self.last_tick = now
        if self.dragging and self.drag_moved:
            state = "running-right" if self.drag_direction > 0 else "running-left"
        elif now < self.temp_until:
            state = self.temp_state
        elif self.active_id:
            state = "review" if self.buffer else "running"
        elif self.cfg["roaming"] and not self.cfg["reduced_motion"] and not self.dragging and not self.roaming_paused():
            if now >= self.roam_next:
                self.roam_until, self.roam_next = now+random.uniform(3, 6), now+random.uniform(25, 45)
                self.direction = random.choice((-1, 1))
            if now < self.roam_until:
                old = self.x
                self.move(self.x+self.direction*round(55*dt), self.y)
                if old == self.x:
                    self.direction *= -1
                state = "running-right" if self.direction > 0 else "running-left"
            else:
                state = "idle"
        else:
            state = "idle"
        look = None
        if state == "idle" and self.cfg["follow_cursor"] and self.atlas.has_look and not self.cfg["reduced_motion"]:
            pointer = self.root.winfo_pointerxy()
            if self.last_pointer and (abs(pointer[0]-self.last_pointer[0])+abs(pointer[1]-self.last_pointer[1])) > 3:
                self.pointer_until = now+1.1
            self.last_pointer = pointer
            dx = pointer[0]-(self.x+self.atlas.width*.48)
            dy = pointer[1]-(self.y+self.atlas.height*.26)
            if now < self.pointer_until and 35 < math.hypot(dx, dy) < 580:
                look = round((math.degrees(math.atan2(dx, -dy))+360)%360/22.5)%16
        if state != self.state:
            self.state, self.frame, self.next_frame = state, 0, now
        row = STATES.index(state)
        if self.cfg["reduced_motion"]:
            col = 0
        elif look is not None:
            row, col = 9+look//8, look%8
        else:
            if now >= self.next_frame:
                self.next_frame = now+DURATIONS[row][self.frame]/1000
                col = self.frame
                self.frame = (self.frame+1)%COUNTS[row]
                self.rendered_col = col
            else:
                col = getattr(self, "rendered_col", 0)
        photo = self.atlas.photos[row, col]
        if photo != getattr(self, "current_photo", None):
            self.label.configure(image=photo)
            self.current_photo = photo
        self.look_index = look
        hide_after = self.cfg["bubble_seconds"]
        if hide_after and self.bubble.visible and not self.active_id and not self.bubble.input.get("1.0", "end-1c").strip():
            focused = self.root.focus_displayof()
            if focused is None and now-self.bubble.last_activity > hide_after:
                self.bubble.hide()
        self.tick_job = self.root.after(30, self.tick)

    def roaming_paused(self):
        if self.bubble.input.get("1.0", "end-1c").strip():
            return True
        for window in (self.context_menu, self.settings_window, self.history_window):
            if window is not None and window.winfo_exists() and window.winfo_ismapped():
                return True
        return False

    def send(self, text=None, continuation=False):
        if self.active_id:
            return
        from_input = text is None
        text = text if text is not None else self.bubble.input.get("1.0", "end-1c").strip()
        if not text:
            return
        if len(text) > 12000:
            self.bubble.status.configure(text="单条消息请控制在 12000 字以内。")
            return
        try:
            endpoint_url(self.cfg["base_url"])
            if not self.cfg["model"].strip():
                raise ValueError("先在设置中填写模型名称。")
        except ValueError as exc:
            self.bubble.status.configure(text=str(exc))
            self.settings()
            return
        self.workers = [t for t in self.workers if t.is_alive()]
        if len(self.workers) >= 4:
            self.bubble.status.configure(text="之前的连接仍在结束，请稍等片刻再试。")
            return
        prefix = self.buffer if continuation else ""
        self.pending_user, self.buffer, self.last_failed = text, prefix, ""
        self.interacted = True
        self.temp_until = 0
        if from_input:
            self.bubble.input.delete("1.0", "end")
        self.bubble.user_line.configure(text="你：" + (text[:115]+"…" if len(text)>115 else text))
        self.bubble.set_text(prefix + "\n\n正在接着说…" if prefix else "正在想一想…")
        self.bubble.status.configure(text="正在连接 · " + self.cfg["model"])
        self.bubble.show()
        self.bubble.busy(True)
        request_id = uuid.uuid4().hex
        control = RequestControl(lambda phase: self.events.put((request_id, "phase", phase)))
        self.request_started = time.monotonic()
        self.request_model = self.cfg["model"]
        self.request_phase = "connecting"
        self.control, self.active_id = control, request_id
        cfg = dict(self.cfg)
        messages = build_messages(cfg, self.history, text)
        if continuation:
            messages.extend([{ "role":"assistant", "content":prefix}, {"role":"user", "content":"上条回复因连接中断没有说完。请直接从断点继续，不要重复已有内容。"}])

        def work():
            total = 0
            try:
                for delta in stream_chat(cfg, messages, control):
                    control.check()
                    total += len(delta)
                    if total > 100000:
                        raise ValueError("本次回复过长，已停止接收。可以请我分段回答。")
                    self.events.put((request_id, "delta", delta))
                control.check()
                if not total:
                    raise ValueError("模型没有返回文字。请检查模型名称、兼容模式或增加输出长度。")
                self.events.put((request_id, "done", ""))
            except Cancelled:
                self.events.put((request_id, "cancelled", ""))
            except Exception as exc:
                if isinstance(exc, ChatNetworkError):
                    try:
                        atomic_json(self.store.directory / "connection-error.json", dict(exc.diagnostic, version=VERSION, time=time.strftime("%Y-%m-%d %H:%M:%S")))
                    except OSError:
                        pass
                detail = str(exc)
                if cfg["api_key"]:
                    detail = detail.replace(cfg["api_key"], "[密钥已隐藏]")
                self.events.put((request_id, "error", detail[:600]))
        thread = threading.Thread(target=work, daemon=True, name="pet-chat")
        self.workers.append(thread)
        thread.start()

    def poll(self):
        if self.closed:
            return
        changed = False
        for _ in range(500):
            try:
                request_id, kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if request_id == "settings-test":
                win, result, button, status = value
                if win.winfo_exists():
                    result.set(status)
                    button.configure(state="normal")
                continue
            if request_id != self.active_id:
                continue
            if kind == "phase":
                self.request_phase = value
            elif kind == "delta":
                self.buffer += value
                changed = True
                self.bubble.status.configure(text="正在回复 · " + self.cfg["model"])
            elif kind == "done":
                self.commit()
                self.active_id, self.control = None, None
                self.bubble.busy(False)
                self.bubble.status.configure(text="回复完成 · 可继续聊聊")
                self.bubble.set_text(self.buffer)
                self.play("waving")
            elif kind == "error":
                self.active_id, self.control = None, None
                self.last_failed = self.pending_user
                self.bubble.busy(False)
                self.bubble.set_text((self.buffer+"\n\n" if self.buffer else "") + "这次没有顺利说完。\n" + value)
                self.bubble.status.configure(text="回复未完成 · 可以接着说" if self.buffer else "回复未完成 · 可以重试")
                self.bubble.retry_button.configure(text="继续回复" if self.buffer else "重试")
                self.bubble.retry_button.pack(side="right", padx=(0, 4))
                self.play("failed")
                changed = False
            elif kind == "cancelled":
                self.stop()
        if changed and self.active_id:
            self.bubble.set_text(self.buffer)
        if self.active_id:
            elapsed = int(time.monotonic() - self.request_started)
            phase = {"connecting":"正在连接", "connected":"已连接，等待模型", "thinking":"模型正在思考", "responding":"正在回复"}.get(self.request_phase, "正在等待")
            self.bubble.status.configure(text="{} · {}秒 · {}".format(phase, elapsed, self.request_model))
        self.poll_job = self.root.after(60, self.poll)

    def commit(self):
        if self.pending_user and self.buffer.strip():
            self.history.extend([{"role":"user", "content":self.pending_user}, {"role":"assistant", "content":self.buffer}])
            self.history = self.history[-200:]
            self.pending_user = ""
            if self.cfg["save_history"]:
                try:
                    self.store.save_history(self.history)
                except OSError:
                    self.bubble.status.configure(text="回复已收到，但本地记录保存失败。")

    def stop(self):
        if not self.active_id:
            return
        if self.control:
            self.control.cancel()
        self.active_id, self.control = None, None
        if self.buffer.strip():
            self.commit()
            self.bubble.set_text(self.buffer)
        else:
            self.last_failed = self.pending_user
            if not self.bubble.input.get("1.0", "end-1c").strip():
                self.bubble.input.insert("1.0", self.pending_user)
            self.bubble.set_text("已停止。这条消息没有加入上下文，你可以修改后重新发送。")
        self.bubble.busy(False)
        self.bubble.status.configure(text="已停止回复")
        self.play("waiting")

    def retry(self):
        if self.last_failed:
            self.send(self.last_failed, continuation=bool(self.buffer.strip()))

    def menu(self, event):
        if self.context_menu is None:
            self.context_menu = self.build_context_menu()
        m = self.context_menu
        m.entryconfigure(0, label="和{}聊天".format(self.cfg["name"]))
        for key, var in self.menu_variables.items():
            var.set(self.cfg[key])
        animations = not self.cfg["reduced_motion"]
        for index in (4, 5):  # Interaction submenu and automatic roaming.
            m.entryconfigure(index, state="normal" if animations else "disabled")
        m.entryconfigure(6, state="normal" if animations and self.atlas.has_look else "disabled")
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            # tk_popup returns after posting, not after selection. Retain the widget
            # and BooleanVars until application shutdown so clicks can reach Tcl.
            if not self.closed:
                m.grab_release()

    def build_context_menu(self):
        m = tk.Menu(self.root, tearoff=False, font=FONT)
        m.add_command(label="和{}聊天".format(self.cfg["name"]), command=lambda: self.bubble.show(True))
        m.add_command(label="性格与模型设置…", command=self.settings)
        m.add_command(label="对话记录…", command=self.show_history)
        m.add_separator()
        actions = tk.Menu(m, tearoff=False)
        for title, state in [("挥挥手", "waving"), ("跳一下", "jumping"), ("想一想", "running"), ("等你回应", "waiting"), ("检查一下", "review")]:
            actions.add_command(label=title, command=lambda s=state: self.play(s))
        m.add_cascade(label="互动动作", menu=actions)
        for title, key in [("自动散步", "roaming"), ("视线跟随鼠标", "follow_cursor"), ("窗口置顶", "always_on_top")]:
            var = tk.BooleanVar(value=self.cfg[key])
            self.menu_variables[key] = var
            m.add_checkbutton(label=title, variable=var, command=lambda k=key: self.toggle(k))
        m.add_command(label="隐藏聊天气泡", command=self.bubble.hide)
        m.add_command(label="移回主屏幕", command=self.reset_position)
        m.add_separator()
        m.add_command(label="退出桌宠", command=self.quit)
        return m

    def apply_topmost(self):
        for window in (self.root, self.bubble.window, self.bubble.composer, self.settings_window, self.history_window):
            if window is not None and window.winfo_exists():
                window.attributes("-topmost", self.cfg["always_on_top"])

    def toggle(self, key):
        self.cfg[key] = not self.cfg[key]
        if key == "always_on_top":
            self.apply_topmost()
        elif key == "roaming":
            now = time.monotonic()
            self.temp_until = 0
            self.roam_until = now + 4 if self.cfg[key] else 0
            self.roam_next = now + random.uniform(25, 45)
        elif key == "follow_cursor":
            self.pointer_until = 0
        for setting, var in self.menu_variables.items():
            var.set(self.cfg[setting])
        self.persist()

    def reset_position(self):
        l,t,r,b = work_area()
        self.move(r-self.atlas.width-65, b-self.atlas.height-14)
        self.persist()

    def show_history(self):
        if self.history_window and self.history_window.winfo_exists():
            self.history_window.lift()
            return
        win = self.history_window = tk.Toplevel(self.root)
        win.title("{} · 对话记录".format(self.cfg["name"]))
        win.geometry("620x620")
        win.minsize(440, 400)
        win.configure(bg=SURFACE)
        win.attributes("-topmost", self.cfg["always_on_top"])
        text = ScrolledText(win, wrap="word", bg="white", fg=INK, font=FONT, relief="flat", padx=18, pady=14)
        text.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        def transcript():
            return "\n\n".join(("你" if m["role"]=="user" else self.cfg["name"])+"：\n"+m["content"] for m in self.history)
        def refresh():
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", transcript() or "还没有完整对话。")
            text.configure(state="disabled")
            text.see("end")
        def export():
            file = filedialog.asksaveasfilename(parent=win, title="导出对话", defaultextension=".txt", filetypes=[("文本文件", "*.txt")])
            if file:
                try:
                    Path(file).write_text(transcript(), encoding="utf-8")
                except OSError as exc:
                    messagebox.showerror("导出失败", str(exc), parent=win)
        def clear():
            if messagebox.askyesno("清空对话", "清空本地记录和本轮上下文？性格与长期记忆仍保留。", parent=win):
                self.stop()
                self.history, self.pending_user, self.buffer, self.last_failed = [], "", "", ""
                self.store.save_history([])
                self.bubble.set_text("我们重新开始聊吧。")
                refresh()
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=16, pady=(0, 16))
        ttk.Button(bar, text="刷新记录", command=refresh).pack(side="left")
        ttk.Button(bar, text="导出文本…", command=export).pack(side="left", padx=8)
        ttk.Button(bar, text="清空对话", command=clear).pack(side="right")
        refresh()

    def settings(self):
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.lift()
            return
        win = self.settings_window = tk.Toplevel(self.root)
        win.title("桌宠设置 · 性格 / 模型 / 动画")
        win.geometry("670x700")
        win.minsize(620, 650)
        win.configure(bg=SURFACE)
        win.attributes("-topmost", self.cfg["always_on_top"])
        tabs = ttk.Notebook(win)
        tabs.pack(fill="both", expand=True, padx=18, pady=(18, 10))
        character, model, appearance = [ttk.Frame(tabs, padding=18) for _ in range(3)]
        for page, title in zip((character, model, appearance), ("人物设定", "模型接口", "外观与习惯")):
            tabs.add(page, text=title)
            page.columnconfigure(1, weight=1)
        variables, texts = {}, {}
        def entry(page, row, title, key, width=38, secret=False):
            ttk.Label(page, text=title).grid(row=row, column=0, sticky="nw", pady=7, padx=(0, 14))
            var = tk.StringVar(value=str(self.cfg[key]))
            variables[key] = var
            widget = ttk.Entry(page, textvariable=var, width=width, show="•" if secret else "")
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            return widget
        def multiline(page, row, title, key, height):
            ttk.Label(page, text=title).grid(row=row, column=0, sticky="nw", pady=7, padx=(0, 14))
            widget = tk.Text(page, height=height, font=FONT, wrap="word", relief="flat", bg="white", fg=INK, padx=8, pady=6)
            widget.insert("1.0", self.cfg[key])
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            texts[key] = widget
        def check(page, row, title, key):
            variables[key] = tk.BooleanVar(value=self.cfg[key])
            ttk.Checkbutton(page, text=title, variable=variables[key]).grid(row=row, column=1, sticky="w", pady=7)
        entry(character, 0, "桌宠名字", "name")
        entry(character, 1, "如何称呼你", "user_name")
        ttk.Label(character, text="性格预设").grid(row=2, column=0, sticky="w")
        variables["persona"] = tk.StringVar(value=self.cfg["persona"])
        preset = ttk.Combobox(character, textvariable=variables["persona"], values=list(PERSONAS), state="readonly")
        preset.grid(row=2, column=1, sticky="ew", pady=7)
        multiline(character, 3, "性格细节", "character", 3)
        multiline(character, 4, "角色背景", "background", 3)
        multiline(character, 5, "说话方式", "style", 3)
        multiline(character, 6, "长期记忆", "memory", 3)
        ttk.Label(character, text="这些设定会随每次对话发送给模型；修改后从下一条消息生效。", foreground=MUTED, wraplength=480).grid(row=7, column=0, columnspan=2, sticky="w", pady=(12,0))
        def apply_persona(event):
            value = PERSONAS.get(variables["persona"].get())
            if value and variables["persona"].get() != "自定义":
                for key, content in zip(("character", "style"), value):
                    texts[key].delete("1.0", "end")
                    texts[key].insert("1.0", content)
        preset.bind("<<ComboboxSelected>>", apply_persona)
        entry(model, 0, "接口基础地址", "base_url")
        ttk.Label(model, text="填写 /v1 基础地址或完整 /chat/completions 地址。", foreground=MUTED).grid(row=1, column=1, sticky="w")
        entry(model, 2, "模型名称", "model")
        key_entry = entry(model, 3, "API Key", "api_key", secret=True)
        show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(model, text="显示密钥", variable=show_key, command=lambda: key_entry.configure(show="" if show_key.get() else "•")).grid(row=4, column=0, sticky="w")
        check(model, 4, "记住密钥（Windows 用户加密）", "remember_key")
        entry(model, 5, "记忆最近几轮", "context_rounds")
        entry(model, 6, "回复长度（含思考）", "max_tokens")
        ttk.Label(model, text="长度参数").grid(row=7, column=0, sticky="w")
        variables["token_parameter"] = tk.StringVar(value=self.cfg["token_parameter"])
        ttk.Combobox(model, textvariable=variables["token_parameter"], state="readonly", values=["max_tokens", "max_completion_tokens", "none"]).grid(row=7, column=1, sticky="ew", pady=7)
        entry(model, 8, "超时（秒）", "timeout")
        check(model, 12, "流式回复（关闭可兼容部分代理）", "stream_response")
        check(model, 9, "发送 temperature 参数", "send_temperature")
        entry(model, 10, "temperature", "temperature")
        ttk.Label(model, text="兼容本地 / 云端 Chat Completions 接口。推理模型若无正文，可将回复长度调至 8192。部分代理可关闭流式回复。测试会发送一次简短消息。", foreground=MUTED, wraplength=490).grid(row=11, column=0, columnspan=2, sticky="w", pady=10)
        entry(appearance, 0, "角色大小（0.65–2.5）", "scale")
        entry(appearance, 1, "气泡隐藏秒数（0=保留）", "bubble_seconds")
        check(appearance, 2, "窗口始终置顶", "always_on_top")
        check(appearance, 3, "空闲时自动散步", "roaming")
        check(appearance, 4, "视线跟随鼠标", "follow_cursor")
        check(appearance, 5, "减少动画（只显示静态帧）", "reduced_motion")
        check(appearance, 6, "在本机保存聊天记录", "save_history")
        entry(appearance, 7, "自定义精灵图", "sprite_file")
        def choose_sprite():
            path = filedialog.askopenfilename(parent=win, title="选择精灵图", filetypes=[("透明精灵图", "*.png *.webp")])
            if path:
                variables["sprite_file"].set(path)
        controls = ttk.Frame(appearance)
        controls.grid(row=8, column=1, sticky="w", pady=5)
        ttk.Button(controls, text="选择精灵图…", command=choose_sprite).pack(side="left")
        ttk.Button(controls, text="恢复默认角色", command=lambda: variables["sprite_file"].set("")).pack(side="left", padx=8)
        ttk.Label(appearance, text="支持 8列×11行 v2 或 8列×9行 v1 精灵图，单帧192×208。\n双击人物聊天，按住人物拖动，右键打开菜单。\n关闭本地记录后，当前会话仍有上下文，退出后不保留。", foreground=MUTED, wraplength=480).grid(row=9, column=0, columnspan=2, sticky="w", pady=18)
        result = tk.StringVar(value="")
        ttk.Label(win, textvariable=result, foreground=MUTED, wraplength=620).pack(fill="x", padx=22, pady=(0, 8))
        def collect():
            cfg = dict(self.cfg)
            for key, var in variables.items():
                cfg[key] = var.get()
            for key, widget in texts.items():
                cfg[key] = widget.get("1.0", "end-1c").strip()
            for key, lo, hi, cast in [("scale",.65,2.5,float),("bubble_seconds",0,600,int),("context_rounds",1,50,int),("max_tokens",64,16384,int),("timeout",10,300,int),("temperature",0,2,float)]:
                try:
                    value = cast(cfg[key])
                    if not lo <= value <= hi:
                        raise ValueError()
                    cfg[key] = value
                except (ValueError, TypeError):
                    raise ValueError("{} 的值需要在 {}–{} 之间。".format(key,lo,hi))
            endpoint_url(cfg["base_url"])
            cfg["model"], cfg["api_key"] = cfg["model"].strip(), cfg["api_key"].strip()
            return normalize_config(cfg)
        def save():
            try:
                cfg = collect()
                sprite = Path(cfg["sprite_file"]) if cfg["sprite_file"] else resource("assets/spritesheet.png")
                atlas = SpriteAtlas(sprite, cfg["scale"], self.root)
                self.store.save_config(cfg)
                if not cfg["save_history"]:
                    self.store.save_history([])
            except (OSError, ValueError, tk.TclError) as exc:
                messagebox.showerror("设置未保存", str(exc), parent=win)
                return
            self.cfg, self.atlas, self.current_photo = cfg, atlas, None
            self.frame, self.next_frame = 0, 0
            self.apply_topmost()
            self.bubble.name.configure(text=cfg["name"])
            self.move(self.x, self.y)
            self.persist()
            win.destroy()
        def test_connection():
            try:
                cfg = collect()
                if not cfg["model"]:
                    raise ValueError("先填写模型名称。")
            except ValueError as exc:
                result.set(str(exc)); return
            test_button.configure(state="disabled")
            result.set("正在发送测试消息…")
            test_control = RequestControl()
            def test_worker():
                try:
                    reply = "".join(stream_chat(cfg, [{"role":"user", "content":"请用一句简短的中文问候我。"}], test_control))
                    status = "连接成功："+reply[:120] if reply.strip() else "连接成功但没有返回文字，请检查模型和长度参数。"
                except Exception as exc:
                    status = str(exc).replace(cfg["api_key"], "[密钥已隐藏]") if cfg["api_key"] else str(exc)
                self.events.put(("settings-test", "result", (win, result, test_button, status)))
            threading.Thread(target=test_worker, daemon=True).start()
        def import_legacy():
            initial = Path(os.environ.get("APPDATA", str(app_directory()))) / "DesktopPet"
            file = filedialog.askopenfilename(parent=win, title="选择旧版 config.json", initialdir=str(initial if initial.exists() else app_directory()), filetypes=[("JSON 配置", "*.json")])
            if not file:
                return
            try:
                data = json.loads(Path(file).read_text(encoding="utf-8-sig"))
                if not isinstance(data, dict):
                    raise ValueError("配置文件需要是 JSON 对象。")
                for old, new in (("endpoint","base_url"),("base_url","base_url"),("api_key","api_key"),("model","model")):
                    if isinstance(data.get(old), str):
                        variables[new].set(data[old])
                if isinstance(data.get("system_prompt"), str):
                    texts["background"].delete("1.0", "end")
                    texts["background"].insert("1.0", data["system_prompt"])
                    variables["persona"].set("自定义")
                result.set("已载入旧版配置，请检查接口和人物设定后保存。")
            except (OSError, ValueError) as exc:
                messagebox.showerror("导入失败", str(exc), parent=win)
        bar = ttk.Frame(win)
        bar.pack(fill="x", padx=18, pady=(0,18))
        test_button = ttk.Button(bar, text="发送测试消息", command=test_connection)
        test_button.pack(side="left")
        ttk.Button(bar, text="导入旧配置…", command=import_legacy).pack(side="left", padx=8)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="保存设置", command=save).pack(side="right", padx=8)

    def quit(self):
        if self.closed:
            return
        self.stop()
        self.persist()
        self.closed = True
        for job in (self.tick_job, self.poll_job, self.welcome_job):
            try:
                self.root.after_cancel(job)
            except tk.TclError:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir")
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--test-connection-output", help="发送无历史问候并将脱敏诊断写入指定文件")
    args = parser.parse_args()
    if args.test_connection_output:
        cfg = Store(args.data_dir or app_directory()/"data").config()
        started = time.monotonic()
        control = RequestControl()
        count = 0
        report = {"version":VERSION}
        try:
            for part in stream_chat(cfg, [{"role":"user", "content":"请只用一句中文打招呼。"}], control):
                count += len(part)
            report.update(ok=bool(count), characters=count)
        except Exception as exc:
            report.update(ok=False, error=type(exc).__name__)
            if isinstance(exc, ChatNetworkError):
                report.update(exc.diagnostic)
        report["seconds"] = round(time.monotonic()-started, 2)
        atomic_json(args.test_connection_output, report)
        return
    dpi_awareness()
    mutex = None
    if os.name == "nt" and not args.smoke_test:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        name = "Local\\BlueDressPet-"+hashlib.sha256(str(app_directory()).encode()).hexdigest()[:16]
        mutex = kernel.CreateMutexW(None, False, name)
        if ctypes.get_last_error() == 183:
            ctypes.windll.user32.MessageBoxW(None, "桌宠已经在运行，请在桌面找到人物并右键操作。", "动态桌宠", 0x40)
            return
    try:
        app = DesktopPet(args.data_dir)
    except Exception as exc:
        messagebox.showerror("桌宠启动失败", "无法启动桌宠，请检查文件夹写入权限和动画素材。\n" + str(exc)[:250])
        raise SystemExit(1)
    if args.smoke_test:
        def finish():
            import ssl
            try:
                ssl.create_default_context()
                https_ready = True
            except Exception as exc:
                https_ready = False
                app.errors.append("HTTPS context: " + type(exc).__name__)
            report = {"https_context_ready":https_ready, "composer_visible":bool(app.bubble.composer.winfo_ismapped()), "version": VERSION, "frames":len(app.atlas.photos), "bubble_visible":app.bubble.visible, "errors":app.errors}
            (app.store.directory/"smoke-test.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            app.quit()
        app.root.after(2200, finish)
    app.run()
    if mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
    if app.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

