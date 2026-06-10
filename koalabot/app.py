"""Tkinter GUI for KoalaBot - Python port with live updates and high-concurrency async bots."""

from __future__ import annotations
import asyncio
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import Optional, Dict

from .core import MAX_BOTS, PlayerStatus, BotRunResult, normalize_platform
from .runner import run_live

import sys
from pathlib import Path

def _resource_path(relative: str) -> Path:
    """Get absolute path to resource, works for dev and PyInstaller onefile."""
    if getattr(sys, "frozen", False):
        base = Path(sys._MEIPASS)  # type: ignore[attr-defined]
        return base / relative

    # Dev mode: search upwards from this file until we find the assets folder
    here = Path(__file__).resolve().parent
    for candidate in (here, here.parent, here.parent.parent, Path.cwd()):
        p = candidate / relative
        if p.exists():
            return p
        # Also check if assets lives next to the candidate
        p2 = candidate / "assets" / Path(relative).name
        if p2.exists():
            return p2
    # Fallback
    return here.parent / relative

# ---------------- Cross-thread communication ----------------
class GuiEvent:
    PLAYER = "player"
    LOG = "log"
    FINISHED = "finished"
    ERROR = "error"

class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("KoalaBot")
        root.geometry("980x620")
        root.minsize(820, 520)

        # Set app icon (works in dev and when bundled)
        try:
            icon_path = _resource_path("assets/logo.png")
            if icon_path.exists():
                icon_img = tk.PhotoImage(file=str(icon_path))
                root.iconphoto(True, icon_img)
                self._icon_img = icon_img  # keep reference
        except Exception:
            pass  # icon is optional

        try:
            ico_path = _resource_path("assets/logo.ico")
            if ico_path.exists():
                root.iconbitmap(str(ico_path))
        except Exception:
            pass

        self.stop_event: Optional[asyncio.Event] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.event_queue: "queue.Queue[Dict]" = queue.Queue()
        self.players: Dict[str, PlayerStatus] = {}  # name -> status
        self.current_result: Optional[BotRunResult] = None

        self._build_ui()
        self._poll_queue()

    def _build_ui(self):
        root = self.root

        # Top control frame
        ctrl = ttk.Frame(root, padding=10)
        ctrl.pack(fill="x")

        ttk.Label(ctrl, text="Platform:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.platform_var = tk.StringVar(value="Kahoot")
        self.platform_cb = ttk.Combobox(
            ctrl, textvariable=self.platform_var, values=["Kahoot", "Blooket", "Gimkit", "LessonUp"],
            state="readonly", width=12
        )
        self.platform_cb.grid(row=0, column=1, padx=4)

        ttk.Label(ctrl, text="Game PIN:").grid(row=0, column=2, sticky="w", padx=(12, 4))
        self.pin_var = tk.StringVar()
        self.pin_entry = ttk.Entry(ctrl, textvariable=self.pin_var, width=18)
        self.pin_entry.grid(row=0, column=3, padx=4)

        ttk.Label(ctrl, text="Bots:").grid(row=0, column=4, sticky="w", padx=(12, 4))
        self.count_var = tk.IntVar(value=40)
        self.count_spin = ttk.Spinbox(
            ctrl, from_=1, to=MAX_BOTS, textvariable=self.count_var, width=6
        )
        self.count_spin.grid(row=0, column=5, padx=4)

        ttk.Label(ctrl, text="Name prefix:").grid(row=0, column=6, sticky="w", padx=(12, 4))
        self.prefix_var = tk.StringVar(value="Bot")
        self.prefix_entry = ttk.Entry(ctrl, textvariable=self.prefix_var, width=14)
        self.prefix_entry.grid(row=0, column=7, padx=4)

        # Buttons
        btns = ttk.Frame(ctrl)
        btns.grid(row=0, column=8, padx=16)
        self.launch_btn = ttk.Button(btns, text="LAUNCH BOTS", command=self.on_launch, width=16)
        self.launch_btn.pack(side="left", padx=3)
        self.stop_btn = ttk.Button(btns, text="STOP", command=self.on_stop, width=10, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        # Stats row
        stats = ttk.Frame(root, padding=(10, 2))
        stats.pack(fill="x")
        self.stats_labels: Dict[str, ttk.Label] = {}
        for i, key in enumerate(["attempted", "joined", "failed", "completed", "totalAnswers", "successRate"]):
            lbl = ttk.Label(stats, text=f"{key}: 0", font=("Segoe UI", 10, "bold"))
            lbl.pack(side="left", padx=8)
            self.stats_labels[key] = lbl

        # Main content: Tree + Log
        main = ttk.PanedWindow(root, orient="horizontal")
        main.pack(fill="both", expand=True, padx=8, pady=6)

        # Players table
        tree_frame = ttk.Frame(main)
        columns = ("#", "name", "status", "joined", "answers", "last", "error")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=18)
        self.tree.heading("#", text="#")
        self.tree.heading("name", text="Name")
        self.tree.heading("status", text="Status")
        self.tree.heading("joined", text="Joined")
        self.tree.heading("answers", text="Answers")
        self.tree.heading("last", text="Last Action")
        self.tree.heading("error", text="Error")

        self.tree.column("#", width=40, anchor="center")
        self.tree.column("name", width=160)
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("joined", width=60, anchor="center")
        self.tree.column("answers", width=70, anchor="center")
        self.tree.column("last", width=220)
        self.tree.column("error", width=180)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        main.add(tree_frame, weight=3)

        # Log
        log_frame = ttk.Frame(main)
        ttk.Label(log_frame, text="Log").pack(anchor="w")
        self.log_text = tk.Text(log_frame, height=12, wrap="word", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        main.add(log_frame, weight=2)

        # Bottom bar
        bottom = ttk.Frame(root, padding=8)
        bottom.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready. Enter a PIN and press LAUNCH BOTS.")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left")

        ttk.Button(bottom, text="Copy Summary", command=self.copy_summary).pack(side="right", padx=4)
        ttk.Button(bottom, text="Export JSON", command=self.export_json).pack(side="right", padx=4)
        ttk.Button(bottom, text="Clear", command=self.clear_all).pack(side="right", padx=4)

        # Footer note
        note = ttk.Label(
            root,
            text=f"Python port • asyncio + aiohttp • up to {MAX_BOTS} concurrent bots • Tkinter UI",
            foreground="#666",
            font=("Segoe UI", 8),
        )
        note.pack(anchor="e", padx=10, pady=(0, 6))

    # ---------------- Queue polling (thread safe) ----------------
    def _poll_queue(self):
        try:
            while True:
                ev = self.event_queue.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _handle_event(self, ev: Dict):
        typ = ev.get("type")
        if typ == GuiEvent.PLAYER:
            p: PlayerStatus = ev["player"]
            self.players[p.name] = p
            self._upsert_tree_row(p)
            self._refresh_stats()
        elif typ == GuiEvent.LOG:
            self._append_log(ev.get("msg", ""))
        elif typ == GuiEvent.FINISHED:
            self.current_result = ev.get("result")
            self._append_log("=== Run finished ===")
            self._set_running(False)
            self.status_var.set("Finished. You can export results or launch again.")
        elif typ == GuiEvent.ERROR:
            self._append_log("ERROR: " + str(ev.get("error")))
            self._set_running(False)
            messagebox.showerror("Error", str(ev.get("error")))

    def _upsert_tree_row(self, p: PlayerStatus):
        # Find existing or insert
        for iid in self.tree.get_children():
            if self.tree.set(iid, "name") == p.name:
                self.tree.item(iid, values=(
                    self.tree.set(iid, "#"),
                    p.name,
                    p.status,
                    "yes" if p.joined else "no",
                    p.answers,
                    p.last_action,
                    p.error or "",
                ))
                # Colorize
                self._color_row(iid, p)
                return

        # New row
        idx = len(self.tree.get_children()) + 1
        iid = self.tree.insert("", "end", values=(
            idx, p.name, p.status, "yes" if p.joined else "no", p.answers, p.last_action, p.error or ""
        ))
        self._color_row(iid, p)

    def _color_row(self, iid: str, p: PlayerStatus):
        if p.status == "failed":
            self.tree.item(iid, tags=("fail",))
        elif p.status == "completed":
            self.tree.item(iid, tags=("done",))
        elif p.joined:
            self.tree.item(iid, tags=("ok",))
        self.tree.tag_configure("ok", foreground="#0a7d2f")
        self.tree.tag_configure("done", foreground="#1e5aa8")
        self.tree.tag_configure("fail", foreground="#b22")

    def _refresh_stats(self):
        joined = sum(1 for p in self.players.values() if p.joined)
        failed = len(self.players) - joined
        completed = sum(1 for p in self.players.values() if p.status == "completed")
        total_ans = sum(p.answers for p in self.players.values())
        rate = f"{round((joined / max(1, len(self.players))) * 100)}%" if self.players else "0%"
        self.stats_labels["attempted"].config(text=f"attempted: {len(self.players)}")
        self.stats_labels["joined"].config(text=f"joined: {joined}")
        self.stats_labels["failed"].config(text=f"failed: {failed}")
        self.stats_labels["completed"].config(text=f"completed: {completed}")
        self.stats_labels["totalAnswers"].config(text=f"totalAnswers: {total_ans}")
        self.stats_labels["successRate"].config(text=f"successRate: {rate}")

    def _append_log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_running(self, running: bool):
        if running:
            self.launch_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
            self.platform_cb.config(state="disabled")
            self.pin_entry.config(state="disabled")
            self.count_spin.config(state="disabled")
            self.prefix_entry.config(state="disabled")
        else:
            self.launch_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            self.platform_cb.config(state="readonly")
            self.pin_entry.config(state="normal")
            self.count_spin.config(state="normal")
            self.prefix_entry.config(state="normal")

    # ---------------- Actions ----------------
    def on_launch(self):
        platform = self.platform_var.get()
        pin = self.pin_var.get().strip()
        count = int(self.count_var.get())
        prefix = self.prefix_var.get().strip() or "Bot"

        if not pin:
            messagebox.showwarning("Missing PIN", "Please enter a game PIN.")
            return
        if not (1 <= count <= MAX_BOTS):
            messagebox.showwarning("Count", f"Bot count must be between 1 and {MAX_BOTS}.")
            return

        # Reset UI state
        self.clear_all(keep_inputs=True)
        self._set_running(True)
        self.status_var.set(f"Running {count} bots on {platform}...")

        self.stop_event = asyncio.Event()
        self._append_log(f"Launching {count} {platform} bots for pin {pin}...")

        # Start background asyncio loop + task
        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            try:
                result = loop.run_until_complete(
                    run_live(
                        platform=platform,
                        game_pin=pin,
                        player_count=count,
                        name_prefix=prefix,
                        on_player_update=lambda p: self.event_queue.put({"type": GuiEvent.PLAYER, "player": p}),
                        on_log=lambda m: self.event_queue.put({"type": GuiEvent.LOG, "msg": m}),
                        stop_event=self.stop_event,
                    )
                )
                self.event_queue.put({"type": GuiEvent.FINISHED, "result": result})
            except Exception as e:
                self.event_queue.put({"type": GuiEvent.ERROR, "error": str(e)})
            finally:
                try:
                    loop.close()
                except Exception:
                    pass
                self.loop = None

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def on_stop(self):
        if self.stop_event:
            self.stop_event.set()
            self._append_log("Stop requested. Waiting for bots to wind down...")
            self.status_var.set("Stopping...")

    def clear_all(self, keep_inputs: bool = False):
        self.tree.delete(*self.tree.get_children())
        self.players.clear()
        self.current_result = None
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        for k, lbl in self.stats_labels.items():
            lbl.config(text=f"{k}: 0")
        if not keep_inputs:
            self.pin_var.set("")
            self.count_var.set(40)
            self.prefix_var.set("Bot")
        self.status_var.set("Ready.")

    def copy_summary(self):
        if not self.current_result:
            # build from live players
            from .core import make_summary
            summary = make_summary(list(self.players.values()))
            text = json.dumps({
                "platform": self.platform_var.get(),
                "gamePin": self.pin_var.get(),
                "summary": summary,
            }, indent=2)
        else:
            text = json.dumps({
                "platform": self.current_result.platform,
                "gamePin": self.current_result.game_pin,
                "joinUrl": self.current_result.join_url,
                "config": self.current_result.config,
                "summary": self.current_result.summary,
            }, indent=2)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Summary copied to clipboard.")

    def export_json(self):
        if not self.current_result and not self.players:
            messagebox.showinfo("Export", "Nothing to export yet.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            title="Export KoalaBot results"
        )
        if not path:
            return

        if self.current_result:
            data = {
                "platform": self.current_result.platform,
                "gamePin": self.current_result.game_pin,
                "joinUrl": self.current_result.join_url,
                "config": self.current_result.config,
                "summary": self.current_result.summary,
                "players": self.current_result.players,
                "note": self.current_result.note,
            }
        else:
            from .core import make_summary
            data = {
                "platform": self.platform_var.get(),
                "gamePin": self.pin_var.get(),
                "summary": make_summary(list(self.players.values())),
                "players": [p.to_dict() for p in self.players.values()],
            }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self.status_var.set(f"Exported to {path}")

def main():
    root = tk.Tk()
    # Try a nicer theme if available
    try:
        root.tk.call("source", "azure.tcl")  # optional
        ttk.Style().theme_use("azure")
    except Exception:
        pass
    App(root)
    root.mainloop()

if __name__ == "__main__":
    main()
