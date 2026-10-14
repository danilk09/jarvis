-- WirePlumber 0.4 (Raspberry Pi OS Bookworm). Installed by scripts/pocket/setup_bench.sh into
-- ~/.config/wireplumber/bluetooth.lua.d/  (same settings as 51-jarvis-bluetooth.conf)

bluez_monitor.properties["bluez5.roles"] = "[ a2dp_sink a2dp_source bap_sink bap_source hsp_hs hsp_ag hfp_hf hfp_ag ]"
bluez_monitor.properties["bluez5.headset-roles"] = "[ hsp_hs hsp_ag hfp_hf hfp_ag ]"
bluez_monitor.properties["bluez5.hfphsp-backend"] = "native"
bluez_monitor.properties["bluez5.enable-msbc"] = true
bluez_monitor.properties["bluez5.enable-hw-volume"] = true
-- headless Pi: don't turn Bluetooth audio off because nobody is logged in on a seat
bluez_monitor.properties["with-logind"] = false
