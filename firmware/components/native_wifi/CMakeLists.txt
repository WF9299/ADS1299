idf_component_register(
    SRCS "native_wifi.c"
    INCLUDE_DIRS "include"
    REQUIRES app_config esp_common esp_event esp_netif esp_wifi
)
