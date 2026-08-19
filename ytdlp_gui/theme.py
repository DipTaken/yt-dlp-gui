"""Catppuccin Mocha palette and the ttk style setup built on it.

Every colour in the app comes from here — never hard-code a hex value at a call
site.
"""
import tkinter as tk
from tkinter import ttk

# Catppuccin Mocha palette
# ─────────────────────────────────────────────────────────────────────────────
BASE   = '#1e1e2e'
MANTLE = '#181825'
CRUST  = '#11111b'
SURF0  = '#313244'
SURF1  = '#45475a'
SURF2  = '#585b70'
OVL0   = '#6c7086'
TEXT   = '#cdd6f4'
SUBT0  = '#a6adc8'
MAUVE  = '#cba6f7'
BLUE   = '#89b4fa'
GREEN  = '#a6e3a1'
RED    = '#f38ba8'
YELLOW = '#f9e2af'
PEACH  = '#fab387'


def apply_dark_titlebar(win):
    """Ask DWM for a dark title bar. No-op off Windows / on older builds."""
try:
    import ctypes
    hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
    ctypes.windll.dwmapi.DwmSetWindowAttribute(
        hwnd, 20, ctypes.byref(ctypes.c_int(1)), ctypes.sizeof(ctypes.c_int))
except Exception:
    pass


def build_styles(root):
    """Configure every ttk style the app uses."""
    st = ttk.Style(root)
    st.theme_use('clam')
    st.configure('.', background=BASE, foreground=TEXT,
                 font=('Segoe UI', 10), borderwidth=0, relief='flat')
    st.configure('TFrame', background=BASE)
    st.configure('TLabel', background=BASE, foreground=TEXT, font=('Segoe UI', 10))

    # Combobox
    st.configure('TCombobox', fieldbackground=SURF0, foreground=TEXT,
                 background=SURF0, selectbackground=SURF1,
                 arrowcolor=SUBT0, borderwidth=1, padding=(8, 5))
    st.map('TCombobox',
           fieldbackground=[('focus', SURF1)],
           selectbackground=[('!focus', SURF0)],
           bordercolor=[('focus', MAUVE), ('!focus', SURF1)])

    # Checkbutton
    st.configure('TCheckbutton', background=MANTLE, foreground=TEXT,
                 focuscolor='', indicatorcolor=SURF1)
    st.map('TCheckbutton',
           background=[('active', MANTLE)],
           foreground=[('active', TEXT)],
           indicatorcolor=[('selected', MAUVE), ('!selected', SURF1)])

    # Progress bars
    st.configure('Horizontal.TProgressbar', background=MAUVE,
                 troughcolor=SURF1, borderwidth=0, thickness=5)
    st.configure('Green.Horizontal.TProgressbar', background=GREEN,
                 troughcolor=SURF1, borderwidth=0, thickness=5)

    # Notebook (tabbed panels)
    st.configure('TNotebook', background=MANTLE, borderwidth=0,
                 tabmargins=[0, 0, 0, 0])
    st.configure('TNotebook.Tab', background=SURF0, foreground=SUBT0,
                 font=('Segoe UI', 10, 'bold'), padding=[16, 8], borderwidth=0)
    st.map('TNotebook.Tab',
           background=[('selected', BASE), ('active', SURF1)],
           foreground=[('selected', MAUVE), ('active', TEXT)])

    # Scrollbar
    st.configure('TScrollbar', background=SURF0, troughcolor=MANTLE,
                 arrowcolor=SUBT0, borderwidth=0, width=10)
    st.map('TScrollbar', background=[('active', SURF1)])

    # Treeview
    st.configure('Treeview', background=SURF0, fieldbackground=SURF0,
                 foreground=TEXT, rowheight=24, font=('Segoe UI', 9),
                 borderwidth=0)
    st.configure('Treeview.Heading', background=MANTLE, foreground=MAUVE,
                 font=('Segoe UI', 9, 'bold'), borderwidth=0)
    st.map('Treeview',
           background=[('selected', MAUVE)],
           foreground=[('selected', CRUST)])
