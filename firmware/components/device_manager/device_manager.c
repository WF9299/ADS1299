#include "device_manager.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "cJSON.h"
#include "eeg_acq.h"
#include "esp_mac.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/sockets.h"
#include "lwip/inet.h"
#include "psa/crypto.h"
#include "native_wifi.h"
#include "p300_stream.h"
#include "runtime_config.h"
#include "uart_link.h"
#ifndef P300_DISCOVERY_PORT
#define P300_DISCOVERY_PORT 1236
#endif
#if P300_DISCOVERY_PORT < 1 || P300_DISCOVERY_PORT > 65535
#error "P300_DISCOVERY_PORT must be in 1..65535"
#endif
typedef struct { uint32_t id,generation; size_t size; char json[]; } job_t;
typedef struct { uint32_t id,generation,crc; char *reply; } cached_t;
static QueueHandle_t s_jobs;
static bool s_wifi_ready;
static cached_t s_cache[8];
static unsigned s_cache_next;
static const char *string(cJSON *object,const char *name)
{ cJSON *v=cJSON_GetObjectItemCaseSensitive(object,name); return cJSON_IsString(v)?v->valuestring:NULL; }
static bool hex_key(const char *hex,uint8_t key[32])
{
    if (!hex || strlen(hex)!=64) return false;
    for (unsigned i=0;i<32;i++) {
        char pair[3]={hex[i*2],hex[i*2+1],0};
        if (!isxdigit((unsigned char)pair[0]) || !isxdigit((unsigned char)pair[1])) return false;
        key[i]=(uint8_t)strtoul(pair,NULL,16);
    }
    return true;
}
static bool hmac_hex(const char *hex,const char *message,char out[65])
{
    uint8_t key[32],digest[32];
    if (!hex_key(hex,key)) return false;
    psa_key_attributes_t attributes=PSA_KEY_ATTRIBUTES_INIT;
    psa_set_key_type(&attributes,PSA_KEY_TYPE_HMAC);
    psa_set_key_bits(&attributes,256);
    psa_set_key_usage_flags(&attributes,PSA_KEY_USAGE_SIGN_MESSAGE);
    psa_set_key_algorithm(&attributes,PSA_ALG_HMAC(PSA_ALG_SHA_256));
    psa_key_id_t key_id=0; size_t produced=0;
    psa_status_t err=psa_crypto_init();
    if (err==PSA_SUCCESS) err=psa_import_key(&attributes,key,sizeof(key),&key_id);
    if (err==PSA_SUCCESS) err=psa_mac_compute(key_id,PSA_ALG_HMAC(PSA_ALG_SHA_256),
        (const uint8_t *)message,strlen(message),digest,sizeof(digest),&produced);
    if (key_id) psa_destroy_key(key_id);
    psa_reset_key_attributes(&attributes);
    memset(key,0,sizeof(key)); if (err!=PSA_SUCCESS || produced!=32) return false;
    for (unsigned i=0;i<32;i++) snprintf(out+i*2,3,"%02x",digest[i]);
    return true;
}
static bool equal_secret(const char *a,const char *b)
{
    if (!a || !b || strlen(a)!=64 || strlen(b)!=64) return false;
    unsigned diff=0; for (unsigned i=0;i<64;i++) diff|=(unsigned char)a[i]^(unsigned char)b[i];
    return diff==0;
}
static cJSON *status(void)
{
    runtime_config_t cfg; runtime_config_get(&cfg);
    char ip[16]; int reason=0; native_wifi_get_status(ip,&reason);
    uint8_t mac[6]; esp_read_mac(mac,ESP_MAC_WIFI_STA);
    char id[32]; snprintf(id,sizeof(id),"P300_%02X%02X%02X%02X%02X%02X",mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
    cJSON *out=cJSON_CreateObject();
    cJSON_AddBoolToObject(out,"ok",true);
    cJSON_AddStringToObject(out,"device_id",id);
    cJSON_AddStringToObject(out,"mode",p300_stream_wifi_mode()?"wifi":"serial");
    cJSON_AddBoolToObject(out,"ads_running",eeg_acq_is_running());
    const bool bias_verified=eeg_acq_normal_bias_is_verified();
    cJSON_AddBoolToObject(out,"bias_power_control",true);
    cJSON_AddNumberToObject(out,"config_id",eeg_acq_get_config_id());
    cJSON_AddBoolToObject(out,"bias_verified",bias_verified);
    cJSON_AddNumberToObject(out,"bias_mask",bias_verified?eeg_acq_get_normal_bias_mask():0);
    if (bias_verified) cJSON_AddBoolToObject(out,"bias_enabled",
        eeg_acq_get_config_id()==EEG_ACQ_CONFIG_NORMAL && eeg_acq_get_normal_bias_mask()!=0);
    cJSON_AddBoolToObject(out,"wifi_available",s_wifi_ready);
    cJSON_AddBoolToObject(out,"wifi_connected",native_wifi_is_connected());
    cJSON_AddStringToObject(out,"ssid",cfg.ssid);
    cJSON_AddStringToObject(out,"ip",ip);
    cJSON_AddNumberToObject(out,"wifi_reason",reason);
    cJSON_AddStringToObject(out,"tcp_host",cfg.host);
    cJSON_AddNumberToObject(out,"tcp_port",cfg.port);
    cJSON_AddBoolToObject(out,"paired",cfg.monitor_id[0]!=0);
    cJSON_AddBoolToObject(out,"time_ready",p300_stream_time_ready());
    cJSON_AddNumberToObject(out,"serial_drops",uart_link_drops());
    cJSON_AddNumberToObject(out,"serial_errors",uart_link_errors());
    memset(&cfg,0,sizeof(cfg)); return out;
}
static cJSON *error(const char *message)
{
    cJSON *out=cJSON_CreateObject(); cJSON_AddBoolToObject(out,"ok",false);
    cJSON_AddStringToObject(out,"error",message); return out;
}
static bool valid_host(const char *host)
{ struct in_addr a; return host && inet_pton(AF_INET,host,&a)==1 && a.s_addr!=0 && a.s_addr!=0xffffffffU; }
static cJSON *manage(cJSON *in)
{
    const char *op=string(in,"op");
    if (!op) return error("invalid_operation");
    if (!strcmp(op,"confirm")) { uart_link_confirm(); return status(); }
    if (!strcmp(op,"status")) return status();
    if (!strcmp(op,"stream_restart")) return p300_stream_restart()==ESP_OK?status():error("device_busy");
    if (!strcmp(op,"stream_pause")) { p300_stream_pause_serial(); return status(); }
    if (!strcmp(op,"unix_time")) {
        cJSON *epoch=cJSON_GetObjectItemCaseSensitive(in,"unix_us");
        cJSON *mono=cJSON_GetObjectItemCaseSensitive(in,"pc_monotonic_us");
        if (!cJSON_IsNumber(epoch)||!cJSON_IsNumber(mono) || epoch->valuedouble<0 || mono->valuedouble<0)
            return error("invalid_time");
        return p300_stream_set_unix_time((uint64_t)epoch->valuedouble,(uint64_t)mono->valuedouble)==ESP_OK?
            status():error("clock_unsynchronized");
    }
    runtime_config_t old,cfg; runtime_config_get(&old); cfg=old;
    cJSON *out=NULL; esp_err_t err=ESP_OK;
    if (!strcmp(op,"pair")) {
        const char *id=string(in,"monitor_id"), *key=string(in,"pair_key"); uint8_t decoded[32];
        bool valid=id && strlen(id)>0 && strlen(id)<=64 && hex_key(key,decoded);
        if (valid) for (const char *p=id;*p;p++) if (!isalnum((unsigned char)*p) && *p!='-' && *p!='_') valid=false;
        memset(decoded,0,sizeof(decoded));
        if (!valid) out=error("invalid_pairing");
        else {
            bool changed=strcmp(old.monitor_id,id) || strcmp(old.pair_key,key);
            if (changed && p300_stream_is_running() && p300_stream_set_mode(false)!=ESP_OK)
                out=error("device_busy");
            else {
                if (changed) { native_wifi_enable(false); cfg.wifi_mode=0; }
                strlcpy(cfg.monitor_id,id,sizeof(cfg.monitor_id)); strlcpy(cfg.pair_key,key,sizeof(cfg.pair_key));
            }
        }
    } else if (!strcmp(op,"endpoint")) {
        const char *host=string(in,"host"); cJSON *port=cJSON_GetObjectItemCaseSensitive(in,"port");
        if (!valid_host(host) || !cJSON_IsNumber(port) || port->valueint<1 || port->valueint>65535) out=error("invalid_endpoint");
        else if (p300_stream_is_running() && p300_stream_set_mode(false)!=ESP_OK) out=error("device_busy");
        else {
            native_wifi_enable(false); cfg.wifi_mode=0;
            strlcpy(cfg.host,host,sizeof(cfg.host)); cfg.port=port->valueint;
        }
    } else if (!strcmp(op,"mode")) {
        const char *mode=string(in,"mode");
        if (!mode || (strcmp(mode,"serial") && strcmp(mode,"wifi"))) out=error("invalid_mode");
        else if (!strcmp(mode,"wifi") && (!s_wifi_ready || !cfg.ssid[0] || !cfg.monitor_id[0])) out=error("wifi_not_configured_or_paired");
        else {
            bool wifi=!strcmp(mode,"wifi");
            err=p300_stream_set_mode(wifi);
            if (err==ESP_OK) err=native_wifi_enable(wifi);
            if (err==ESP_OK) cfg.wifi_mode=wifi;
            else { p300_stream_set_mode(old.wifi_mode!=0); out=error("mode_busy_or_unavailable"); }
        }
    } else if (!strcmp(op,"scan")) {
        char *networks=s_wifi_ready?native_wifi_scan_json():NULL;
        if (!networks) out=error("scan_unavailable");
        else { out=status(); cJSON_AddItemToObject(out,"networks",cJSON_Parse(networks)); free(networks); }
    } else if (!strcmp(op,"clear_network")) {
        if (p300_stream_is_running()) err=p300_stream_set_mode(false);
        if (err!=ESP_OK) out=error("device_busy");
        else {
            native_wifi_enable(false); native_wifi_configure("","");
            memset(cfg.ssid,0,sizeof(cfg.ssid)); memset(cfg.password,0,sizeof(cfg.password)); cfg.wifi_mode=0;
        }
    } else if (!strcmp(op,"set_network")) {
        const char *ssid=string(in,"ssid"), *password=string(in,"password"), *host=string(in,"host");
        cJSON *port=cJSON_GetObjectItemCaseSensitive(in,"port");
        if (!s_wifi_ready || !ssid || !password || strlen(ssid)==0 || strlen(ssid)>32 || strlen(password)>64 ||
            (host && !valid_host(host)) || (port && (!cJSON_IsNumber(port) || port->valueint<1 || port->valueint>65535)))
            out=error("invalid_network");
        else if (p300_stream_is_running() && p300_stream_set_mode(false)!=ESP_OK) out=error("device_busy");
        else {
            native_wifi_enable(false); vTaskDelay(pdMS_TO_TICKS(100));
            err=native_wifi_configure(ssid,password);
            if (err==ESP_OK) err=native_wifi_enable(true);
            for (int i=0;err==ESP_OK && i<200 && !native_wifi_is_connected();i++) vTaskDelay(pdMS_TO_TICKS(100));
            if (err!=ESP_OK || !native_wifi_is_connected()) {
                native_wifi_enable(false); native_wifi_configure(old.ssid,old.password);
                native_wifi_enable(old.wifi_mode!=0);
                if (p300_stream_is_running()) p300_stream_set_mode(old.wifi_mode!=0);
                out=error("wifi_connection_failed_old_config_preserved");
            } else {
                strlcpy(cfg.ssid,ssid,sizeof(cfg.ssid)); strlcpy(cfg.password,password,sizeof(cfg.password)); cfg.wifi_mode=0;
                if (host) strlcpy(cfg.host,host,sizeof(cfg.host));
                if (port) cfg.port=port->valueint;
                native_wifi_enable(false);
            }
        }
    } else out=error("unknown_operation");
    if (!out) {
        err=runtime_config_save(&cfg);
        if (err!=ESP_OK) {
            native_wifi_enable(false); native_wifi_configure(old.ssid,old.password);
            if (p300_stream_is_running()) p300_stream_set_mode(old.wifi_mode!=0);
            native_wifi_enable(old.wifi_mode!=0);
        }
        out=err==ESP_OK?status():error("config_save_failed");
        if (err==ESP_OK && !strcmp(op,"set_network")) cJSON_AddBoolToObject(out,"network_verified",true);
    }
    memset(&old,0,sizeof(old)); memset(&cfg,0,sizeof(cfg)); return out;
}
static void management_task(void *arg)
{
    (void)arg;
    while (true) {
        job_t *job; xQueueReceive(s_jobs,&job,portMAX_DELAY);
        uint32_t crc=serial_crc32c((const uint8_t *)job->json,job->size);
        char *reply=NULL; bool duplicate=false;
        for (unsigned i=0;i<8;i++) if (s_cache[i].id==job->id && s_cache[i].generation==job->generation && s_cache[i].reply) {
            duplicate=true;
            reply=s_cache[i].crc==crc?s_cache[i].reply:"{\"ok\":false,\"error\":\"request_id_reused\"}"; break;
        }
        if (!duplicate && job->generation==uart_link_generation()) {
            cJSON *in=cJSON_ParseWithLength(job->json,job->size), *out;
            out=cJSON_IsObject(in)?manage(in):error("invalid_json");
            cJSON_Delete(in); reply=cJSON_PrintUnformatted(out); cJSON_Delete(out);
            cached_t *cache=&s_cache[s_cache_next++%8]; free(cache->reply);
            *cache=(cached_t){.id=job->id,.generation=job->generation,.crc=crc,.reply=reply};
        }
        if (reply && job->generation==uart_link_generation()) uart_link_send(SERIAL_MANAGE_REPLY,job->id,reply,strlen(reply));
        memset(job,0,sizeof(*job)+job->size+1); free(job);
    }
}
static void receive(uint8_t type,uint32_t id,const uint8_t *data,size_t size,void *ctx)
{
    (void)ctx;
    if (type==SERIAL_CONTROL) { p300_stream_serial_control(data,size); return; }
    job_t *job=malloc(sizeof(*job)+size+1);
    if (!job) return;
    job->id=id; job->generation=uart_link_generation(); job->size=size;
    memcpy(job->json,data,size); job->json[size]=0;
    if (xQueueSend(s_jobs,&job,0)!=pdTRUE) {
        memset(job,0,sizeof(*job)+size+1); free(job);
        const char *reply="{\"ok\":false,\"error\":\"management_busy\"}";
        uart_link_send(SERIAL_MANAGE_REPLY,id,reply,strlen(reply));
    }
}
static void discovery_task(void *arg)
{
    (void)arg;
    int sock=socket(AF_INET,SOCK_DGRAM,IPPROTO_IP), enabled=1;
    if (sock<0) { vTaskDelete(NULL); return; }
    setsockopt(sock,SOL_SOCKET,SO_BROADCAST,&enabled,sizeof(enabled));
    struct timeval timeout={.tv_sec=0,.tv_usec=200000};
    setsockopt(sock,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
    while (true) {
        runtime_config_t cfg; runtime_config_get(&cfg);
        if (native_wifi_is_connected() && runtime_config_wifi_mode() && cfg.monitor_id[0] && cfg.pair_key[0]) {
            char nonce[33],sig[65],canonical[256],packet[512];
            uint8_t random[16]; esp_fill_random(random,sizeof(random));
            for (unsigned i=0;i<16;i++) snprintf(nonce+i*2,3,"%02x",random[i]);
            snprintf(canonical,sizeof(canonical),"P300_DISCOVER|1|%s|%s",cfg.monitor_id,nonce);
            if (hmac_hex(cfg.pair_key,canonical,sig)) {
                snprintf(packet,sizeof(packet),"{\"type\":\"P300_DISCOVER\",\"version\":1,\"monitor_id\":\"%s\",\"nonce\":\"%s\",\"signature\":\"%s\"}",cfg.monitor_id,nonce,sig);
                struct sockaddr_in dest={.sin_family=AF_INET,.sin_port=htons(P300_DISCOVERY_PORT),.sin_addr.s_addr=INADDR_BROADCAST};
                if (valid_host(cfg.host)) {
                    struct sockaddr_in direct=dest;
                    inet_pton(AF_INET,cfg.host,&direct.sin_addr);
                    sendto(sock,packet,strlen(packet),0,(struct sockaddr *)&direct,sizeof(direct));
                }
                sendto(sock,packet,strlen(packet),0,(struct sockaddr *)&dest,sizeof(dest));
                const int64_t deadline=esp_timer_get_time()+200000;
                while (esp_timer_get_time()<deadline) {
                    struct sockaddr_in source; socklen_t source_size=sizeof(source);
                    int n=recvfrom(sock,packet,sizeof(packet)-1,0,(struct sockaddr *)&source,&source_size);
                    if (n<=0) break;
                    packet[n]=0; cJSON *reply=cJSON_Parse(packet);
                    const char *type=string(reply,"type"), *id=string(reply,"monitor_id"), *rnonce=string(reply,"nonce"), *rsig=string(reply,"signature");
                    cJSON *port=cJSON_GetObjectItemCaseSensitive(reply,"port"), *version=cJSON_GetObjectItemCaseSensitive(reply,"version");
                    if (type && !strcmp(type,"P300_MONITOR") && id && !strcmp(id,cfg.monitor_id) && rnonce && !strcmp(rnonce,nonce) &&
                        cJSON_IsNumber(version) && version->valueint==1 && cJSON_IsNumber(port) && port->valueint>0 && port->valueint<=65535) {
                        snprintf(canonical,sizeof(canonical),"P300_MONITOR|1|%s|%s|%d",id,nonce,port->valueint);
                        if (hmac_hex(cfg.pair_key,canonical,sig) && equal_secret(sig,rsig)) {
                            char host[16]; inet_ntoa_r(source.sin_addr,host,sizeof(host));
                            runtime_config_t current; runtime_config_get(&current);
                            if (runtime_config_wifi_mode() && !strcmp(current.monitor_id,cfg.monitor_id) &&
                                !strcmp(current.pair_key,cfg.pair_key))
                                runtime_config_set_endpoint(host,(uint16_t)port->valueint);
                            memset(&current,0,sizeof(current));
                            cJSON_Delete(reply);
                            break;
                        }
                    }
                    cJSON_Delete(reply);
                }
            }
        }
        memset(&cfg,0,sizeof(cfg)); vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
esp_err_t device_manager_start(void)
{
    s_jobs=xQueueCreate(8,sizeof(job_t *));
    if (!s_jobs) return ESP_ERR_NO_MEM;
    esp_err_t err=uart_link_start(receive,NULL); if (err!=ESP_OK) return err;
    s_wifi_ready=native_wifi_init()==ESP_OK;
    if (xTaskCreate(management_task,"usb_manage",8192,NULL,7,NULL)!=pdPASS ||
        xTaskCreate(discovery_task,"monitor_discovery",6144,NULL,5,NULL)!=pdPASS) return ESP_ERR_NO_MEM;
    return ESP_OK;
}
