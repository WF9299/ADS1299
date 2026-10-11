#pragma once

#include "driver/gpio.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    gpio_num_t ads_drdy;
    gpio_num_t ads_reset;
    gpio_num_t ads_cs;
    gpio_num_t ads_sclk;
    gpio_num_t ads_mosi;
    gpio_num_t ads_miso;
    gpio_num_t ads_start;
    gpio_num_t ads_pwdn;
} board_config_t;

board_config_t board_default_config(void);
void board_log_config(const board_config_t *config);

#ifdef __cplusplus
}
#endif
