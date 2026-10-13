#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
typedef struct {
    uint32_t version;
    char ssid[33], password[65], monitor_id[65], pair_key[65], host[16];
    uint16_t port;
    uint8_t wifi_mode;
} runtime_config_t;
esp_err_t runtime_config_init(void);
void runtime_config_get(runtime_config_t *out);
esp_err_t runtime_config_save(const runtime_config_t *config);
void runtime_config_set_endpoint(const char *host, uint16_t port);
bool runtime_config_endpoint(char host[16], uint16_t *port);
bool runtime_config_wifi_mode(void);
void runtime_config_set_mode(bool wifi);
