#include "board_config.h"

#include "esp_log.h"

static const char *TAG = "board_config";

board_config_t board_default_config(void)
{
    /* R8 release schematic: ESP32-C5-WROOM-1U-N8R8 to ADS1299. */
    return (board_config_t) {
        .ads_drdy = GPIO_NUM_23,
        .ads_reset = GPIO_NUM_24,
        .ads_cs = GPIO_NUM_10,
        .ads_sclk = GPIO_NUM_6,
        .ads_mosi = GPIO_NUM_8,
        .ads_miso = GPIO_NUM_9,
        .ads_start = GPIO_NUM_0,
        .ads_pwdn = GPIO_NUM_1,
    };
}

void board_log_config(const board_config_t *config)
{
    if (config == NULL) {
        return;
    }
    ESP_LOGI(TAG, "ADS1299 pins DRDY=%d RESET=%d CS=%d SCLK=%d MOSI=%d MISO=%d START=%d PWDN=%d",
             config->ads_drdy,
             config->ads_reset,
             config->ads_cs,
             config->ads_sclk,
             config->ads_mosi,
             config->ads_miso,
             config->ads_start,
             config->ads_pwdn);
}
