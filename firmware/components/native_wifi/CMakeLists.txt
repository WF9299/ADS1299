idf_component_register(
    SRCS "native_wifi.c"
    INCLUDE_DIRS "include"
    REQUIRES runtime_config esp_common esp_event esp_netif esp_wifi esp_timer espressif__cjson
)
