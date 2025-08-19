from __future__ import annotations

import csv
import json
import logging
import os
import pathlib
import re
import tkinter as tk
from typing import Any, Dict, Optional, TypedDict

from config import appname, config  # pyright: ignore[reportMissingImports]
from theme import theme  # pyright: ignore[reportMissingImports]


plugin_name = os.path.basename(os.path.dirname(__file__))
logger = logging.getLogger(f"{appname}.{plugin_name}")

CANONICALISE_RE = re.compile(r"\$(.+)_name;")

# Track the list of rare commodity symbols ourselves, as there isn't metadata
# readily available. But we have to load it from CSV at start.
RARE_COMMODITY: set[str] = set()


class This:
    """Holds module globals."""

    def __init__(self):
        self.parent: tk.Tk

        self.ui: tk.Frame
        self.ui_cargo_count: tk.StringVar = tk.StringVar(value="")
        self.ui_manifest: tk.Frame

        self.reset()

    def reset(self):
        self.current_vessel: str = ""
        self.current_vessel_is_srv: bool = False

        self.ship_capacity: Optional[int] = None
        self.ship_cargo = dict()  # TODO

        self.srv_capacity: Optional[int] = None
        self.srv_cargo = dict()  # TODO


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
    event = entry["event"]
    logger.debug(f"Journal entry received: {entry}")

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

    data_has_changed = False

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
                this.ship_cargo = state["CargoJSON"]
                logger.debug(f"Ship cargo load: {this.ship_cargo}")
            case "SRV":
                this.srv_cargo = load_json("Cargo") or {}
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
        # TODO: our GUI stuff
        pass

    return None


def setup_gui():
    pass
    # TODO: any Tk mucking to prepare the GUI


def cleanup_gui():
    pass
    # TODO: any Tk mucking, like emptying out the cargo list or counts


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
