"""Small themed dialogs in place of tkinter.messagebox.

On macOS, messagebox shows a native NSAlert; with a game controller connected, macOS 27 attaches a
GameControllerUI navigation session to it and the app crashed when the alert closed. These are
plain Tk windows, so that never happens, and they match the app's dark theme.
"""
import tkinter as tk
from tkinter import ttk

from i18n import _


def _dialog(parent, title, message, buttons, dark_title_bar=None):
    """Show a modal dialog; returns the index of the button pressed (or None if closed)."""
    result = {"value": None}
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    win.attributes("-topmost", True)
    frame = ttk.Frame(win, padding=18)
    frame.grid(sticky="nsew")
    ttk.Label(frame, text=message, wraplength=360, justify="left").grid(row=0, column=0, sticky="w")
    row = ttk.Frame(frame)
    row.grid(row=1, column=0, sticky="e", pady=(16, 0))

    def choose(i):
        result["value"] = i
        win.destroy()

    for i, (text, primary) in enumerate(buttons):
        ttk.Button(row, text=text, style="Accent.TButton" if primary else "TButton",
                   command=lambda i=i: choose(i)).grid(row=0, column=i, padx=(8 if i else 0, 0))
    win.bind("<Escape>", lambda e: win.destroy())
    win.update_idletasks()
    # Centre over the parent window.
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
    win.geometry(f"+{max(0, x)}+{max(0, y)}")
    if dark_title_bar:
        win.after(50, lambda: dark_title_bar(win))
    win.grab_set()
    win.focus_force()
    parent.wait_window(win)
    return result["value"]


def ask(parent, title, message, yes=None, no=None, dark_title_bar=None):
    return _dialog(parent, title, message, [(no or _("Cancel"), False), (yes or _("Yes"), True)],
                   dark_title_bar) == 1


def inform(parent, title, message, dark_title_bar=None):
    _dialog(parent, title, message, [(_("OK"), True)], dark_title_bar)
