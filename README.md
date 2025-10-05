# EDMC Cargo Manifest

Elite: Dangerous Market Connector (EDMC) plugin to display information about
the cargo inventory.

## Caveats

ED doesn't refresh existing mission details upon start, so we can't provide any
up-to-date mission cargo information.

> This plugin will cache cargo missions, so they're available across game
> instances. If EDMC wasn't running when the mission is accepted, then the
> mission details will not be available.

ED doesn't refresh Ship Loadout cargo capacity on start, so we don't have an
accurate cargo capacity until the ship loadout is viewed. Even parsing the
ModuleInfo isn't helpful since Sept-2025, as there's now Engineered Cargo Racks.

> This plugin will guess using the maximum stored cargo, until an explicit
> capacity event is received.

ED isn't consistent with sending localised commodity strings, so sometimes the
displayed names will be weird/wrong.

ED don't consistently write a Cargo entry. Usually one gets written within
20-seconds of changes, but we've (rarely) seen them not happen entirely. While
we could workaround by tracking all cargo-effecting entries, figuring out the
metadata (e.g. stolen) for cargo is not always possible.

## TODO

* stolen cargo is tracked separately, but not fully tested
  * verify that the mission names (Mission_Rescue, Mission_Salvage) are tagged
    properly to normal/stolen cargo.

## Maintenance Notes

* SRV cargo capacity is hardcoded based on SRV identifier; if a new SRV type
  is added, `get_srv_capacity()` and `is_srv()` will need to be updated.

## Notes

Requires Python 3.11+ (EDMC 5.7.0+).
Developed starting with EDMC 5.13.0, so YMMV.

Acknowledgements:
* Inspired by [RemainNA/cargo-manifest](https://github.com/RemainNA/cargo-manifest).
