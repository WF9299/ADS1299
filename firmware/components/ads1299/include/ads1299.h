#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "board_config.h"
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    board_config_t board;
    uint32_t spi_clock_hz;
    uint8_t enabled_channel_mask;
} ads1299_config_t;

typedef struct {
    uint8_t status[3];
    int32_t channel[8];
} ads1299_frame_t;

typedef struct {
    uint8_t tested_channel_mask;
    uint8_t loff_statp;
    uint8_t loff_statn;
} ads1299_leadoff_result_t;

ads1299_config_t ads1299_default_config(const board_config_t *board);
esp_err_t ads1299_init(const ads1299_config_t *config);
esp_err_t ads1299_read_id(uint8_t *id);
esp_err_t ads1299_read_reg(uint8_t reg, uint8_t *value);
esp_err_t ads1299_write_reg(uint8_t reg, uint8_t value);
esp_err_t ads1299_apply_verified_reference_config(bool enable_all_channels);
esp_err_t ads1299_apply_verified_reference_config_with_bias(uint8_t bias_mask);
esp_err_t ads1299_set_bias_sensp_verified(uint8_t bias_mask);
esp_err_t ads1299_apply_internal_test_config(void);
esp_err_t ads1299_apply_internal_short_config(void);
esp_err_t ads1299_measure_leadoff(ads1299_leadoff_result_t *result);
esp_err_t ads1299_configure_leadoff_dc(uint8_t channel_mask);
esp_err_t ads1299_configure_leadoff_ac(uint8_t channel_mask);
esp_err_t ads1299_read_leadoff_status(uint8_t channel_mask,
                                     ads1299_leadoff_result_t *result);
esp_err_t ads1299_start_continuous(void);
esp_err_t ads1299_stop_continuous(void);
esp_err_t ads1299_read_frame(ads1299_frame_t *frame);

#ifdef __cplusplus
}
#endif
