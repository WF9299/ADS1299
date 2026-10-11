idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES app_config board_config native_wifi ads1299 eeg_acq p300_stream esp_netif nvs_flash
)
