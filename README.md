# EDMC Cargo Manifest

Elite: Dangerous Market Connector (EDMC) plugin to display information about
the cargo inventory.

## Caveats

ED doesn't refresh existing mission details upon start, so we can't provide any
up-to-date mission cargo information.  A possible workaround is to parse older
journal files looking for MissionIDs.

ED doesn't refresh Ship Loadout cargo capacity on start, so we don't have an
accurate cargo capacity until the ship loadout is viewed. Even parsing the
ModuleInfo isn't helpful since Sept-2025, as there's now Engineered Cargo Racks.

ED isn't consistent with sending localised commodity strings, so sometimes the
displayed names will be weird/wrong.

## TODO

* stolen cargo is tracked separately, but not fully tested
  * verify that the mission names (Mission_Rescue, Mission_Salvage) are tagged
    properly to normal/stolen cargo.

## Notes

Requires Python 3.11+ (EDMC 5.7.0+).
Developed starting with EDMC 5.13.0, so YMMV.

Acknowledgements:
* Inspired by [RemainNA/cargo-manifest](https://github.com/RemainNA/cargo-manifest).
