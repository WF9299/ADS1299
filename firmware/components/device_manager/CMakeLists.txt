idf_component_register(SRCS "device_manager.c" INCLUDE_DIRS "include"
    REQUIRES runtime_config native_wifi uart_link p300_stream eeg_acq espressif__cjson esp_timer esp_hw_support lwip mbedtls)
if(DEFINED P300_DISCOVERY_PORT)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE "P300_DISCOVERY_PORT=${P300_DISCOVERY_PORT}")
endif()
