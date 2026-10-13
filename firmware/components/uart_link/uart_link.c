#include "uart_link.h"
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
typedef struct { uint32_t generation, baud, epoch; bool data; size_t size; uint8_t bytes[]; } tx_item_t;
static QueueHandle_t s_high,s_low;
static SemaphoreHandle_t s_wire;
static serial_parser_t s_parser;
static serial_frame_fn s_callback;
static void *s_context;
static volatile bool s_session,s_confirmed;
static volatile uint32_t s_generation,s_drops;
static uint32_t s_data_epoch;
static int64_t s_last_rx;
static vprintf_like_t s_original_log;

static bool enqueue(uint8_t type,uint32_t id,const void *payload,size_t length,uint32_t baud)
{
    if (!s_session || length>SERIAL_PAYLOAD_MAX) return false;
    tx_item_t *item=malloc(sizeof(*item)+length+16);
    if (!item) { s_drops++; return false; }
    item->generation=s_generation; item->baud=baud;
    item->data=type==SERIAL_EEG || type==SERIAL_CONFIG;
    item->epoch=s_data_epoch;
    item->size=serial_encode(type,id,payload,length,item->bytes,length+16);
    QueueHandle_t queue=(type==SERIAL_EEG || type==SERIAL_LOG)?s_low:s_high;
    if (!item->size || xQueueSend(queue,&item,0)!=pdTRUE) {
        memset(item,0,sizeof(*item)+length+16); free(item); s_drops++; return false;
    }
    return true;
}
bool uart_link_send(uint8_t type,uint32_t id,const void *payload,size_t length)
{ return enqueue(type,id,payload,length,0); }
bool uart_link_active(void) { return s_session && s_confirmed; }
void uart_link_confirm(void) { if (s_session) s_confirmed=true; }
uint32_t uart_link_generation(void) { return s_generation; }
uint32_t uart_link_drops(void) { return s_drops; }
uint32_t uart_link_errors(void) { return s_parser.errors; }
void uart_link_new_stream(void)
{
    xSemaphoreTake(s_wire,portMAX_DELAY);
    uart_wait_tx_done(UART_NUM_0,pdMS_TO_TICKS(100)); s_data_epoch++;
    xSemaphoreGive(s_wire);
}

static int log_capture(const char *format,va_list args)
{
    if (!s_session) {
        if (xSemaphoreTake(s_wire,0)!=pdTRUE) return 0;
        int result=s_original_log(format,args);
        xSemaphoreGive(s_wire); return result;
    }
    char line[192]; int length=vsnprintf(line,sizeof(line),format,args);
    if (uxQueueMessagesWaiting(s_low)>2) return length;
    cJSON *object=cJSON_CreateObject();
    if (!object) return length;
    cJSON_AddStringToObject(object,"message",line);
    char *json=cJSON_PrintUnformatted(object);
    if (json) { enqueue(SERIAL_LOG,0,json,strlen(json),0); free(json); }
    cJSON_Delete(object); return length;
}

static void receive(uint8_t type,uint32_t id,const uint8_t *data,size_t size,void *ctx)
{
    (void)ctx;
    if (type==SERIAL_HELLO) {
        cJSON *hello=cJSON_ParseWithLength((const char *)data,size);
        cJSON *requested=cJSON_GetObjectItemCaseSensitive(hello,"baud");
        int baud=cJSON_IsNumber(requested)?requested->valueint:921600;
        cJSON_Delete(hello);
        if (baud!=921600 && baud!=460800) return;
        s_session=true; s_confirmed=false; s_generation++; s_last_rx=esp_timer_get_time();
        char reply[96];
        snprintf(reply,sizeof(reply),"{\"protocol\":1,\"baud\":%d,\"generation\":%lu}",baud,(unsigned long)s_generation);
        enqueue(SERIAL_HELLO_REPLY,id,reply,strlen(reply),(uint32_t)baud);
        return;
    }
    if (!s_session) return;
    s_last_rx=esp_timer_get_time();
    if (type==SERIAL_HEARTBEAT) {
        enqueue(SERIAL_HEARTBEAT_REPLY,id,NULL,0,0); return;
    }
    if (type==SERIAL_MANAGE || (type==SERIAL_CONTROL && s_confirmed))
        s_callback(type,id,data,size,s_context);
}

static void tx_task(void *arg)
{
    (void)arg;
    while (true) {
        tx_item_t *item=NULL;
        if (xQueueReceive(s_high,&item,0)!=pdTRUE && xQueueReceive(s_low,&item,pdMS_TO_TICKS(2))!=pdTRUE)
            continue;
        xSemaphoreTake(s_wire,portMAX_DELAY);
        if (s_session && item->generation==s_generation && (!item->data || item->epoch==s_data_epoch)) {
            int written=uart_write_bytes(UART_NUM_0,item->bytes,item->size);
            if (written!=(int)item->size) s_drops++;
            if (item->baud && uart_wait_tx_done(UART_NUM_0,pdMS_TO_TICKS(200))==ESP_OK) {
                vTaskDelay(pdMS_TO_TICKS(50));
                uart_set_baudrate(UART_NUM_0,item->baud);
            }
        }
        xSemaphoreGive(s_wire);
        memset(item,0,sizeof(*item)+item->size); free(item);
    }
}
static void rx_task(void *arg)
{
    (void)arg; uint8_t data[256];
    while (true) {
        int n=uart_read_bytes(UART_NUM_0,data,sizeof(data),pdMS_TO_TICKS(10));
        if (n>0) serial_parser_feed(&s_parser,data,(size_t)n,receive,NULL);
        if (s_session && esp_timer_get_time()-s_last_rx>5000000LL) {
            xSemaphoreTake(s_wire,portMAX_DELAY);
            uart_wait_tx_done(UART_NUM_0,pdMS_TO_TICKS(100));
            s_session=false; s_confirmed=false; s_generation++;
            uart_set_baudrate(UART_NUM_0,115200);
            xSemaphoreGive(s_wire);
            s_parser.used=0;
        }
    }
}
esp_err_t uart_link_start(serial_frame_fn callback,void *ctx)
{
    if (!callback) return ESP_ERR_INVALID_ARG;
    s_callback=callback; s_context=ctx;
    s_high=xQueueCreate(12,sizeof(tx_item_t *)); s_low=xQueueCreate(6,sizeof(tx_item_t *));
    s_wire=xSemaphoreCreateMutex();
    if (!s_high || !s_low || !s_wire) return ESP_ERR_NO_MEM;
    uart_config_t cfg={.baud_rate=115200,.data_bits=UART_DATA_8_BITS,
        .parity=UART_PARITY_DISABLE,.stop_bits=UART_STOP_BITS_1,
        .flow_ctrl=UART_HW_FLOWCTRL_DISABLE,.source_clk=UART_SCLK_DEFAULT};
    esp_err_t err=uart_param_config(UART_NUM_0,&cfg);
    if (err!=ESP_OK) return err;
    err=uart_set_pin(UART_NUM_0,11,12,UART_PIN_NO_CHANGE,UART_PIN_NO_CHANGE);
    if (err!=ESP_OK) return err;
    err=uart_driver_install(UART_NUM_0,8192,0,0,NULL,0);
    if (err!=ESP_OK) return err;
    s_original_log=esp_log_set_vprintf(log_capture);
    if (xTaskCreate(tx_task,"usb_tx",4096,NULL,9,NULL)!=pdPASS ||
        xTaskCreate(rx_task,"usb_rx",6144,NULL,9,NULL)!=pdPASS) return ESP_ERR_NO_MEM;
    return ESP_OK;
}
