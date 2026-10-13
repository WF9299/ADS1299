#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define SERIAL_PAYLOAD_MAX 4096U
#define SERIAL_FRAME_MAX (SERIAL_PAYLOAD_MAX + 16U)
enum {
    SERIAL_HELLO=1, SERIAL_HELLO_REPLY, SERIAL_MANAGE, SERIAL_MANAGE_REPLY,
    SERIAL_CONFIG, SERIAL_EEG, SERIAL_CONTROL, SERIAL_CONTROL_REPLY,
    SERIAL_LOG, SERIAL_HEARTBEAT, SERIAL_HEARTBEAT_REPLY
};
typedef void (*serial_frame_fn)(uint8_t type, uint32_t id,
                               const uint8_t *payload, size_t size, void *ctx);
typedef struct {
    uint8_t data[SERIAL_FRAME_MAX];
    size_t used;
    uint32_t errors;
} serial_parser_t;
uint32_t serial_crc32c(const uint8_t *data, size_t size);
size_t serial_encode(uint8_t type, uint32_t id, const uint8_t *payload,
                     size_t size, uint8_t *out, size_t capacity);
void serial_parser_feed(serial_parser_t *parser, const uint8_t *data, size_t size,
                        serial_frame_fn callback, void *ctx);
