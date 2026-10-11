idf_component_register(
    SRCS "ads1299.c"
    INCLUDE_DIRS "include"
    REQUIRES board_config app_config esp_driver_gpio esp_driver_spi
)
