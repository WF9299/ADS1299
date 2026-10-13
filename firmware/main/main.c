#include "esp_err.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "ads1299.h"
#include "app_config.h"
#include "board_config.h"
#include "eeg_acq.h"
#include "native_wifi.h"
#include "p300_stream.h"
#include "runtime_config.h"
#include "device_manager.h"

static const char *TAG = "p300_acq";

static esp_err_t init_nvs(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    return err;
}

void app_main(void)
{
    ESP_LOGI(TAG, "ADS1299 P300 firmware: 8ch %u SPS USB/Wi-Fi EEG_BATCH v1",
             (unsigned)P300_SAMPLE_RATE_HZ);
    ESP_ERROR_CHECK(init_nvs());
    ESP_ERROR_CHECK(runtime_config_init());
    ESP_ERROR_CHECK(device_manager_start());

    const board_config_t board = board_default_config();
    board_log_config(&board);
    const ads1299_config_t ads = ads1299_default_config(&board);
    if (ads1299_init(&ads) != ESP_OK || eeg_acq_start(&board) != ESP_OK) {
        ESP_LOGE(TAG, "ADS1299 acquisition startup failed");
        return;
    }

    ESP_ERROR_CHECK(p300_stream_start());
}
