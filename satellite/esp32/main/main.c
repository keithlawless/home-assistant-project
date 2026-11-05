/*
 * Taco mic satellite for the Waveshare ESP32-S3-AUDIO-Board.
 *
 * Streams the board's two mics (mixed to mono, 16 kHz s16) to taco-voice
 * over TCP, using the same protocol as satellite/taco_satellite.py:
 *   "TACO <name> <token>\n", then raw little-endian samples, forever.
 * Wake word, VAD and STT all run on the kitchen Pi.
 *
 * Audio path: ES7210 ADC (I2C 0x40, MIC1/MIC2) -> I2S0 in standard stereo
 * mode with the ESP32 as clock master -> mic task (mix to mono) -> stream
 * buffer -> net task -> socket. The two tasks are decoupled so a slow or
 * dropped connection never stalls I2S; audio is simply dropped instead.
 *
 * Firmware updates (ota task): at boot and every OTA_CHECK_S the board asks
 * taco-voice, on the same port, "TACO-OTA <name> <token> <version>". If the
 * Pi has a different build it streams it, and the board checks its SHA-256,
 * writes the other app slot and restarts. A new image must stream to Taco
 * for STREAM_OK_S within OTA_CONFIRM_S of booting, or the board rolls back
 * to the old one and never takes that version again (a crash rolls back
 * too, via the bootloader).
 */

#include <string.h>
#include <sys/socket.h>
#include <netdb.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"
#include "driver/i2c_master.h"
#include "driver/i2s_std.h"
#include "lwip/sockets.h"
#include "esp_ota_ops.h"
#include "esp_app_desc.h"
#include "esp_timer.h"
#include "psa/crypto.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es7210_adc.h"

#include "secrets.h"

static const char *TAG = "taco-sat";

// Board pins (Waveshare ESP32-S3-AUDIO-Board v1.1 schematic).
#define PIN_I2C_SDA   11
#define PIN_I2C_SCL   10
#define PIN_I2S_MCLK  12
#define PIN_I2S_BCLK  13
#define PIN_I2S_WS    14
#define PIN_I2S_DIN   15   // ES7210 ASDOUT

#define SAMPLE_RATE   16000
#define FRAME_SAMPLES 1280                 // 80 ms, the frame size taco-voice uses
#define FRAME_BYTES   (FRAME_SAMPLES * 2)  // mono s16
#define STREAM_BYTES  (FRAME_BYTES * 8)    // ~0.6 s of slack for network hiccups

#define WIFI_UP BIT0

#define OTA_CHECK_S    (15 * 60)   // how often to ask Taco for new firmware
#define OTA_CONFIRM_S  300         // a new image must reach Taco within this
#define STREAM_OK_S    10          // ...and stream this long, or it rolls back

static bool s_pending_verify;      // running a new image that hasn't proved itself
static char s_bad_version[32];     // the image that last rolled back; never retaken

static EventGroupHandle_t s_wifi;
static StreamBufferHandle_t s_audio;

// -- Wi-Fi ----------------------------------------------------------------------

static void on_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && (id == WIFI_EVENT_STA_START || id == WIFI_EVENT_STA_DISCONNECTED)) {
        xEventGroupClearBits(s_wifi, WIFI_UP);
        esp_wifi_connect();   // retries forever; the driver paces attempts
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = data;
        ESP_LOGI(TAG, "wifi up, ip " IPSTR, IP2STR(&ev->ip_info.ip));
        xEventGroupSetBits(s_wifi, WIFI_UP);
    }
}

static void wifi_start(void)
{
    s_wifi = xEventGroupCreate();
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_t *netif = esp_netif_create_default_wifi_sta();
    esp_netif_set_hostname(netif, "taco-" SAT_NAME);

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, on_wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, on_wifi_event, NULL));

    wifi_config_t cfg = {0};
    strlcpy((char *)cfg.sta.ssid, WIFI_SSID, sizeof(cfg.sta.ssid));
    strlcpy((char *)cfg.sta.password, WIFI_PASSWORD, sizeof(cfg.sta.password));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    // Modem sleep adds latency and jitter to a constant audio stream.
    esp_wifi_set_ps(WIFI_PS_NONE);
}

// -- Mic ------------------------------------------------------------------------

static esp_codec_dev_handle_t mic_start(void)
{
    i2c_master_bus_handle_t i2c_bus;
    i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_cfg, &i2c_bus));

    // The ESP32 is the I2S master: the ES7210 (and the ES8311 sharing the
    // bus) take MCLK/BCLK/LRCK from it rather than making their own.
    i2s_chan_handle_t rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, NULL, &rx));
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK,
            .bclk = PIN_I2S_BCLK,
            .ws = PIN_I2S_WS,
            .dout = I2S_GPIO_UNUSED,
            .din = PIN_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));

    audio_codec_i2s_cfg_t i2s_if_cfg = {.port = I2S_NUM_0, .rx_handle = rx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    audio_codec_i2c_cfg_t ctrl_cfg = {
        .port = I2C_NUM_0,
        .addr = ES7210_CODEC_DEFAULT_ADDR,   // 0x40 as a 7-bit address
        .bus_handle = i2c_bus,
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&ctrl_cfg);
    // MIC1/MIC2 are the two physical mics; MIC3 is the speaker loopback
    // (an echo reference for AEC, unused while the satellite has no speaker).
    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = ctrl_if,
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2,
    };
    const audio_codec_if_t *codec_if = es7210_codec_new(&es7210_cfg);
    esp_codec_dev_cfg_t dev_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = codec_if,
        .data_if = data_if,
    };
    esp_codec_dev_handle_t mic = esp_codec_dev_new(&dev_cfg);
    if (!mic) {
        ESP_LOGE(TAG, "ES7210 not found; check the board");
        abort();
    }
    esp_codec_dev_sample_info_t fs = {
        .sample_rate = SAMPLE_RATE,
        .channel = 2,
        .bits_per_sample = 16,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(mic, &fs) == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL);
    esp_codec_dev_set_in_gain(mic, MIC_GAIN_DB);
    ESP_LOGI(TAG, "mic ready: %d Hz, gain %.1f dB", SAMPLE_RATE, MIC_GAIN_DB);
    return mic;
}

static void mic_task(void *arg)
{
    esp_codec_dev_handle_t mic = arg;
    static int16_t stereo[FRAME_SAMPLES * 2];
    static int16_t mono[FRAME_SAMPLES];
    while (true) {
        if (esp_codec_dev_read(mic, stereo, sizeof(stereo)) != ESP_CODEC_DEV_OK) {
            ESP_LOGW(TAG, "mic read failed");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        for (int i = 0; i < FRAME_SAMPLES; i++) {
            mono[i] = (int16_t)(((int32_t)stereo[2 * i] + stereo[2 * i + 1]) / 2);
        }
        // Never block: if the network is behind, drop this frame.
        if (xStreamBufferSpacesAvailable(s_audio) >= FRAME_BYTES) {
            xStreamBufferSend(s_audio, mono, FRAME_BYTES, 0);
        }
    }
}

// -- Network --------------------------------------------------------------------

static int tcp_open(void)
{
    struct addrinfo hints = {.ai_family = AF_INET, .ai_socktype = SOCK_STREAM};
    struct addrinfo *res = NULL;
    char port[8];
    snprintf(port, sizeof(port), "%d", TACO_PORT);
    if (getaddrinfo(TACO_HOST, port, &hints, &res) != 0 || !res) {
        ESP_LOGW(TAG, "can't resolve %s", TACO_HOST);
        return -1;
    }
    int sock = socket(res->ai_family, res->ai_socktype, 0);
    if (sock >= 0 && connect(sock, res->ai_addr, res->ai_addrlen) != 0) {
        close(sock);
        sock = -1;
    }
    freeaddrinfo(res);
    if (sock < 0) {
        return -1;
    }
    int on = 1;
    setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &on, sizeof(on));
    setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
    // If Taco stops reading, fail the send rather than hang forever.
    struct timeval tv = {.tv_sec = 5};
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    return sock;
}

static int taco_connect(void)
{
    int sock = tcp_open();
    const char *hello = "TACO " SAT_NAME " " SAT_TOKEN "\n";
    if (sock >= 0 && send(sock, hello, strlen(hello), 0) < 0) {
        close(sock);
        return -1;
    }
    return sock;
}

static bool send_all(int sock, const uint8_t *buf, size_t len)
{
    while (len) {
        int n = send(sock, buf, len, 0);
        if (n <= 0) {
            return false;
        }
        buf += n;
        len -= n;
    }
    return true;
}

static void net_task(void *arg)
{
    static uint8_t frame[FRAME_BYTES];
    int backoff_ms = 1000;
    while (true) {
        xEventGroupWaitBits(s_wifi, WIFI_UP, pdFALSE, pdTRUE, portMAX_DELAY);
        int sock = taco_connect();
        if (sock < 0) {
            ESP_LOGW(TAG, "can't reach Taco at %s:%d; retrying in %d s", TACO_HOST, TACO_PORT, backoff_ms / 1000);
            vTaskDelay(pdMS_TO_TICKS(backoff_ms));
            backoff_ms = backoff_ms * 2 > 30000 ? 30000 : backoff_ms * 2;
            continue;
        }
        ESP_LOGI(TAG, "streaming to %s:%d", TACO_HOST, TACO_PORT);
        backoff_ms = 1000;
        xStreamBufferReset(s_audio);   // start live, not with stale audio
        int64_t since = esp_timer_get_time();
        while (true) {
            if (s_pending_verify && esp_timer_get_time() - since > STREAM_OK_S * 1000000LL) {
                esp_ota_mark_app_valid_cancel_rollback();
                s_pending_verify = false;
                ESP_LOGI(TAG, "firmware %s confirmed", esp_app_get_description()->version);
            }
            size_t got = xStreamBufferReceive(s_audio, frame, FRAME_BYTES, pdMS_TO_TICKS(1000));
            if (got == 0) {
                continue;
            }
            if (!send_all(sock, frame, got)) {
                ESP_LOGW(TAG, "connection lost");
                break;
            }
        }
        close(sock);
    }
}

// -- Firmware updates -------------------------------------------------------------

static bool recv_line(int sock, char *line, size_t size)
{
    size_t n = 0;
    while (n + 1 < size) {
        if (recv(sock, &line[n], 1, 0) != 1) {
            return false;
        }
        if (line[n] == '\n') {
            line[n] = 0;
            return true;
        }
        n++;
    }
    return false;
}

static void ota_check(void)
{
    int sock = tcp_open();
    if (sock < 0) {
        return;
    }
    struct timeval tv = {.tv_sec = 30};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    char line[192];
    snprintf(line, sizeof(line), "TACO-OTA %s %s %s\n", SAT_NAME, SAT_TOKEN,
             esp_app_get_description()->version);
    unsigned size = 0;
    char want[65] = {0}, version[32] = {0};
    if (!send_all(sock, (const uint8_t *)line, strlen(line)) || !recv_line(sock, line, sizeof(line))
        || sscanf(line, "IMAGE %u %64s %31s", &size, want, version) != 3) {
        close(sock);   // "NONE", or Taco too old to know the question
        return;
    }
    if (strcmp(version, s_bad_version) == 0) {
        ESP_LOGW(TAG, "firmware %s rolled back before; not taking it again", version);
        close(sock);
        return;
    }
    const esp_partition_t *slot = esp_ota_get_next_update_partition(NULL);
    esp_ota_handle_t ota = 0;
    if (!slot || size > slot->size || esp_ota_begin(slot, size, &ota) != ESP_OK) {
        ESP_LOGE(TAG, "can't take firmware %s (%u bytes)", version, size);
        close(sock);
        return;
    }
    ESP_LOGI(TAG, "updating to firmware %s (%u bytes)", version, size);
    psa_crypto_init();   // a no-op once done
    psa_hash_operation_t sha = PSA_HASH_OPERATION_INIT;
    psa_hash_setup(&sha, PSA_ALG_SHA_256);
    static uint8_t buf[4096];
    unsigned left = size;
    while (left) {
        int n = recv(sock, buf, left < sizeof(buf) ? left : sizeof(buf), 0);
        if (n <= 0 || esp_ota_write(ota, buf, n) != ESP_OK) {
            break;
        }
        psa_hash_update(&sha, buf, n);
        left -= n;
    }
    close(sock);
    uint8_t digest[32];
    size_t digest_len = 0;
    char got[65] = {0};
    if (psa_hash_finish(&sha, digest, sizeof(digest), &digest_len) != PSA_SUCCESS) {
        psa_hash_abort(&sha);
    }
    for (int i = 0; i < 32; i++) {
        snprintf(&got[2 * i], 3, "%02x", digest[i]);
    }
    if (left || strcmp(got, want) != 0) {
        ESP_LOGE(TAG, "firmware %s download %s; keeping %s", version, left ? "cut short" : "corrupt",
                 esp_app_get_description()->version);
        esp_ota_abort(ota);
        return;
    }
    // esp_ota_end checks the image itself (format, checksum) before it counts.
    if (esp_ota_end(ota) != ESP_OK || esp_ota_set_boot_partition(slot) != ESP_OK) {
        ESP_LOGE(TAG, "firmware %s isn't a valid image", version);
        return;
    }
    ESP_LOGI(TAG, "firmware %s written; restarting", version);
    vTaskDelay(pdMS_TO_TICKS(200));
    esp_restart();
}

static void ota_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(20000));   // let the stream start first
    while (true) {
        if (s_pending_verify) {
            if (esp_timer_get_time() > OTA_CONFIRM_S * 1000000LL) {
                ESP_LOGE(TAG, "firmware %s never reached Taco; rolling back",
                         esp_app_get_description()->version);
                esp_ota_mark_app_invalid_rollback_and_reboot();
            }
            vTaskDelay(pdMS_TO_TICKS(5000));
            continue;   // no updates until this image has proved itself
        }
        xEventGroupWaitBits(s_wifi, WIFI_UP, pdFALSE, pdTRUE, portMAX_DELAY);
        ota_check();
        vTaskDelay(pdMS_TO_TICKS(OTA_CHECK_S * 1000));
    }
}

static void ota_boot_state(void)
{
    esp_ota_img_states_t state;
    const esp_partition_t *running = esp_ota_get_running_partition();
    s_pending_verify = esp_ota_get_state_partition(running, &state) == ESP_OK
                       && state == ESP_OTA_IMG_PENDING_VERIFY;
    const esp_partition_t *bad = esp_ota_get_last_invalid_partition();
    esp_app_desc_t desc;
    if (bad && esp_ota_get_partition_description(bad, &desc) == ESP_OK) {
        strlcpy(s_bad_version, desc.version, sizeof(s_bad_version));
    }
    ESP_LOGI(TAG, "firmware %s in %s%s%s%s", esp_app_get_description()->version, running->label,
             s_pending_verify ? " (new, on trial)" : "",
             s_bad_version[0] ? "; rolled back from " : "", s_bad_version);
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();   // Wi-Fi keeps calibration data in NVS
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    ota_boot_state();
    s_audio = xStreamBufferCreate(STREAM_BYTES, FRAME_BYTES);
    esp_codec_dev_handle_t mic = mic_start();
    wifi_start();
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, mic, 6, NULL, 1);
    xTaskCreatePinnedToCore(net_task, "net", 4096, NULL, 5, NULL, 0);
    xTaskCreatePinnedToCore(ota_task, "ota", 6144, NULL, 3, NULL, 0);
}
