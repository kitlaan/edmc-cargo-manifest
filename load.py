from __future__ import annotations

import csv
import json
import logging
import os
import pathlib
import re
import tkinter as tk
from typing import Any, Dict, NotRequired, Optional, Required, Tuple, TypedDict

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


class Mission(TypedDict):
    name: str
    name_localised: str
    total: int
    remaining: int
    allocated: bool
    stolen: bool


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

        self.ship_capacity_guessed: bool = True
        self.ship_capacity: int = 0
        self.ship_cargo = dict()  # this is the full Cargo event with Inventory key

        self.srv_capacity: Optional[int] = None
        self.srv_cargo = dict()  # this is the full Cargo event with Inventory key

        self.missions: Dict[str, Mission] = {}


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
    #logger.debug(f"Journal entry received: {entry}")

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
        elif this.current_vessel:
            state_capacity = state.get("CargoCapacity", None)
            if state_capacity is not None:
                this.ship_capacity_guessed = False
                this.ship_capacity = state_capacity

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

    # Track missions, so we can show required cargo
    # Note: there's no mechanism to get all the mission details at start, other
    # than parsing past journal files. The "Missions" event only gives the IDs
    # without details. Instead, we will only track new missions from the active
    # play session. Sadness.
    if event == "Missions":
        # In theory this should be a complete list of missions, so for our
        # purposes, we can ignore 'Failed' and 'Complete'
        tracked_missions = set(this.missions.keys())
        for mission in entry.get("Active", []):
            if mission["MissionID"] in tracked_missions:
                tracked_missions.remove(mission["MissionID"])
        for mission_id in tracked_missions:
            del this.missions[mission_id]
            data_has_changed = True
    elif event == "MissionAccepted":
        if add_mission(entry):
            data_has_changed = True
    elif event in ["MissionAbandoned", "MissionCompleted", "MissionFailed"]:
        # Rely on a Cargo event to update cargo state, so just stop tracking this mission
        if entry["MissionID"] in this.missions:
            del this.missions[entry["MissionID"]]
            data_has_changed = True

    # CargoDepot gives us information on mission cargo, particularly parial completion
    if event == "CargoDepot":
        name = canonicalise(entry["CargoType"])
        if entry["MissionID"] not in this.missions:
            logger.debug(f"Unknown mission info {entry}")
            this.missions[entry["MissionID"]] = Mission(
                name=name,
                name_localised=entry.get("CargoType_Localised", entry["CargoType"]),
                total=entry["TotalItemsToDeliver"],
                remaining=entry["TotalItemsToDeliver"] - entry["ItemsDelivered"],
                allocated=entry["ItemsCollected"] > 0,
                stolen=False,  # TODO: no clue... is there a way to tell?
            )
        else:
            record = this.missions[entry["MissionID"]]
            record["remaining"] = entry["TotalItemsToDeliver"] - entry["ItemsDelivered"]

    if data_has_changed:
        update_gui()

    return None


def populate_manifest(
    manifest_frame: tk.Frame, cargo: Dict[str, Any], missions: Dict[str, Mission] = {}
) -> Tuple[bool, int]:
    """Populate the cargo manifest UI with the current cargo data."""
    for widget in manifest_frame.winfo_children():
        widget.destroy()

    if not cargo or "Inventory" not in cargo:
        return False, 0

    total = 0
    manifest = {}

    # collate all the items into a single manifest
    inventory: list[CargoItem] = cargo.get("Inventory", [])
    for item in inventory:
        total += item["Count"]

        name = canonicalise(item["Name"])
        if name in manifest:
            manifest[name]["count"] += item["Count"] - item["Stolen"]
            manifest[name]["stolen"] += item["Stolen"]
        else:
            # sometimes Cargo isn't localized, but the mission is, so search there...
            display = None
            if "Name_Localised" in item:
                display = item["Name_Localised"]
            else:
                for mission in missions.values():
                    if mission["name"] == name:
                        display = mission["name_localised"]
                        break
            if display is None:
                display = item["Name"].title()

            # give rare commodities a special flair so it's more visible
            if name in RARE_COMMODITY:
                display += " ⚜️"

            # manifest entry holds all info for a specific commodity
            manifest[name] = {
                "name": display,
                "count": item["Count"] - item["Stolen"],
                "stolen": item["Stolen"],
                "missions": {},
            }

        # If this specific inventory item has a mission associated, make sure to allocate it
        if "MissionID" in item:
            manifest[name]["missions"][item["MissionID"]] = {
                "count": item["Count"] - item["Stolen"],
                "stolen": item["Stolen"],
                "count_need": None,
                "stolen_need": None,
            }
            manifest[name]["count"] -= item["Count"] - item["Stolen"]
            manifest[name]["stolen"] -= item["Stolen"]

    # go through all of the known missions and attach them to the corresponding Cargo
    for mission_id, mission in missions.items():
        if mission["name"] not in manifest:
            manifest[mission["name"]] = {
                "name": mission["name_localised"],
                "count": 0,
                "stolen": 0,
                "missions": {},
            }
        if mission_id not in manifest[mission["name"]]["missions"]:
            manifest[mission["name"]]["missions"][mission_id] = {
                "count": 0,
                "stolen": 0,
                "count_need": None,
                "stolen_need": None,
            }
        record = manifest[mission["name"]]["missions"][mission_id]

        if mission["stolen"]:
            record["stolen_need"] = mission["remaining"]
        else:
            record["count_need"] = mission["remaining"]

        if manifest[mission["name"]]["count"] > 0 and record["count_need"]:
            allocate = min(
                manifest[mission["name"]]["count"],
                record["count_need"],
            )
            manifest[mission["name"]]["count"] -= allocate
            record["count"] = allocate
        if manifest[mission["name"]]["stolen"] > 0 and record["stolen_need"]:
            allocate = min(
                manifest[mission["name"]]["stolen"],
                record["stolen_need"],
            )
            manifest[mission["name"]]["stolen"] -= allocate
            record["stolen"] = allocate

    if missions:
        logger.debug(manifest)

    row = 0
    def make_label(count: int, symbol: str, name: str, suffix: str = ""):
        nonlocal row
        display = f"{name} [{suffix}]" if suffix else name
        tk.Label(
            manifest_frame,
            text=f"{count}",
            pady=0,
            borderwidth=0,
            highlightthickness=0,
        ).grid(row=row, column=0, sticky=tk.E)
        tk.Label(
            manifest_frame, text=symbol, pady=0, borderwidth=0, highlightthickness=0
        ).grid(row=row, column=1, padx=2)
        tk.Label(
            manifest_frame,
            text=display,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
        ).grid(row=row, column=2, sticky=tk.W)
        row += 1

    # populate the UI, sorted by name
    for item in sorted(manifest.values(), key=lambda x: x["name"]):
        # show mission details first
        mission_count = 0
        mission_stolen = 0
        for mission in item["missions"].values():
            mission_count += mission["count"]
            mission_stolen += mission["stolen"]
            if mission["count_need"] is not None:
                make_label(
                    mission["count"],
                    "🔰",
                    item["name"],
                    f"{mission['count_need']}",
                )
            elif mission["count"] > 0:
                make_label(mission["count"], "🔰", item["name"], "#?")
            if mission["stolen_need"] is not None:
                make_label(
                    mission["stolen"],
                    "📛",
                    item["name"],
                    f"need {mission['stolen_need']}",
                )
            elif mission["stolen"] > 0:
                make_label(mission["stolen"], "📛", item["name"], "#?")

        # finally show what's remaining
        if item["count"] > 0:
            make_label(item["count"], "–", item["name"])
        if item["stolen"] > 0:
            make_label(item["stolen"], "⚠️", item["name"])

    return row > 0, total


def update_gui():
    has_rows = False

    ship_has_rows = False
    ship_occupied = 0
    if this.ship_cargo is None:
        this.ui_ship_manifest.grid_remove()
    else:
        ship_has_rows, ship_occupied = populate_manifest(
            this.ui_ship_manifest, this.ship_cargo, this.missions
        )
        if ship_has_rows:
            this.ui_ship_manifest.grid()
            theme.update(this.ui_ship_manifest)
            has_rows = True
        else:
            this.ui_ship_manifest.grid_remove()

    if (this.ship_capacity_guessed and this.ship_capacity < ship_occupied):
        this.ship_capacity = ship_occupied

    if this.ship_capacity == 0 and not ship_has_rows:
        this.ui_ship_info.grid_remove()
    else:
        capacity = this.ship_capacity
        remaining = capacity - ship_occupied
        marker = "?" if this.ship_capacity_guessed else ""
        this.ui_ship_info_text.set(f"Ship Manifest: {ship_occupied} / {capacity}{marker} [{remaining}]")
        this.ui_ship_info.grid()
        has_rows = True

    if not this.current_vessel_is_srv:
        this.ui_srv_info.grid_remove()
        this.ui_srv_manifest.grid_remove()
    else:
        srv_has_rows = False
        srv_occupied = 0
        if this.srv_capacity is None:
            this.ui_srv_manifest.grid_remove()
        else:
            srv_has_rows, srv_occupied = populate_manifest(this.ui_srv_manifest, this.srv_cargo)
            if srv_has_rows:
                this.ui_srv_manifest.grid()
                theme.update(this.ui_srv_manifest)
                has_rows = True
            else:
                this.ui_srv_manifest.grid_remove()

        if this.srv_capacity is None and not srv_has_rows:
            this.ui_srv_info.grid_remove()
        else:
            if this.srv_capacity is None:
                this.ui_srv_info_text.set(f"SRV Manifest: {srv_occupied} / ???")
            else:
                capacity = this.srv_capacity
                remaining = this.srv_capacity - srv_occupied
                this.ui_srv_info_text.set(f"SRV Manifest: {srv_occupied} / {capacity} [{remaining}]")
            this.ui_srv_info.grid()
            has_rows = True

    # If we're showing no details, show a placeholder
    if not has_rows:
        capacity = this.ship_capacity if not this.ship_capacity_guessed else "???"
        this.ui_ship_info_text.set(f"Ship Capacity: {capacity}")
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


def add_mission(mission: dict) -> bool:
    type = mission["Name"].lower()
    if type.startswith(("mission_onfoot_", "mission_sightseeing_")):
        return False

    if "Commodity" not in mission or "Count" not in mission:
        return False

    # Here are the type of missions that have ship commodities:
    # Mission_Altruism
    # Mission_Collect*
    # Mission_Delivery* -- allocated
    # Mission_Mining*
    # Mission_Rescue* -- stolen?
    # Mission_Salvage* -- stolen?

    # TODO: verify the stolen mission mapping

    # Some missions require "stolen" cargo. Some require "allocation"...
    is_allocated = type.startswith("mission_delivery")
    is_stolen = type.startswith(("mission_rescue", "mission_salvage"))

    name = canonicalise(mission["Commodity"])
    this.missions[mission["MissionID"]] = Mission(
        name=name,
        name_localised=mission.get("Commodity_Localised", mission["Commodity"]),
        total=mission["Count"],
        remaining=mission["Count"],
        allocated=is_allocated,
        stolen=is_stolen,
    )
    return True
