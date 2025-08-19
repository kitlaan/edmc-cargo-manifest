from __future__ import annotations

import logging
import os
import tkinter as tk
from typing import Any, Dict, Optional

from config import appname  # pyright: ignore[reportMissingImports]
from theme import theme  # pyright: ignore[reportMissingImports]


plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f"{appname}.{plugin_name}")


class This:
    """Holds module globals."""

    def __init__(self):
        self.parent: tk.Tk

        self.ui: tk.Frame
        self.ui_cargo_count: tk.StringVar = tk.StringVar(value="")
        self.ui_manifest: tk.Frame


this = This()


def plugin_start3(plugin_dir: str) -> str:
    """
    Start the plugin.

    :param plugin_dir: Name of directory this was loaded from.
    :return: Identifier string for this plugin.
    """
    return plugin_name


def plugin_app(parent: tk.Tk) -> Optional[tk.Frame]:
    """
    Construct this plugin's main UI, if any.

    :param parent: The tk parent to place our widgets into.
    :return: See PLUGINS.md#display
    """
    this.parent = parent

    this.ui = tk.Frame(parent)
    ui_row = this.ui.grid_size()[1]

    tk.Label(this.ui, text="Cargo Manifest:").grid(row=ui_row, column=0, sticky=tk.W)
    tk.Label(this.ui, textvariable=this.ui_cargo_count, anchor=tk.W).grid(
        row=ui_row, column=1, sticky=tk.E
    )
    ui_row += 1

    this.ui_manifest = tk.Frame(this.ui)
    this.ui_manifest.grid(row=ui_row, columnspan=2, sticky=tk.NSEW)
    ui_row += 1

    return this.ui


def journal_entry(
    cmdr: str,
    is_beta: bool,
    system: str,
    station: str,
    entry: Dict[str, Any],
    state: Dict[str, Any],
) -> Optional[str]:
    """
    Handle a new Journal event.

    :param cmdr: Name of Commander.
    :param is_beta: Whether game beta was detected.
    :param system: Name of current tracked system.
    :param station: Name of current tracked station location.
    :param entry: The journal event.
    :param state: `monitor.state`
    :return: None if no error, else an error string.
    """
    return None
