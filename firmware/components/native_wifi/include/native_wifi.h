#pragma once

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t native_wifi_init(void);
bool native_wifi_is_connected(void);
esp_err_t native_wifi_enable(bool enable);
esp_err_t native_wifi_configure(const char *ssid, const char *password);
void native_wifi_get_status(char ip[16], int *reason);
char *native_wifi_scan_json(void);

#ifdef __cplusplus
}
#endif
