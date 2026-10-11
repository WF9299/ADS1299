#pragma once

#include "esp_err.h"
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t native_wifi_init(void);
bool native_wifi_is_connected(void);

#ifdef __cplusplus
}
#endif
