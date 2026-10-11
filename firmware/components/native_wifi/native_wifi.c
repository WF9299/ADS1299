#include "native_wifi.h"

#include <inttypes.h>
#include <string.h>

#include "app_config.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"

static const char *TAG = "native_wifi";
static bool s_started;
static volatile bool s_connected;
static uint32_t s_retry_count;

static esp_err_t configure_static_ip(esp_netif_t *netif)
{
#if P300_WIFI_STATIC_IP_ENABLE
    if (netif == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    esp_netif_ip_info_t info = {0};
    info.ip.addr = esp_ip4addr_aton(P300_WIFI_STATIC_IP);
    info.gw.addr = esp_ip4addr_aton(P300_WIFI_STATIC_GATEWAY);
    info.netmask.addr = esp_ip4addr_aton(P300_WIFI_STATIC_NETMASK);
    ESP_RETURN_ON_ERROR(esp_netif_dhcpc_stop(netif), TAG, "stop DHCP client");
    ESP_RETURN_ON_ERROR(esp_netif_set_ip_info(netif, &info), TAG, "set static IP");
    ESP_LOGI(TAG, "static IP=%s gateway=%s netmask=%s",
             P300_WIFI_STATIC_IP, P300_WIFI_STATIC_GATEWAY, P300_WIFI_STATIC_NETMASK);
#else
    (void)netif;
#endif
    return ESP_OK;
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        const esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "initial connect failed: %s", esp_err_to_name(err));
        }
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        s_retry_count++;
        ESP_LOGW(TAG, "Wi-Fi disconnected; reconnecting (retry=%" PRIu32 ")", s_retry_count);
        const esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "reconnect request failed: %s", esp_err_to_name(err));
        }
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
        s_connected = true;
        s_retry_count = 0;
        ESP_LOGI(TAG, "Wi-Fi connected, ip=" IPSTR, IP2STR(&event->ip_info.ip));
    }
}

esp_err_t native_wifi_init(void)
{
    if (s_started) {
        return ESP_OK;
    }
    if (strlen(P300_WIFI_SSID) == 0) {
        ESP_LOGE(TAG, "P300_WIFI_SSID is empty");
        return ESP_ERR_INVALID_ARG;
    }

    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    if (netif == NULL) {
        return ESP_ERR_NO_MEM;
    }
    ESP_RETURN_ON_ERROR(configure_static_ip(netif), TAG, "configure STA IP");

    const wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&init), TAG, "esp_wifi_init");
    ESP_RETURN_ON_ERROR(esp_wifi_set_storage(WIFI_STORAGE_RAM), TAG, "set RAM storage");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                            wifi_event_handler, NULL, NULL),
        TAG, "register Wi-Fi events");
    ESP_RETURN_ON_ERROR(
        esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                            wifi_event_handler, NULL, NULL),
        TAG, "register IP events");

    wifi_config_t config = {0};
    strlcpy((char *)config.sta.ssid, P300_WIFI_SSID, sizeof(config.sta.ssid));
    strlcpy((char *)config.sta.password, P300_WIFI_PASSWORD, sizeof(config.sta.password));
    config.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    config.sta.sort_method = WIFI_CONNECT_AP_BY_SIGNAL;
    config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set STA mode");
    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &config), TAG, "set STA config");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "start Wi-Fi");
    /* Modem sleep can add latency to 20 ms EEG batches and UDP marker sync. */
    ESP_RETURN_ON_ERROR(esp_wifi_set_ps(WIFI_PS_NONE), TAG, "disable modem sleep");

    s_started = true;
    ESP_LOGI(TAG, "ESP32-C5 native Wi-Fi STA started for SSID \"%s\"", P300_WIFI_SSID);
    return ESP_OK;
}

bool native_wifi_is_connected(void)
{
    return s_connected;
}
