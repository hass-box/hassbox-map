DOMAIN = "hassbox_map"
CONF_TENCENT_MAP_KEY = "tencent_map_key"

CONF_REPORT_LOCATION = "report_location"

BRIDGE_WS_URL = "wss://gps.hasss.cn/ws"

WS_TYPE_GET_CONFIG = "hassbox/gps/get_config"
WS_TYPE_GET_TRACK = "hassbox/gps/get_track"
WS_TYPE_LIST_GEOFENCES = "hassbox/gps/list_geofences"
WS_TYPE_SAVE_GEOFENCE = "hassbox/gps/save_geofence"
WS_TYPE_DELETE_GEOFENCE = "hassbox/gps/delete_geofence"
WS_TYPE_SET_REPORT_INTERVAL = "hassbox/gps/set_report_interval"
WS_TYPE_SET_TIMER_POWER = "hassbox/gps/set_timer_power"
WS_TYPE_REFRESH_LOCATION = "hassbox/gps/refresh_location"
WS_TYPE_REBOOT_DEVICE = "hassbox/gps/reboot_device"

WS_TYPE_GET_ACCOUNT = "hassbox/gps/get_account"
WS_TYPE_GET_QR_CODE = "hassbox/gps/get_qr_code"
WS_TYPE_CHECK_BIND = "hassbox/gps/check_bind"
WS_TYPE_UNBIND_ACCOUNT = "hassbox/gps/unbind_account"

WS_TYPE_BIND_DEVICE = "hassbox/gps/bind_device"
WS_TYPE_UNBIND_DEVICE = "hassbox/gps/unbind_device"

WS_TYPE_SET_MAP_KEY = "hassbox/gps/set_map_key"
WS_TYPE_SET_REPORT_LOCATION = "hassbox/gps/set_report_location"

STORAGE_KEY = f"{DOMAIN}.geofences"
DEVICES_STORAGE_KEY = f"{DOMAIN}.devices"
STORAGE_VERSION = 1

PANEL_URL_PATH = "hassbox-map"
PANEL_TITLE = "HassBox 地图"
PANEL_ICON = "mdi:map-marker-path"
STATIC_URL_PATH = "/hassbox_map_static"

DATA_STORE = "store"
DATA_WS_REGISTERED = "ws_registered"
DATA_TRACKERS = "trackers"
DATA_FENCE_MANAGER = "fence_manager"
DATA_BRIDGE = "bridge"

DATA_FENCES_SIGNAL = f"{DOMAIN}_geofences_updated"

EVENT_FENCES_CHANGED = f"{DOMAIN}_fences_changed"
