#include "esp_err.h"
#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include "ads1299.h"
#include "app_config.h"
#include "board_config.h"
#include "eeg_acq.h"
#include "native_wifi.h"
#include "p300_stream.h"

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

static void start_sntp(void)
{
    const esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(P300_SNTP_SERVER);
    const esp_err_t err = esp_netif_sntp_init(&config);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "SNTP start failed: %s; TCP waits for valid Unix time", esp_err_to_name(err));
    }
}

void app_main(void)
{
    ESP_LOGI(TAG, "ADS1299 P300 firmware: 8ch %u SPS TCP EEG_BATCH v1",
             (unsigned)P300_SAMPLE_RATE_HZ);
    ESP_ERROR_CHECK(init_nvs());

    const board_config_t board = board_default_config();
    board_log_config(&board);
    const ads1299_config_t ads = ads1299_default_config(&board);
    if (ads1299_init(&ads) != ESP_OK || eeg_acq_start(&board) != ESP_OK) {
        ESP_LOGE(TAG, "ADS1299 acquisition startup failed");
        return;
    }

    ESP_ERROR_CHECK(native_wifi_init());
    while (!native_wifi_is_connected()) {
        ESP_LOGI(TAG, "waiting for Wi-Fi station before TCP/marker services");
        vTaskDelay(pdMS_TO_TICKS(1000));
    }

    start_sntp();
    ESP_ERROR_CHECK(p300_stream_start());
}
