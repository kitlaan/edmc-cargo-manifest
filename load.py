from __future__ import annotations

import csv
import json
import logging
import os
import pathlib
import re
import tkinter as tk
from typing import Any, Dict, NotRequired, Optional, Required, TypedDict

from config import appname, config  # pyright: ignore[reportMissingImports]
from theme import theme  # pyright: ignore[reportMissingImports]


plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f"{appname}.{plugin_name}")

CANONICALISE_RE = re.compile(r"\$(.+)_name;")

# Track the list of rare commodity symbols ourselves, as there isn't metadata
# readily available. But we have to load it from CSV at start.
RARE_COMMODITY: set[str] = set()


class CargoItem(TypedDict):
    Name: Required[str]
    Name_Localised: NotRequired[str]
    Count: Required[int]
    Stolen: Required[int]


class This:
    """Holds module globals."""

    def __init__(self):
        self.parent: tk.Tk
        self.ui: tk.Frame

        self.ui_ship_info: tk.Label
        self.ui_ship_info_text: tk.StringVar = tk.StringVar()
        self.ui_ship_manifest: tk.Frame

        self.ui_srv_info: tk.Label
        self.ui_srv_info_text: tk.StringVar = tk.StringVar()
        self.ui_srv_manifest: tk.Frame

        self.reset()

    def reset(self):
        self.current_vessel: str = ""
        self.current_vessel_is_srv: bool = False

        self.ship_capacity: Optional[int] = None
        self.ship_cargo = dict()  # TODO: what're we storing? what's the type?

        self.srv_capacity: Optional[int] = None
        self.srv_cargo = dict()  # TODO: what're we storing? what's the type?


this = This()


def plugin_start3(plugin_dir: str) -> str:
    """
    Start the plugin.

    :param plugin_dir: Name of directory this was loaded from.
    :return: Identifier string for this plugin.
    """
    # In prod, don't include DEBUG logging
    if not get_local_file(".git"):
        logger.setLevel(logging.INFO)

    return plugin_name


def plugin_app(parent: tk.Tk) -> Optional[tk.Frame]:
    """
    Construct this plugin's main UI, if any.

    :param parent: The tk parent to place our widgets into.
    :return: See PLUGINS.md#display
    """
    this.parent = parent
    this.ui = tk.Frame(parent)

    this.ui_ship_info = tk.Label(this.ui, textvariable=this.ui_ship_info_text)
    this.ui_ship_info.grid(row=0, sticky=tk.W)
    this.ui_ship_info.grid_remove()

    this.ui_ship_manifest = tk.Frame(this.ui)
    this.ui_ship_manifest.grid(row=1, sticky=tk.NSEW)
    this.ui_ship_manifest.columnconfigure(2, weight=1)
    this.ui_ship_manifest.grid_remove()

    this.ui_srv_info = tk.Label(this.ui, textvariable=this.ui_srv_info_text)
    this.ui_srv_info.grid(row=2, sticky=tk.W)
    this.ui_srv_info.grid_remove()

    this.ui_srv_manifest = tk.Frame(this.ui)
    this.ui_srv_manifest.grid(row=3, sticky=tk.NSEW)
    this.ui_srv_manifest.columnconfigure(2, weight=1)
    this.ui_srv_manifest.grid_remove()

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
    event = entry["event"]
    logger.debug(f"Journal entry received: {entry}")

    data_has_changed = False

    # Manage the state of the GUI
    if event == "ShutDown":
        # Cleanup the GUI when quitting the game.
        # Note: The game sends 'Shutdown' for a clean quit, where EDMC will always
        # send a 'ShutDown' when it detects any type of quit.
        this.reset()
        cleanup_gui()
        return None
    elif event in ["LoadGame", "StartUp"]:
        # Make sure GUI is set up at game start
        load_commodity_csv()
        setup_gui()
    elif event == "Resurrect":
        # Reset cargo tracking on resurrection
        this.reset()
        data_has_changed = True

    # Track the current vessel (ship/SRV) that we're in, so we can use it
    # to track cargo later and figure out the cargo capacity.
    if event in ["LoadGame", "Loadout", "LaunchSRV"]:
        if event in ["LoadGame", "Loadout"]:
            this.current_vessel = canonicalise(entry.get("Ship", ""))
        elif event == "LaunchSRV":
            this.current_vessel = canonicalise(entry.get("SRVType", ""))

        this.current_vessel_is_srv = is_srv(this.current_vessel)
        if this.current_vessel_is_srv:
            # There is no Loadout for SRVs, so we have to guess
            this.srv_capacity = get_srv_capacity(this.current_vessel)
            logger.debug(f"SRV capacity: {this.srv_capacity}")
        elif this.current_vessel:
            this.ship_capacity = state.get("CargoCapacity", None)
            logger.debug(f"Ship capacity: {this.ship_capacity}")

        data_has_changed = True

    # Cargo events only include the Inventory key on first load; EDSM helps
    # by coalescing CargoJSON when it's not present, but only when Vessel==Ship.
    #
    # In theory, Cargo events get generated whenever anything affects cargo,
    # but the game is not consistent on doing this after the associated action.
    # There are a lot of events:
    #  * CollectCargo, MarketBuy, BuyDrones, MiningRefined
    #    Type[_Localised], +Count, [StolenGoods]
    #  * EjectCargo, MarketSell, SellDrones
    #    Type[_Localised], -Count, [StolenGoods]
    #  * SearchAndRescue
    #    Name[_Localised], -Count
    #  * TechnologyBroker
    #    Commodities[]: Name[_Localised], -Count
    #  * MissionCompleted
    #    CommodityReward[]: Name[_Localised], +Count
    #  * EngineerContribution
    #    Commodity[_Localised], -Quantity
    #  * CargoTransfer
    #    Transfers[]: Type[_Localised], Count, Direction(toship/tosrv/tocarrier)
    #  * CarrierDepositFuel
    #    -Amount
    #  * CargoDepot
    #    UpdateType(Collect/Deliver), CargoType[_Localised], Count
    # Note that not all events will include StolenGoods tag, so YMMV...
    # (e.g. did the cmdr transfer a stolen or not-stolen item? no clue)

    if event in "Cargo":
        match entry.get("Vessel"):
            case "Ship":
                this.ship_cargo = entry if "Inventory" in entry else state["CargoJSON"]
                logger.debug(f"Ship cargo load: {this.ship_cargo}")
            case "SRV":
                this.srv_cargo = (
                    entry if "Inventory" in entry else load_json("Cargo") or {}
                )
                logger.debug(f"SRV cargo load: {this.srv_cargo}")

        data_has_changed = True
    elif event == "StartUp":
        # At startup, we don't get back-filled any events or state.
        # We can load Cargo.json to get what we're currently holding (and pray
        # that it's up-to-date), but that still doesn't help the other variables.
        data = load_json("Cargo")
        if data:
            match data.get("Vessel"):
                case "Ship":
                    this.ship_cargo = data
                    logger.debug(f"Ship cargo load at startup: {this.ship_cargo}")
                case "SRV":
                    this.srv_cargo = data
                    logger.debug(f"SRV cargo load at startup: {this.srv_cargo}")

            data_has_changed = True

    # TODO: Does entry[Inventory] give us what we need? Unlikely, as I think
    # I want to separate mission delivery from normal cargo.
    # TODO: Can I display all missions together, or show them separately?
    # If we mission stack, how ugly does that look?

    # TODO: Mission
    # TODO: ModuleInfo
    # TODO: state[Modules]

    if data_has_changed:
        update_gui()

    return None


def populate_manifest(manifest_frame: tk.Frame, cargo: Dict[str, Any]) -> int:
    """Populate the cargo manifest UI with the current cargo data."""
    for widget in manifest_frame.winfo_children():
        widget.destroy()

    if not cargo or "Inventory" not in cargo:
        return 0

    total = 0
    manifest = {}

    # collate all the items
    inventory: list[CargoItem] = cargo.get("Inventory", [])
    for item in inventory:
        total += item["Count"]
        name = canonicalise(item["Name"])
        if name in manifest:
            manifest[name]["count"] += item["Count"]
            manifest[name]["stolen"] += item["Stolen"]
        else:
            display: str = (
                item["Name_Localised"] if "Name_Localised" in item else item["Name"]
            )
            if name in RARE_COMMODITY:
                display += " ⚜️"
            manifest[name] = {
                "name": display,
                "count": item["Count"],
                "stolen": item["Stolen"],
            }

    # populate the UI, sorted by name
    row = 0
    for item in sorted(manifest.values(), key=lambda x: x["name"]):
        count = item["count"] - item["stolen"]
        if count > 0:
            tk.Label(manifest_frame, text=f"{count}", pady=0, borderwidth=0, highlightthickness=0).grid(
                row=row, column=0, sticky=tk.E
            )
            tk.Label(manifest_frame, text="–", pady=0, borderwidth=0, highlightthickness=0).grid(row=row, column=1)
            tk.Label(manifest_frame, text=item["name"], pady=0, borderwidth=0, highlightthickness=0).grid(
                row=row, column=2, sticky=tk.W
            )
            row += 1
        if item["stolen"]:
            tk.Label(manifest_frame, text=f"{item['stolen']}", pady=0, borderwidth=0, highlightthickness=0).grid(
                row=row, column=0, sticky=tk.E
            )
            tk.Label(manifest_frame, text="⚠️", pady=0, borderwidth=0, highlightthickness=0).grid(row=row, column=1)
            tk.Label(manifest_frame, text=item["name"], pady=0, borderwidth=0, highlightthickness=0).grid(
                row=row, column=2, sticky=tk.W
            )
            row += 1

    return total


def update_gui():
    has_rows = False

    ship_occupied = 0
    if this.ship_cargo is None:
        this.ui_ship_manifest.grid_remove()
    else:
        ship_occupied = populate_manifest(this.ui_ship_manifest, this.ship_cargo)
        if ship_occupied == 0:
            this.ui_ship_manifest.grid_remove()
        else:
            this.ui_ship_manifest.grid()
            theme.update(this.ui_ship_manifest)
            has_rows = True

    if this.ship_capacity is None and ship_occupied == 0:
        this.ui_ship_info.grid_remove()
    else:
        ship_capacity = this.ship_capacity if this.ship_capacity is not None else "???"
        this.ui_ship_info_text.set(f"Ship Manifest: {ship_occupied} / {ship_capacity}")
        this.ui_ship_info.grid()
        has_rows = True

    if not this.current_vessel_is_srv:
        this.ui_srv_info.grid_remove()
        this.ui_srv_manifest.grid_remove()
    else:
        srv_occupied = 0
        if this.srv_capacity is None:
            this.ui_srv_manifest.grid_remove()
        else:
            srv_occupied = populate_manifest(this.ui_srv_manifest, this.srv_cargo)
            if srv_occupied == 0:
                this.ui_srv_manifest.grid_remove()
            else:
                this.ui_srv_manifest.grid()
                theme.update(this.ui_srv_manifest)
                has_rows = True

        if this.srv_capacity is None and srv_occupied == 0:
            this.ui_srv_info.grid_remove()
        else:
            srv_capacity = this.srv_capacity if this.srv_capacity is not None else "???"
            this.ui_srv_info_text.set(f"SRV Manifest: {srv_occupied} / {srv_capacity}")
            this.ui_srv_info.grid()
            has_rows = True

    # If we're showing no details, show a placeholder
    if not has_rows:
        ship_capacity = this.ship_capacity if this.ship_capacity is not None else "???"
        this.ui_ship_info_text.set(f"Ship Capacity: {ship_capacity}")
        this.ui_ship_info.grid()

def setup_gui():
    # Nothing yet!
    pass


def cleanup_gui():
    this.ui_ship_info.grid_remove()
    this.ui_ship_manifest.grid_remove()

    this.ui_srv_info.grid_remove()
    this.ui_srv_manifest.grid_remove()

    this.ui_ship_info_text.set("")
    for widget in this.ui_ship_manifest.winfo_children():
        widget.destroy()

    this.ui_srv_info_text.set("")
    for widget in this.ui_srv_manifest.winfo_children():
        widget.destroy()


def load_commodity_csv():
    """Load commodity CSV files from EDMC data directory."""

    # Figure out which file to use
    def get_file(filename: str) -> Optional[pathlib.Path]:
        try:
            commodityfile = config.app_dir_path / "FDevIDs" / filename
            if pathlib.Path.is_file(commodityfile):
                return commodityfile
        except FileNotFoundError:
            pass

        commodityfile = pathlib.Path(f"FDevIDs/{filename}")
        if pathlib.Path.is_file(commodityfile):
            return commodityfile

        return None

    # Reminder: the commodity.csv and rare_commodity.csv are only updated after
    # plugin_start().
    commodityfile = get_file("rare_commodity.csv")
    if commodityfile:
        RARE_COMMODITY.clear()
        with open(commodityfile, newline="", encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                symbol = row.get("symbol")
                if symbol:
                    RARE_COMMODITY.add(canonicalise(symbol))


def canonicalise(item: str) -> str:
    """Convert an item name to a canonical form for comparison."""
    item = item.lower()
    match = CANONICALISE_RE.match(item)
    return match.group(1) if match else item


def is_srv(vessel: str) -> bool:
    return vessel in ["testbuggy", "combat_multicrew_srv_01"]


def get_srv_capacity(vessel: str) -> Optional[int]:
    match vessel:
        case "testbuggy":
            return 4
        case "combat_multicrew_srv_01":
            return 2
        case _:
            return None


def load_json(item: str) -> Optional[dict]:
    journaldir = config.get_str("journaldir") or config.default_journal_dir
    filepath = pathlib.Path(journaldir) / f"{item}.json"
    try:
        with filepath.open("rb") as f:
            return json.load(f)
    except:
        return None


def get_local_file(filename: str) -> Optional[pathlib.Path]:
    plugin_dir = pathlib.Path(__file__).parent
    filepath = plugin_dir / filename
    return filepath if filepath.is_file() else None
