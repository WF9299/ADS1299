#pragma once
#include "esp_err.h"
#include "serial_protocol.h"
esp_err_t uart_link_start(serial_frame_fn callback, void *ctx);
bool uart_link_send(uint8_t type, uint32_t id, const void *payload, size_t length);
bool uart_link_active(void);
void uart_link_confirm(void);
uint32_t uart_link_generation(void);
uint32_t uart_link_drops(void);
uint32_t uart_link_errors(void);
void uart_link_new_stream(void);
