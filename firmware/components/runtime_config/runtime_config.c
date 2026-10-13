#include "runtime_config.h"
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"
static runtime_config_t s_config;
static SemaphoreHandle_t s_lock;
static volatile bool s_wifi_mode;
esp_err_t runtime_config_init(void)
{
    s_lock=xSemaphoreCreateMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
    s_config=(runtime_config_t){.version=1,.port=1234};
    nvs_handle_t nvs;
    esp_err_t err=nvs_open("p300_network",NVS_READWRITE,&nvs);
    if (err!=ESP_OK) return err;
    runtime_config_t saved={0}; size_t size=sizeof(saved);
    if (nvs_get_blob(nvs,"config",&saved,&size)==ESP_OK && size==sizeof(saved) &&
        saved.version==1 && saved.ssid[32]==0 && saved.password[64]==0 &&
        saved.monitor_id[64]==0 && saved.pair_key[64]==0 && saved.host[15]==0 &&
        saved.port!=0 && saved.wifi_mode<=1) s_config=saved;
    nvs_close(nvs); s_wifi_mode=s_config.wifi_mode!=0;
    return ESP_OK;
}
void runtime_config_get(runtime_config_t *out)
{ xSemaphoreTake(s_lock,portMAX_DELAY); *out=s_config; xSemaphoreGive(s_lock); }
esp_err_t runtime_config_save(const runtime_config_t *c)
{
    if (!c || c->version!=1 || !c->port || c->wifi_mode>1 ||
        c->ssid[32] || c->password[64] || c->monitor_id[64] || c->pair_key[64] || c->host[15])
        return ESP_ERR_INVALID_ARG;
    xSemaphoreTake(s_lock,portMAX_DELAY);
    if (memcmp(&s_config,c,sizeof(*c))==0) { xSemaphoreGive(s_lock); return ESP_OK; }
    nvs_handle_t nvs;
    esp_err_t err=nvs_open("p300_network",NVS_READWRITE,&nvs);
    if (err==ESP_OK) {
        err=nvs_set_blob(nvs,"config",c,sizeof(*c));
        if (err==ESP_OK) err=nvs_commit(nvs);
        nvs_close(nvs);
    }
    if (err==ESP_OK) s_config=*c;
    xSemaphoreGive(s_lock); return err;
}
void runtime_config_set_endpoint(const char *host,uint16_t port)
{
    xSemaphoreTake(s_lock,portMAX_DELAY);
    strlcpy(s_config.host,host,sizeof(s_config.host)); s_config.port=port;
    xSemaphoreGive(s_lock);
}
bool runtime_config_endpoint(char host[16],uint16_t *port)
{
    xSemaphoreTake(s_lock,portMAX_DELAY);
    strlcpy(host,s_config.host,16); *port=s_config.port;
    bool valid=host[0]!=0; xSemaphoreGive(s_lock); return valid;
}
bool runtime_config_wifi_mode(void) { return s_wifi_mode; }
void runtime_config_set_mode(bool wifi) { s_wifi_mode=wifi; }
