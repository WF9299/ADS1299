#include "native_wifi.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "runtime_config.h"
#include "cJSON.h"
#include "esp_timer.h"
#include "esp_check.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_wifi_default.h"

static const char *TAG = "native_wifi";
static bool s_initialized;
static volatile bool s_enabled, s_connected, s_connecting, s_scanning;
static volatile int s_reason;
static bool s_has_ssid;
static esp_netif_t *s_netif;

static void connect_retry(void *arg)
{
    (void)arg;
    if (s_enabled && s_has_ssid && !s_scanning && !s_connected && !s_connecting)
        s_connecting = esp_wifi_connect() == ESP_OK;
}

static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        connect_retry(NULL);
        return;
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        s_connecting = false;
        s_reason = ((wifi_event_sta_disconnected_t *)data)->reason;
        return;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *event = (const ip_event_got_ip_t *)data;
        s_connected = true;
        s_connecting = false;
        s_reason = 0;
        ESP_LOGI(TAG, "Wi-Fi connected, ip=" IPSTR, IP2STR(&event->ip_info.ip));
    }
}

esp_err_t native_wifi_init(void)
{
    if (s_initialized) {
        return ESP_OK;
    }
    esp_err_t err = esp_netif_init();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }
    err = esp_event_loop_create_default();
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        return err;
    }

    s_netif = esp_netif_create_default_wifi_sta();
    if (s_netif == NULL) {
        return ESP_ERR_NO_MEM;
    }

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

    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "set STA mode");
    runtime_config_t config;
    runtime_config_get(&config);
    err = native_wifi_configure(config.ssid, config.password);
    memset(&config, 0, sizeof(config));
    ESP_RETURN_ON_ERROR(err, TAG, "set runtime network");
    esp_timer_handle_t timer;
    esp_timer_create_args_t args = {.callback=connect_retry, .name="wifi_retry"};
    ESP_RETURN_ON_ERROR(esp_timer_create(&args, &timer), TAG, "retry timer");
    ESP_RETURN_ON_ERROR(esp_timer_start_periodic(timer, 2000000), TAG, "retry start");
    s_initialized = true;
    return native_wifi_enable(runtime_config_wifi_mode());
}

bool native_wifi_is_connected(void)
{
    return s_enabled && s_connected;
}

esp_err_t native_wifi_configure(const char *ssid, const char *password)
{
    if (!ssid || !password || strlen(ssid)>32 || strlen(password)>64)
        return ESP_ERR_INVALID_ARG;
    s_has_ssid=false; s_connected=false; s_connecting=false;
    if (s_enabled) esp_wifi_disconnect();
    wifi_config_t cfg={0};
    memcpy(cfg.sta.ssid,ssid,strlen(ssid));
    memcpy(cfg.sta.password,password,strlen(password));
    cfg.sta.scan_method=WIFI_ALL_CHANNEL_SCAN;
    cfg.sta.sort_method=WIFI_CONNECT_AP_BY_SIGNAL;
    cfg.sta.threshold.authmode=password[0]?WIFI_AUTH_WPA2_PSK:WIFI_AUTH_OPEN;
    esp_err_t err=esp_wifi_set_config(WIFI_IF_STA,&cfg);
    memset(&cfg,0,sizeof(cfg));
    if (err==ESP_OK) { s_has_ssid=ssid[0]!=0; connect_retry(NULL); }
    return err;
}

esp_err_t native_wifi_enable(bool enable)
{
    if (enable==s_enabled) return ESP_OK;
    s_enabled=enable;
    esp_err_t err=enable?esp_wifi_start():esp_wifi_stop();
    if (err!=ESP_OK) { s_enabled=!enable; return err; }
    if (enable) return esp_wifi_set_ps(WIFI_PS_NONE);
    s_connected=false; s_connecting=false; return ESP_OK;
}

void native_wifi_get_status(char ip[16], int *reason)
{
    esp_netif_ip_info_t info={0};
    strcpy(ip,"0.0.0.0"); *reason=s_reason;
    if (native_wifi_is_connected() && esp_netif_get_ip_info(s_netif,&info)==ESP_OK)
        snprintf(ip,16,IPSTR,IP2STR(&info.ip));
}

char *native_wifi_scan_json(void)
{
    bool was_enabled=s_enabled;
    s_scanning=true;
    if (native_wifi_enable(true)!=ESP_OK) { s_scanning=false; return NULL; }
    wifi_scan_config_t scan={0};
    esp_err_t err=esp_wifi_scan_start(&scan,true);
    cJSON *array=cJSON_CreateArray();
    if (err==ESP_OK) {
        wifi_ap_record_t records[12]; uint16_t count=12;
        if (esp_wifi_scan_get_ap_records(&count,records)==ESP_OK) {
            for (uint16_t i=0;i<count;i++) {
                char ssid[33]={0}; memcpy(ssid,records[i].ssid,32);
                cJSON *ap=cJSON_CreateObject();
                cJSON_AddStringToObject(ap,"ssid",ssid);
                cJSON_AddNumberToObject(ap,"rssi",records[i].rssi);
                cJSON_AddNumberToObject(ap,"auth",records[i].authmode);
                cJSON_AddItemToArray(array,ap);
            }
        }
    }
    if (!was_enabled) native_wifi_enable(false);
    s_scanning=false; connect_retry(NULL);
    char *json=err==ESP_OK?cJSON_PrintUnformatted(array):NULL;
    cJSON_Delete(array); return json;
}
