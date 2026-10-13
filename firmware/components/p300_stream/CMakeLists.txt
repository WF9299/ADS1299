idf_component_register(
    SRCS "p300_stream.c" "p300_protocol.c" "p300_bias_state.c" "p300_tx_control.c" "p300_tx_group.c"
    INCLUDE_DIRS "include"
    REQUIRES eeg_acq native_wifi app_config runtime_config uart_link esp_timer esp_hw_support lwip
)

if(DEFINED P300_TCP_COALESCE_FRAMES)
    target_compile_definitions(${COMPONENT_LIB}
        PRIVATE "P300_TCP_COALESCE_FRAMES=${P300_TCP_COALESCE_FRAMES}")
endif()
