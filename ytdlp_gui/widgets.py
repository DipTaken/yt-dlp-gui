"""Reusable widget builders, tooltips and mouse-wheel routing.

All styling for the settings panels funnels through here so the two tabs and
the preferences window stay visually identical. Colours come from theme.
"""
import tkinter as tk
from tkinter import ttk

from .theme import MANTLE, MAUVE, SUBT0, SURF0, SURF1, SURF2, TEXT


class WheelRouter:
    """Routes <MouseWheel> to whichever scrollable canvas the cursor is over.

    tkinter delivers wheel events to the focused widget, not the hovered one,
    so every scrollable area registers here and Enter/Leave picks the target.
    The accumulator keeps fractional touchpad deltas from being discarded.
    """

    def __init__(self, root):
        self._target = None
        self._accum = 0.0
        root.bind_all('<MouseWheel>', self._on_wheel)

    def set_target(self, canvas):
        self._target = canvas

    def _on_wheel(self, event):
        if not self._target:
            return
        self._accum -= event.delta / 120.0
        units = int(self._accum)
        if units:
            try:
                self._target.yview_scroll(units, 'units')
            except tk.TclError:
                self._target = None
            self._accum -= units

    def bind_tree(self, widget, canvas):
        """Recursively bind Enter/Leave so any child activates the right canvas."""
        widget.bind('<Enter>', lambda _e: self.set_target(canvas), add='+')
        widget.bind('<Leave>', lambda _e: self.set_target(None), add='+')
        for child in widget.winfo_children():
            self.bind_tree(child, canvas)


def section(parent, text, collapsed=False):
    """Create a collapsible section. Returns the body frame; pack children
    into it (not into `parent`).

    `collapsed=True` starts folded — use it for groups that are configured once
    and then ignored, so they don't crowd the panel.
    """
    hdr = tk.Frame(parent, bg=MANTLE, cursor='hand2')
    hdr.pack(fill='x', padx=16, pady=(14, 4))
    chev = tk.Label(hdr, text='▾  ', bg=MANTLE, fg=MAUVE,
                    font=('Segoe UI', 9, 'bold'), cursor='hand2')
    chev.pack(side='left')
    tk.Label(hdr, text=text, bg=MANTLE, fg=MAUVE,
             font=('Segoe UI', 9, 'bold'), cursor='hand2').pack(side='left')
    tk.Frame(hdr, bg=SURF1, height=1).pack(side='left', fill='x',
                                           expand=True, padx=(8, 0), pady=3)

    body = tk.Frame(parent, bg=MANTLE)
    state = {'open': not collapsed}
    if collapsed:
        chev.configure(text='▸  ')
    else:
        body.pack(fill='x')

    def toggle(_e=None):
        if state['open']:
            body.pack_forget()
            chev.configure(text='▸  ')
        else:
            # `after=hdr` is essential: a bare pack() appends the body at the
            # end of the panel, so collapsing then re-expanding a section
            # teleported it to the bottom of the settings list.
            body.pack(fill='x', after=hdr)
            chev.configure(text='▾  ')
        state['open'] = not state['open']

    for w in (hdr, chev) + tuple(hdr.winfo_children()):
        w.bind('<Button-1>', toggle)
    return body

def entry(parent, var):
    return tk.Entry(parent, textvariable=var,
                    bg=SURF0, fg=TEXT, insertbackground=TEXT,
                    font=('Segoe UI', 10), relief='flat', bd=0,
                    highlightthickness=1, highlightbackground=SURF1,
                    highlightcolor=MAUVE)

def check(parent, text, var):
    ttk.Checkbutton(parent, text=text, variable=var).pack(
        anchor='w', padx=16, pady=2)

def labeled_combo(parent, label, var, values, on_select=None):
    """Returns the wrapper frame so callers can use it as a pack anchor."""
    f = tk.Frame(parent, bg=MANTLE)
    f.pack(fill='x', padx=16, pady=3)
    tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
             font=('Segoe UI', 9)).pack(anchor='w')
    cb = ttk.Combobox(f, textvariable=var, values=values,
                      state='readonly', font=('Segoe UI', 10))
    cb.pack(fill='x', pady=(2, 0))
    if on_select:
        cb.bind('<<ComboboxSelected>>', on_select)
    return f

def labeled_entry(parent, label, var, width=None):
    f = tk.Frame(parent, bg=MANTLE)
    f.pack(fill='x', padx=16, pady=3)
    tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
             font=('Segoe UI', 9)).pack(anchor='w')
    e = entry(f, var)
    if width:
        e.configure(width=width)
        e.pack(anchor='w', pady=(2, 0), ipady=4)
    else:
        e.pack(fill='x', pady=(2, 0), ipady=4)

def mini_combo(parent, label, var, values):
    f = tk.Frame(parent, bg=MANTLE)
    f.pack(side='left', fill='x', expand=True)
    tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
             font=('Segoe UI', 9)).pack(anchor='w')
    ttk.Combobox(f, textvariable=var, values=values,
                 state='readonly', width=10).pack(fill='x', pady=(2, 0))

def mini_entry(parent, label, var, width=8):
    f = tk.Frame(parent, bg=MANTLE)
    f.pack(side='left')
    tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
             font=('Segoe UI', 9)).pack(anchor='w')
    e = entry(f, var)
    e.configure(width=width)
    e.pack(anchor='w', ipady=4)

def browse_row(parent, label, var, cmd):
    f = tk.Frame(parent, bg=MANTLE)
    f.pack(fill='x', padx=16, pady=3)
    tk.Label(f, text=label, bg=MANTLE, fg=SUBT0,
             font=('Segoe UI', 9)).pack(anchor='w')
    row = tk.Frame(f, bg=MANTLE)
    row.pack(fill='x', pady=(2, 0))
    entry(row, var).pack(side='left', fill='x', expand=True, ipady=4)
    tk.Button(row, text='…', command=cmd,
              bg=SURF1, fg=TEXT, font=('Segoe UI', 10), relief='flat',
              bd=0, padx=8, pady=4, cursor='hand2',
              activebackground=SURF2).pack(side='left', padx=(4, 0))

def tooltip(widget, text):
    """Attach a small tooltip that appears on hover. Returns the widget so
    the call can be chained inside `.pack()`."""
    tip = {'win': None, 'after_id': None}

    def show():
        if tip['win']:
            return
        x = widget.winfo_rootx() + 12
        y = widget.winfo_rooty() + widget.winfo_height() + 4
        w = tk.Toplevel(widget)
        w.wm_overrideredirect(True)
        w.wm_geometry(f'+{x}+{y}')
        w.configure(bg=SURF1)
        tk.Label(w, text=text, bg=SURF1, fg=TEXT,
                 font=('Segoe UI', 8), padx=6, pady=2).pack()
        tip['win'] = w

    def hide(_e=None):
        if tip['after_id']:
            widget.after_cancel(tip['after_id'])
            tip['after_id'] = None
        if tip['win']:
            tip['win'].destroy()
            tip['win'] = None

    def schedule(_e):
        hide()
        tip['after_id'] = widget.after(500, show)

    widget.bind('<Enter>', schedule, add='+')
    widget.bind('<Leave>', hide,     add='+')
    widget.bind('<ButtonPress>', hide, add='+')
    return widget
