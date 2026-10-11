idf_component_register(
    SRCS "eeg_acq.c" "eeg_impedance_math.c" "eeg_drdy_health.c" "eeg_drdy_monitor.c"
    INCLUDE_DIRS "include"
    REQUIRES ads1299 board_config app_config esp_timer esp_driver_gpio esp_driver_pcnt heap
)
