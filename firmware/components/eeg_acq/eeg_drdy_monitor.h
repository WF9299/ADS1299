#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

esp_err_t eeg_drdy_monitor_init(int gpio_num);
esp_err_t eeg_drdy_monitor_reset(void);
esp_err_t eeg_drdy_monitor_take_count(uint32_t *count);
esp_err_t eeg_drdy_monitor_deinit(void);
bool eeg_drdy_monitor_is_available(void);
