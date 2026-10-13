idf_component_register(SRCS "uart_link.c" INCLUDE_DIRS "include"
    REQUIRES serial_protocol esp_driver_uart esp_timer log espressif__cjson)
