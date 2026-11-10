/*
 * Taco mic satellite for the Waveshare ESP32-S3-AUDIO-Board.
 *
 * Streams the board's two mics to taco-voice over TCP (16 kHz s16), using
 * the protocol of satellite/taco_satellite.py plus two handshake options:
 *   "TACO <name> <token> afe=<on|off|failed> ch=<1|2>\n", then raw
 *   little-endian samples, forever.
 * With the speech front end off (ch=1) that's the two mics mixed to mono.
 * With it on (ch=2) it's ESP-SR's AFE source separation (BSS): two
 * channels, interleaved, each holding mostly one sound source (say, a
 * speaker and the TV). taco-voice runs its wake word on both and takes the
 * request from whichever fired. Wake word, VAD and STT all run on the Pi.
 *
 * Audio path: ES7210 ADC (I2C 0x40, all four inputs) -> I2S0 in 32-bit
 * stereo mode, which packs four 16-bit channels per frame (Waveshare's
 * layout: 0 = speaker reference, 1 = mic, 2 = unused, 3 = mic), with the
 * ESP32 as clock master -> mic task (decimate 48 -> 16 kHz, mix the two
 * mics to mono) -> stream buffer -> net task -> socket. The two tasks are
 * decoupled so a slow or dropped connection never stalls I2S; audio is
 * simply dropped instead.
 *
 * The I2S bus runs at 48 kHz so the speaker (ES8311, mono) can play music
 * at full bandwidth; the mics share its clocks, so the mic task low-passes
 * and decimates them 3:1 to the 16 kHz everything downstream expects.
 *
 * Light and mute (ring task): the 7-LED ring shows Taco's state during a
 * turn that started in this room (listening, thinking, speaking) and stays
 * dark otherwise; dim red means the mic is muted. The middle key (K2)
 * mutes this room's mic. That mute lives on the board (kept in NVS), so it
 * holds through a reboot or with the Pi down, and is reported to taco-voice
 * as "TACO-EVENT <name> <token> MUTE on|off" on a short connection of its own.
 *
 * Control lines from taco-voice (net task, read between frames):
 *   STATE <idle|listening|thinking|speaking> [here|elsewhere]   for the light
 *   MUTE on|off      the kiosk's "stop listening" (house-wide): send silence
 *   UPDATE           check for new firmware now
 *   AFE on|off       switch the speech front end (kept in NVS; restarts,
 *                    but never while a new image is still on trial)
 *   AUDIO <n>        then n bytes of mono s16le at the speaker rate (the
 *                    handshake's spk=<rate>): Taco's reply, for the board's
 *                    speaker (ES8311). Taco paces it in real time.
 *   MUSIC <n>        the same, for music: its own buffer, mixed with replies
 *   STOP             drop reply audio that hasn't played yet
 *   MSTOP            drop music that hasn't played yet
 *   VOLUME <0-100>   speaker volume
 * A receive task reads these as they arrive, so a music stream never waits
 * on the mic uplink.
 *
 * Firmware updates (ota task): at boot and every OTA_CHECK_S the board asks
 * taco-voice, on the same port, "TACO-OTA <name> <token> <version>". If the
 * Pi has a different build it streams it, and the board checks its SHA-256,
 * writes the other app slot and restarts. A new image must stream to Taco
 * for STREAM_OK_S within OTA_CONFIRM_S of booting, or the board rolls back
 * to the old one and never takes that version again (a crash rolls back
 * too, via the bootloader).
 */

#include <errno.h>
#include <math.h>
#include <string.h>
#include <sys/socket.h>
#include <netdb.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/stream_buffer.h"
#include "esp_log.h"
#include "esp_system.h"
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
#include "nvs.h"
#include "led_strip.h"
#include "esp_io_expander_tca95xx_16bit.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es7210_adc.h"
#include "esp_afe_sr_models.h"
#include "esp_afe_config.h"
#include "esp_psram.h"

// Each board's Wi-Fi, name, port and token: secrets.h, or secrets.<board>.h
// when built with TACO_SAT=<board> (deploy/satellite-ota.sh does this).
#ifdef TACO_SECRETS
#include TACO_SECRETS
#else
#include "secrets.h"
#endif

static const char *TAG = "taco-sat";

// Board pins (Waveshare ESP32-S3-AUDIO-Board v1.1 schematic).
#define PIN_I2C_SDA   11
#define PIN_I2C_SCL   10
#define PIN_I2S_MCLK  12
#define PIN_I2S_BCLK  13
#define PIN_I2S_WS    14
#define PIN_I2S_DIN   15   // ES7210 ASDOUT
#define PIN_I2S_DOUT  16   // ES8311 DSDIN (the speaker)
#define PIN_LED_RING  38   // 7 WS2812s (RGB order, per Waveshare's demo)
#define RING_LEDS     7
// The TCA9555 I/O expander (I2C 0x20) has the three user keys, pressed = low:
// K1 = P9, K2 = P10, K3 = P11. P8 enables the speaker amp, active high.
#define KEY_MUTE      IO_EXPANDER_PIN_NUM_10   // K2, the middle key
#define AMP_ENABLE    IO_EXPANDER_PIN_NUM_8
#define RING_MAX      48   // brightness cap, 0-255: this sits in a living room

#define SAMPLE_RATE   16000                // the mics, as streamed to Taco
#define I2S_RATE      48000                // the bus, and the speaker
#define DECIM         (I2S_RATE / SAMPLE_RATE)
#define FIR_TAPS      127                  // decimation low-pass (odd, symmetric)
#define FIR_CUTOFF_HZ 7200                 // -6 dB point; 8 kHz is the new Nyquist
#ifndef SPEAKER_AEC
#define SPEAKER_AEC   1                    // echo-cancel the speaker (front end on)
#endif
#define ADC_CHANNELS  4                    // per frame, see the audio path above
#define MIC_A         1                    // the two mic channels
#define MIC_B         3
// Every LEVELS_EVERY_S, report each channel's level to taco-voice (rms and
// peak dBFS), to check the channel layout. 0 = off.
#define LEVELS_EVERY_S 60
#define FRAME_SAMPLES 1280                 // 80 ms, the frame size taco-voice uses
#define FRAME_BYTES   (FRAME_SAMPLES * 2)  // one channel, s16
#define OUT_CH_MAX    2                    // channels streamed with the front end on
#define STREAM_BYTES  (FRAME_BYTES * OUT_CH_MAX * 8)   // ~0.6 s of slack for network hiccups
// How the AFE lays out its multi-channel raw_data: 1 = interleaved (L R L R),
// 0 = one channel after the other. Not documented; checked on the board.
#define AFE_RAW_INTERLEAVED 1

#define WIFI_UP BIT0

#define OTA_CHECK_S    (15 * 60)   // how often to ask Taco for new firmware
#define OTA_CONFIRM_S  300         // a new image must reach Taco within this
#define STREAM_OK_S    10          // ...and stream this long, or it rolls back

// What taco-voice last said (control lines); the ring shows these.
static volatile bool s_house_muted;       // the kiosk's "stop listening"
static volatile bool s_button_muted;      // K2 on this board, kept in NVS
#define MUTED() (s_house_muted || s_button_muted)   // send silence
static TaskHandle_t s_event_task;
#define EVENT_MUTE    BIT0             // event task notification bits
#define EVENT_LEVELS  BIT1
static i2c_master_bus_handle_t s_i2c;
static char s_state[16] = "idle";
static volatile bool s_here;              // this room has the current turn
static TaskHandle_t s_ota_task;

// The speech front end (ESP-SR AFE). s_afe_want is kept in NVS; the AFE
// starts only at boot, so switching it means a restart.
static bool s_afe_want;
static volatile bool s_afe_restart;        // switched; restart once it's safe
static const char *s_afe_status = "off";   // for the handshake: on, off, failed
static int s_out_ch = 1;                   // channels in each streamed frame
static const esp_afe_sr_iface_t *s_afe_if;
static esp_afe_sr_data_t *s_afe;
static int s_feed_chunk;                   // samples per channel per feed()

// The speaker: reply audio from Taco (AUDIO lines) -> s_play -> speaker task
// -> ES8311. The amp is switched by the ring task, which owns the expander.
#define PLAY_BYTES    (I2S_RATE * 2 * 4)      // 4 s of mono s16 replies, in PSRAM
#define MUSIC_BYTES   (I2S_RATE * 2 * 2)      // 2 s of music
#define PLAY_SAMPLES  (I2S_RATE / 50)         // 20 ms per write
#define SPEAKER_VOLUME_DEFAULT 70
static esp_codec_dev_handle_t s_spk;
static StreamBufferHandle_t s_play;
static StreamBufferHandle_t s_music;
static volatile bool s_play_stop;          // STOP: drop the queued reply
static volatile bool s_music_stop;         // MSTOP: drop the queued music
static volatile bool s_amp_on;             // wanted state of the amp
static volatile bool s_mic_alive;          // the mics have shown real signal

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
    i2c_master_bus_config_t i2c_cfg = {
        .i2c_port = I2C_NUM_0,
        .sda_io_num = PIN_I2C_SDA,
        .scl_io_num = PIN_I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_cfg, &s_i2c));

    // The ESP32 is the I2S master: the ES7210 (and the ES8311 sharing the
    // bus) take MCLK/BCLK/LRCK from it rather than making their own.
    // Full duplex: the speaker's TX shares the mics' clocks and frame format.
    i2s_chan_handle_t tx, rx;
    i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    chan_cfg.auto_clear_after_cb = true;   // silence when there's nothing to play
    // 8 x 5 ms of DMA: the mic task decimates a frame (a few ms) between reads.
    // Internal RAM is tight (Wi-Fi needs its share), so no more than that.
    chan_cfg.dma_desc_num = 8;
    chan_cfg.dma_frame_num = I2S_RATE / 200;
    ESP_ERROR_CHECK(i2s_new_channel(&chan_cfg, &tx, &rx));
    i2s_std_config_t std_cfg = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(I2S_RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_32BIT, I2S_SLOT_MODE_STEREO),
        .gpio_cfg = {
            .mclk = PIN_I2S_MCLK,
            .bclk = PIN_I2S_BCLK,
            .ws = PIN_I2S_WS,
            .dout = PIN_I2S_DOUT,
            .din = PIN_I2S_DIN,
        },
    };
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_init_std_mode(rx, &std_cfg));
    ESP_ERROR_CHECK(i2s_channel_enable(tx));
    ESP_ERROR_CHECK(i2s_channel_enable(rx));

    audio_codec_i2s_cfg_t i2s_if_cfg = {.port = I2S_NUM_0, .rx_handle = rx, .tx_handle = tx};
    const audio_codec_data_if_t *data_if = audio_codec_new_i2s_data(&i2s_if_cfg);
    audio_codec_i2c_cfg_t ctrl_cfg = {
        .port = I2C_NUM_0,
        .addr = ES7210_CODEC_DEFAULT_ADDR,   // 0x40 as a 7-bit address
        .bus_handle = s_i2c,
    };
    const audio_codec_ctrl_if_t *ctrl_if = audio_codec_new_i2c_ctrl(&ctrl_cfg);
    // All four inputs, as Waveshare's speech demo does: two mics, the
    // speaker loopback (an echo reference for later) and one unused.
    es7210_codec_cfg_t es7210_cfg = {
        .ctrl_if = ctrl_if,
        .mic_selected = ES7210_SEL_MIC1 | ES7210_SEL_MIC2 | ES7210_SEL_MIC3 | ES7210_SEL_MIC4,
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
        .sample_rate = I2S_RATE,
        .channel = 2,           // two 32-bit slots = four 16-bit channels
        .bits_per_sample = 32,
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(mic, &fs) == ESP_CODEC_DEV_OK ? ESP_OK : ESP_FAIL);
    esp_codec_dev_set_in_gain(mic, MIC_GAIN_DB);
    ESP_LOGI(TAG, "mic ready: %d Hz bus, %d Hz out, gain %.1f dB", I2S_RATE, SAMPLE_RATE, MIC_GAIN_DB);

    // The speaker DAC, opened with exactly the mics' format: the two share
    // one I2S controller, and a different format would reclock the mics.
    ctrl_cfg.addr = ES8311_CODEC_DEFAULT_ADDR;
    es8311_codec_cfg_t es8311_cfg = {
        .ctrl_if = audio_codec_new_i2c_ctrl(&ctrl_cfg),
        .gpio_if = audio_codec_new_gpio(),
        .codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin = -1,            // the amp is on the I/O expander (AMP_ENABLE)
        .use_mclk = true,
        .hw_gain = {.pa_voltage = 5.0, .codec_dac_voltage = 3.3},
    };
    esp_codec_dev_cfg_t spk_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = es8311_codec_new(&es8311_cfg),
        .data_if = data_if,
    };
    s_spk = spk_cfg.codec_if ? esp_codec_dev_new(&spk_cfg) : NULL;
    if (s_spk && esp_codec_dev_open(s_spk, &fs) == ESP_CODEC_DEV_OK) {
        esp_codec_dev_set_out_vol(s_spk, SPEAKER_VOLUME_DEFAULT);
        ESP_LOGI(TAG, "speaker ready");
    } else {
        ESP_LOGE(TAG, "ES8311 not found; no replies on this board");
        s_spk = NULL;
    }
    return mic;
}

// Per-channel levels for the LEVELS report.
static char s_levels[200];
// Load counters, reported with the levels (frames are 80 ms; µs are totals).
static volatile uint32_t s_n_afe_out, s_n_sent, s_n_dropped;
static int64_t s_us_decim, s_us_feed;

// -- Speech front end -------------------------------------------------------------

static bool afe_start(void)
{
    // The ES7210's four channels, in Waveshare's layout: R = speaker
    // reference, M = mic, N = unused.
    afe_config_t *cfg = afe_config_init("RMNM", NULL, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    if (!cfg) {
        return false;
    }
    cfg->aec_init = SPEAKER_AEC && s_spk != NULL;   // cancel the board's own speaker (reference = R)
    cfg->se_init = true;         // BSS: split the two mics into separate sources
    cfg->ns_init = false;
    cfg->vad_init = false;       // taco-voice does VAD,
    cfg->wakenet_init = false;   // and the wake word
    cfg->agc_init = false;       // kitchen priority compares raw levels
    cfg->memory_alloc_mode = AFE_MEMORY_ALLOC_MORE_PSRAM;
    cfg->afe_perferred_core = 1;
    cfg->afe_perferred_priority = 5;
    s_afe_if = esp_afe_handle_from_config(cfg);
    s_afe = s_afe_if ? s_afe_if->create_from_config(cfg) : NULL;
    afe_config_free(cfg);
    if (!s_afe) {
        return false;
    }
    s_feed_chunk = s_afe_if->get_feed_chunksize(s_afe);
    ESP_LOGI(TAG, "speech front end on: feed %d x %d ch, fetch %d", s_feed_chunk,
             s_afe_if->get_feed_channel_num(s_afe), s_afe_if->get_fetch_chunksize(s_afe));
    s_afe_if->print_pipeline(s_afe);
    return true;
}

// Feed one 80 ms frame of all four ADC channels to the AFE, in its chunk size.
static void afe_feed(const int16_t *quad)
{
    static int16_t *buf;
    static int fill;   // samples per channel in buf
    if (!buf) {
        buf = heap_caps_malloc(s_feed_chunk * ADC_CHANNELS * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    }
    for (int i = 0; i < FRAME_SAMPLES;) {
        int n = s_feed_chunk - fill < FRAME_SAMPLES - i ? s_feed_chunk - fill : FRAME_SAMPLES - i;
        memcpy(&buf[fill * ADC_CHANNELS], &quad[i * ADC_CHANNELS], n * ADC_CHANNELS * sizeof(int16_t));
        fill += n;
        i += n;
        if (fill == s_feed_chunk) {
            s_afe_if->feed(s_afe, buf);
            fill = 0;
        }
    }
}

// Collect the AFE's two separated channels into 80 ms interleaved frames.
static void afe_task(void *arg)
{
    static int16_t out[FRAME_SAMPLES * OUT_CH_MAX];
    int fill = 0;
    bool told = false;
    while (true) {
        afe_fetch_result_t *res = s_afe_if->fetch(s_afe);
        if (!res || res->ret_value == ESP_FAIL || !res->data) {
            continue;
        }
        int n = res->data_size / sizeof(int16_t);
        int ch = res->raw_data ? res->raw_data_channels : 0;
        if (!told) {
            ESP_LOGI(TAG, "front end output: %d samples, %d raw channels", n, ch);
            told = true;
        }
        for (int i = 0; i < n; i++) {
            int16_t a = res->data[i], b = a;
            if (ch >= 2) {
#if AFE_RAW_INTERLEAVED
                a = res->raw_data[i * ch];
                b = res->raw_data[i * ch + 1];
#else
                a = res->raw_data[i];
                b = res->raw_data[n + i];
#endif
            }
            out[2 * fill] = MUTED() ? 0 : a;
            out[2 * fill + 1] = MUTED() ? 0 : b;
            if (++fill == FRAME_SAMPLES) {
                fill = 0;
                s_n_afe_out++;
                if (xStreamBufferSpacesAvailable(s_audio) >= sizeof(out)) {
                    xStreamBufferSend(s_audio, out, sizeof(out), 0);
                } else {
                    s_n_dropped++;
                }
            }
        }
    }
}

// -- Speaker --------------------------------------------------------------------------

// Up to one 20 ms write's worth of samples from a play buffer, never waiting.
// The receive task only ever queues whole samples.
static int take(StreamBufferHandle_t sb, volatile bool *stop, int16_t *dst)
{
    if (*stop) {
        while (xStreamBufferReceive(sb, dst, PLAY_SAMPLES * 2, 0)) {
        }
        *stop = false;
        return 0;
    }
    return xStreamBufferReceive(sb, dst, PLAY_SAMPLES * 2, 0) / 2;
}

// Mix replies and music into the speaker. The I2S write blocks on DMA,
// which paces the loop; Taco keeps both buffers a little ahead.
static void speaker_task(void *arg)
{
    static int16_t reply[PLAY_SAMPLES], music[PLAY_SAMPLES];
    static int32_t slots[PLAY_SAMPLES * 2];   // the I2S frame: two 32-bit slots
    int64_t last_audio = 0;
    while (true) {
        int r = take(s_play, &s_play_stop, reply);
        int m = take(s_music, &s_music_stop, music);
        int n = r > m ? r : m;
        if (n == 0) {
            if (s_amp_on && esp_timer_get_time() - last_audio > 300000) {
                s_amp_on = false;            // no hiss between replies
            }
            vTaskDelay(pdMS_TO_TICKS(10));
            continue;
        }
        if (!s_amp_on) {
            s_amp_on = true;
            vTaskDelay(pdMS_TO_TICKS(80));   // the ring task switches it within 40 ms
        }
        for (int i = 0; i < n; i++) {
            int32_t v = (i < r ? reply[i] : 0) + (i < m ? music[i] : 0);
            v = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
            slots[2 * i] = slots[2 * i + 1] = v << 16;
        }
        esp_codec_dev_write(s_spk, slots, n * 2 * sizeof(int32_t));
        last_audio = esp_timer_get_time();
    }
}

// -- Mic task -----------------------------------------------------------------------

// The 3:1 decimator: a Kaiser-windowed sinc low-pass, run only at the
// output rate, on the three channels in use (the reference and both mics).
static float s_fir[FIR_TAPS];
static const int DECIM_CH[] = {0, MIC_A, MIC_B};
#define DECIM_NCH (sizeof(DECIM_CH) / sizeof(DECIM_CH[0]))
#define HIST      (FIR_TAPS - 1)
#define BUS_SAMPLES (FRAME_SAMPLES * DECIM)

static double bessel_i0(double x)
{
    double sum = 1, term = 1;
    for (int k = 1; k < 30; k++) {
        term *= (x / (2 * k)) * (x / (2 * k));
        sum += term;
    }
    return sum;
}

static void fir_init(void)
{
    const double fc = (double)FIR_CUTOFF_HZ / I2S_RATE, beta = 7.0;
    const int mid = HIST / 2;
    double sum = 0;
    for (int n = 0; n < FIR_TAPS; n++) {
        int x = n - mid;
        double h = x == 0 ? 2 * fc : sin(2 * M_PI * fc * x) / (M_PI * x);
        double r = (double)x / mid;
        h *= bessel_i0(beta * sqrt(1 - r * r)) / bessel_i0(beta);
        s_fir[n] = h;
        sum += h;
    }
    for (int n = 0; n < FIR_TAPS; n++) {
        s_fir[n] /= sum;   // unity gain at DC
    }
}

// 80 ms of the 48 kHz bus (four channels) -> 80 ms at 16 kHz, same layout.
// Works in small blocks so the float history stays small: internal RAM is
// needed by Wi-Fi, and the bus frame itself is in PSRAM.
#define DECIM_BLOCK 128   // output samples per block
static void decimate(const int16_t *bus, int16_t *quad)
{
    static float x[DECIM_NCH][HIST + DECIM_BLOCK * DECIM];   // history, then a block
    const int mid = HIST / 2;
    for (int c = 0; c < DECIM_NCH; c++) {
        float *xc = x[c];
        const int ch = DECIM_CH[c];
        for (int o = 0; o < FRAME_SAMPLES; o += DECIM_BLOCK) {
            int outs = FRAME_SAMPLES - o < DECIM_BLOCK ? FRAME_SAMPLES - o : DECIM_BLOCK;
            int ins = outs * DECIM;
            for (int i = 0; i < ins; i++) {
                xc[HIST + i] = bus[(o * DECIM + i) * ADC_CHANNELS + ch];
            }
            for (int i = 0; i < outs; i++) {
                const float *w = &xc[i * DECIM];
                float y = s_fir[mid] * w[mid];
                for (int k = 0; k < mid; k++) {
                    y += s_fir[k] * (w[k] + w[HIST - k]);   // symmetric taps
                }
                int v = lrintf(y);
                quad[(o + i) * ADC_CHANNELS + ch] = v > 32767 ? 32767 : v < -32768 ? -32768 : v;
            }
            memmove(xc, &xc[ins], HIST * sizeof(float));
        }
    }
    for (int i = 0; i < FRAME_SAMPLES; i++) {
        quad[i * ADC_CHANNELS + 2] = 0;   // the unused input
    }
}

// The I2S task only drains the DMA ring (40 ms) into s_bus (PSRAM, 0.5 s),
// so the mic task's processing (decimation, the front end's AEC) can run
// late now and then without losing audio.
#define BUS_FRAME_BYTES (BUS_SAMPLES * ADC_CHANNELS * sizeof(int16_t))   // 80 ms of the bus
#define I2S_CHUNK_BYTES (BUS_FRAME_BYTES / 4)                           // 20 ms
static StreamBufferHandle_t s_bus;

static void i2s_task(void *arg)
{
    esp_codec_dev_handle_t mic = arg;
    uint8_t *chunk = heap_caps_malloc(I2S_CHUNK_BYTES, MALLOC_CAP_SPIRAM);
    while (true) {
        if (esp_codec_dev_read(mic, chunk, I2S_CHUNK_BYTES) != ESP_CODEC_DEV_OK) {
            ESP_LOGW(TAG, "mic read failed");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        if (xStreamBufferSend(s_bus, chunk, I2S_CHUNK_BYTES, 0) != I2S_CHUNK_BYTES) {
            s_n_dropped++;   // the mic task is half a second behind
        }
    }
}

static void mic_task(void *arg)
{
    int16_t *bus = heap_caps_malloc(BUS_FRAME_BYTES, MALLOC_CAP_SPIRAM);
    static int16_t quad[FRAME_SAMPLES * ADC_CHANNELS];
    static int16_t mono[FRAME_SAMPLES];
    double sumsq[ADC_CHANNELS] = {0};
    int peak[ADC_CHANNELS] = {0};
    int frames = 0;
    fir_init();
    while (true) {
        for (size_t got = 0; got < BUS_FRAME_BYTES;) {
            got += xStreamBufferReceive(s_bus, (uint8_t *)bus + got, BUS_FRAME_BYTES - got, portMAX_DELAY);
        }
        int64_t t0 = esp_timer_get_time();
        decimate(bus, quad);
        s_us_decim += esp_timer_get_time() - t0;
        for (int i = 0; i < FRAME_SAMPLES; i++) {
            const int16_t *f = &quad[ADC_CHANNELS * i];
            mono[i] = MUTED() ? 0 : (int16_t)(((int32_t)f[MIC_A] + f[MIC_B]) / 2);
            if (!s_mic_alive && (f[MIC_A] > 2 || f[MIC_A] < -2)) {
                s_mic_alive = true;   // about -84 dBFS: a working mic, not zeros
            }
            for (int c = 0; c < ADC_CHANNELS && LEVELS_EVERY_S; c++) {
                sumsq[c] += (double)f[c] * f[c];
                int a = f[c] < 0 ? -f[c] : f[c];
                peak[c] = a > peak[c] ? a : peak[c];
            }
        }
        if (LEVELS_EVERY_S && ++frames >= LEVELS_EVERY_S * SAMPLE_RATE / FRAME_SAMPLES) {
            int n = 0;
            for (int c = 0; c < ADC_CHANNELS; c++) {
                double rms = sqrt(sumsq[c] / (frames * FRAME_SAMPLES));
                n += snprintf(&s_levels[n], sizeof(s_levels) - n, " %.0f/%.0f",
                              20 * log10(rms / 32768 + 1e-9), 20 * log10(peak[c] / 32768.0 + 1e-9));
                sumsq[c] = 0;
                peak[c] = 0;
            }
            // Load: wall time for these frames (60 s if none were lost at the
            // I2S DMA), ms per frame decimating and feeding the front end, and
            // frames out of the front end, sent, and dropped (stream full).
            static int64_t since;
            int64_t now = esp_timer_get_time();
            snprintf(&s_levels[n], sizeof(s_levels) - n, " wall=%.1fs dec=%.2fms feed=%.2fms out=%u sent=%u drop=%u",
                     since ? (now - since) / 1e6 : 0.0, s_us_decim / 1000.0 / frames, s_us_feed / 1000.0 / frames,
                     (unsigned)s_n_afe_out, (unsigned)s_n_sent, (unsigned)s_n_dropped);
            since = now;
            s_us_decim = s_us_feed = 0;
            s_n_afe_out = s_n_sent = s_n_dropped = 0;
            frames = 0;
            if (s_event_task) {
                xTaskNotify(s_event_task, EVENT_LEVELS, eSetBits);
            }
        }
        if (s_afe) {
            int64_t t1 = esp_timer_get_time();
            afe_feed(quad);   // the AFE task streams its output
            s_us_feed += esp_timer_get_time() - t1;
            continue;
        }
        // Never block: if the network is behind, drop this frame.
        if (xStreamBufferSpacesAvailable(s_audio) >= FRAME_BYTES) {
            xStreamBufferSend(s_audio, mono, FRAME_BYTES, 0);
        } else {
            s_n_dropped++;
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

// Why the last connection ended, for the next handshake (taco-voice logs it).
static char s_lost[24] = "none";

static int taco_connect(void)
{
    int sock = tcp_open();
    char hello[200];
    // spk = the rate it plays at; boot/up/lost are for diagnosis.
    snprintf(hello, sizeof(hello), "TACO %s %s afe=%s ch=%d spk=%d boot=%d up=%lld lost=%s\n", SAT_NAME,
             SAT_TOKEN, s_afe_status, s_out_ch, s_spk ? I2S_RATE : 0, (int)esp_reset_reason(),
             esp_timer_get_time() / 1000000, s_lost);
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

static void save_settings(void);

static void handle_line(char *line)
{
    char word[16] = {0}, arg[16] = {0}, where[16] = {0};
    if (sscanf(line, "%15s %15s %15s", word, arg, where) < 1) {
        return;
    }
    if (strcmp(word, "STATE") == 0 && arg[0]) {
        strlcpy(s_state, arg, sizeof(s_state));
        s_here = strcmp(where, "here") == 0;
        ESP_LOGI(TAG, "taco is %s%s", s_state, where[0] ? (s_here ? " (here)" : " (elsewhere)") : "");
    } else if (strcmp(word, "MUTE") == 0) {
        bool muted = strcmp(arg, "on") == 0;
        if (muted != s_house_muted) {
            ESP_LOGI(TAG, "taco %s", muted ? "muted (stop listening)" : "listening again");
        }
        s_house_muted = muted;
    } else if (strcmp(word, "AFE") == 0 && (strcmp(arg, "on") == 0 || strcmp(arg, "off") == 0)) {
        bool on = strcmp(arg, "on") == 0;
        if (on != s_afe_want) {
            s_afe_want = on;
            save_settings();
            s_afe_restart = true;
            ESP_LOGI(TAG, "speech front end %s; restarting", arg);
        }
    } else if (strcmp(word, "STOP") == 0) {
        s_play_stop = true;
    } else if (strcmp(word, "MSTOP") == 0) {
        s_music_stop = true;
    } else if (strcmp(word, "VOLUME") == 0 && arg[0]) {
        int vol = atoi(arg);
        if (s_spk && vol >= 0 && vol <= 100) {
            esp_codec_dev_set_out_vol(s_spk, vol);
        }
    } else if (strcmp(word, "UPDATE") == 0) {
        ESP_LOGI(TAG, "taco has new firmware");
        if (s_ota_task) {
            xTaskNotifyGive(s_ota_task);
        }
    } else {
        ESP_LOGW(TAG, "unknown control line: %s", line);
    }
}

// The receive task: reads everything Taco sends on the live connection as
// it arrives. Control lines, and after "AUDIO <n>" or "MUSIC <n>", n bytes of
// audio for that play buffer. net_task owns the socket: it hands it over in
// s_rx_sock, and takes it back (-1) before closing it.
static volatile int s_rx_sock = -1;
static volatile bool s_rx_busy;   // the receive task may be using a socket
static volatile bool s_rx_dead;   // the connection closed under it

// Queue audio bytes, whole samples only: a byte split across two reads waits
// in *carry for its partner.
static void queue_audio(StreamBufferHandle_t sb, volatile bool *stop, TickType_t wait,
                        const uint8_t *data, size_t n, int *carry)
{
    if (!sb || *stop) {
        *carry = -1;
        return;
    }
    if (*carry >= 0 && n) {
        uint8_t pair[2] = {(uint8_t)*carry, data[0]};
        xStreamBufferSend(sb, pair, 2, wait);
        *carry = -1;
        data++;
        n--;
    }
    size_t even = n & ~(size_t)1;
    if (even) {
        // Taco paces it, so there's room; if not, drop rather than stall.
        xStreamBufferSend(sb, data, even, wait);
    }
    if (n & 1) {
        *carry = data[n - 1];
    }
}

static void rx_task(void *arg)
{
    static uint8_t buf[2048];
    char line[64];
    while (true) {
        int sock = s_rx_sock;
        if (sock < 0) {
            s_rx_busy = false;
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        s_rx_busy = true;
        size_t len = 0, left = 0;   // line so far; audio bytes still to come
        bool to_music = false;
        int carry = -1;
        while (s_rx_sock == sock) {
            // Wait with select, not SO_RCVTIMEO: lwIP's receive timeout left the
            // socket reporting ENOTCONN after a few seconds of quiet.
            fd_set rd;
            FD_ZERO(&rd);
            FD_SET(sock, &rd);
            struct timeval tv = {.tv_sec = 0, .tv_usec = 250000};
            if (select(sock + 1, &rd, NULL, NULL, &tv) <= 0) {
                continue;   // quiet (or interrupted); check the socket is still ours
            }
            int n = recv(sock, buf, sizeof(buf), MSG_DONTWAIT);
            if (n == 0 || (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK)) {
                snprintf(s_lost, sizeof(s_lost), "rx:%d:%d", n, n < 0 ? errno : 0);
                s_rx_dead = true;
                break;
            }
            for (int i = 0; i < n;) {
                if (left) {
                    size_t k = left < (size_t)(n - i) ? left : (size_t)(n - i);
                    if (to_music) {
                        queue_audio(s_music, &s_music_stop, pdMS_TO_TICKS(500), &buf[i], k, &carry);
                    } else {
                        queue_audio(s_play, &s_play_stop, pdMS_TO_TICKS(100), &buf[i], k, &carry);
                    }
                    left -= k;
                    i += k;
                    continue;
                }
                char c = buf[i++];
                if (c != '\n') {
                    if (len + 1 < sizeof(line)) {
                        line[len++] = c;
                    }
                    continue;
                }
                line[len] = 0;
                len = 0;
                carry = -1;
                if (strncmp(line, "AUDIO ", 6) == 0 || strncmp(line, "MUSIC ", 6) == 0) {
                    to_music = line[0] == 'M';
                    left = strtoul(line + 6, NULL, 10);
                } else {
                    handle_line(line);
                }
            }
        }
        while (s_rx_sock == sock) {   // wait for net_task to take it back
            vTaskDelay(pdMS_TO_TICKS(20));
        }
    }
}

static void net_task(void *arg)
{
    static uint8_t frame[FRAME_BYTES * OUT_CH_MAX];
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
        s_rx_dead = false;
        s_rx_sock = sock;
        int64_t since = esp_timer_get_time();
        while (!s_rx_dead) {
            // A new image proves itself by streaming, and with mics that really
            // hear something: a broken audio path would otherwise stream zeros.
            if (s_pending_verify && s_mic_alive && esp_timer_get_time() - since > STREAM_OK_S * 1000000LL) {
                esp_ota_mark_app_valid_cancel_rollback();
                s_pending_verify = false;
                ESP_LOGI(TAG, "firmware %s confirmed", esp_app_get_description()->version);
            }
            if (s_afe_restart && !s_pending_verify) {
                // Not while a new image is on trial: that restart would roll it back.
                close(sock);
                vTaskDelay(pdMS_TO_TICKS(200));
                esp_restart();
            }
            size_t got = xStreamBufferReceive(s_audio, frame, FRAME_BYTES * s_out_ch, pdMS_TO_TICKS(1000));
            if (got == 0) {
                continue;
            }
            s_n_sent++;
            if (!send_all(sock, frame, got)) {
                snprintf(s_lost, sizeof(s_lost), "tx:%d", errno);
                break;
            }
        }
        ESP_LOGW(TAG, "connection lost");
        s_rx_sock = -1;
        shutdown(sock, SHUT_RDWR);   // wakes the receive task
        for (int i = 0; s_rx_busy && i < 100; i++) {
            vTaskDelay(pdMS_TO_TICKS(20));
        }
        close(sock);
        strlcpy(s_state, "idle", sizeof(s_state));   // don't leave the ring lit
        s_here = false;
    }
}

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

// -- Light and mute button --------------------------------------------------------

static void save_settings(void)
{
    nvs_handle_t nvs;
    if (nvs_open("taco", NVS_READWRITE, &nvs) == ESP_OK) {
        nvs_set_u8(nvs, "btn_mute", s_button_muted);
        nvs_set_u8(nvs, "afe", s_afe_want);
        nvs_commit(nvs);
        nvs_close(nvs);
    }
}

static void load_settings(void)
{
    nvs_handle_t nvs;
    uint8_t mute = 0, afe = 0;
    if (nvs_open("taco", NVS_READONLY, &nvs) == ESP_OK) {
        nvs_get_u8(nvs, "btn_mute", &mute);
        nvs_get_u8(nvs, "afe", &afe);
        nvs_close(nvs);
    }
    s_button_muted = mute;
    s_afe_want = afe;
    if (mute) {
        ESP_LOGI(TAG, "living-room mic muted by its button (press K2 to unmute)");
    }
}

// One frame of the ring for the current state; t_ms drives the animations.
static void ring_frame(led_strip_handle_t ring, uint32_t t_ms)
{
    for (int i = 0; i < RING_LEDS; i++) {
        uint8_t r = 0, g = 0, b = 0;
        if (MUTED()) {
            r = RING_MAX / 3;                               // dim red: not listening
        } else if (s_here && strcmp(s_state, "listening") == 0) {
            b = RING_MAX;                                   // solid blue: speak now
        } else if (s_here && strcmp(s_state, "thinking") == 0) {
            int head = (t_ms / 90) % RING_LEDS;             // a cyan dot chasing round
            int d = (i - head + RING_LEDS) % RING_LEDS;
            uint8_t v = d == 0 ? RING_MAX : d == RING_LEDS - 1 ? RING_MAX / 4 : RING_MAX / 16;
            g = v;
            b = v;
        } else if (s_here && strcmp(s_state, "speaking") == 0) {
            uint32_t phase = t_ms % 1200;                   // soft green breathing
            uint32_t up = phase < 600 ? phase : 1200 - phase;
            g = RING_MAX / 4 + (RING_MAX * 3 / 4) * up / 600;
        }
        led_strip_set_pixel(ring, i, r, g, b);
    }
    led_strip_refresh(ring);
}

static void ring_task(void *arg)
{
    led_strip_handle_t ring = NULL;
    led_strip_config_t strip = {
        .strip_gpio_num = PIN_LED_RING,
        .max_leds = RING_LEDS,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_RGB,
    };
    led_strip_rmt_config_t rmt = {.clk_src = RMT_CLK_SRC_DEFAULT, .resolution_hz = 10 * 1000 * 1000};
    if (led_strip_new_rmt_device(&strip, &rmt, &ring) != ESP_OK) {
        ESP_LOGE(TAG, "LED ring not found");
        ring = NULL;
    }
    esp_io_expander_handle_t exio = NULL;
    if (esp_io_expander_new_i2c_tca95xx_16bit(s_i2c, ESP_IO_EXPANDER_I2C_TCA9555_ADDRESS_000, &exio) != ESP_OK) {
        ESP_LOGE(TAG, "I/O expander not found; no mute button");
        exio = NULL;
    } else {
        esp_io_expander_set_dir(exio, KEY_MUTE, IO_EXPANDER_INPUT);
        esp_io_expander_set_dir(exio, AMP_ENABLE, IO_EXPANDER_OUTPUT);
        esp_io_expander_set_level(exio, AMP_ENABLE, 0);
    }
    bool amp = false;
    bool was_down = false;
    int same = 0;
    while (true) {
        if (exio && s_amp_on != amp) {
            amp = s_amp_on;
            esp_io_expander_set_level(exio, AMP_ENABLE, amp);
        }
        uint32_t t_ms = esp_timer_get_time() / 1000;
        if (ring) {
            ring_frame(ring, t_ms);
        }
        uint32_t level = KEY_MUTE;
        if (exio && esp_io_expander_get_level(exio, KEY_MUTE, &level) == ESP_OK) {
            bool down = !(level & KEY_MUTE);
            same = down == was_down ? 0 : same + 1;
            if (same >= 2) {                                // held for 2 reads: not bounce
                was_down = down;
                same = 0;
                if (down) {
                    s_button_muted = !s_button_muted;
                    save_settings();
                    ESP_LOGI(TAG, "mute button: living-room mic %s", s_button_muted ? "muted" : "on");
                    if (s_event_task) {
                        xTaskNotify(s_event_task, EVENT_MUTE, eSetBits);
                    }
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(40));
    }
}

// Tell taco-voice about the mute button (and channel levels), on a short
// connection of its own.
static bool send_event(const char *what)
{
    int sock = tcp_open();
    if (sock < 0) {
        return false;
    }
    struct timeval tv = {.tv_sec = 5};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    char line[320];
    snprintf(line, sizeof(line), "TACO-EVENT %s %s %s\n", SAT_NAME, SAT_TOKEN, what);
    bool ok = send_all(sock, (const uint8_t *)line, strlen(line)) && recv_line(sock, line, sizeof(line))
              && strcmp(line, "OK") == 0;
    close(sock);
    return ok;
}

static void event_task(void *arg)
{
    uint32_t todo = EVENT_MUTE;   // report the button once at boot, too
    while (true) {
        uint32_t bits = 0;
        xTaskNotifyWait(0, UINT32_MAX, &bits, todo ? 0 : portMAX_DELAY);
        todo |= bits;
        xEventGroupWaitBits(s_wifi, WIFI_UP, pdFALSE, pdTRUE, portMAX_DELAY);
        if ((todo & EVENT_MUTE) && send_event(s_button_muted ? "MUTE on" : "MUTE off")) {
            todo &= ~EVENT_MUTE;
        }
        if (todo & EVENT_LEVELS) {
            char what[256];
            snprintf(what, sizeof(what), "LEVELS%s heap=%uK/%uK", s_levels,
                     (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
                     (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024));
            send_event(what);              // a missed report doesn't matter
            todo &= ~EVENT_LEVELS;
        }
        if (todo) {
            vTaskDelay(pdMS_TO_TICKS(5000));   // Taco not there yet; try again
        }
    }
}

// -- Firmware updates -------------------------------------------------------------

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
        // Sleep until the next check, or until taco-voice says "UPDATE".
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(OTA_CHECK_S * 1000));
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
    load_settings();
    // In PSRAM: internal RAM is what Wi-Fi, lwIP and the I2S DMA need.
    s_audio = xStreamBufferCreateWithCaps(STREAM_BYTES, FRAME_BYTES, MALLOC_CAP_SPIRAM);
    esp_codec_dev_handle_t mic = mic_start();
    if (s_spk) {
        s_play = xStreamBufferCreateWithCaps(PLAY_BYTES, 2, MALLOC_CAP_SPIRAM);
        s_music = xStreamBufferCreateWithCaps(MUSIC_BYTES, 2, MALLOC_CAP_SPIRAM);
        if (!s_play || !s_music) {
            s_spk = NULL;
        }
    }
    ESP_LOGI(TAG, "PSRAM %u KB", (unsigned)(esp_psram_is_initialized() ? esp_psram_get_size() / 1024 : 0));
    if (s_afe_want) {
        if (afe_start()) {
            s_afe_status = "on";
            s_out_ch = 2;
            xTaskCreatePinnedToCore(afe_task, "afe", 8192, NULL, 5, NULL, 1);
        } else {
            s_afe_status = "failed";   // taco-voice won't ask again, so no restart loop
            ESP_LOGE(TAG, "speech front end didn't start; streaming the mic mix");
        }
    }
    wifi_start();
    // Core 0: reading I2S, decimating and feeding the front end (its AEC runs
    // in feed). Core 1 has the front end's fetch side (BSS) and the speaker.
    s_bus = xStreamBufferCreateWithCaps(BUS_FRAME_BYTES * 6, I2S_CHUNK_BYTES, MALLOC_CAP_SPIRAM);
    xTaskCreatePinnedToCore(i2s_task, "i2s", 3072, mic, 8, NULL, 0);
    xTaskCreatePinnedToCore(mic_task, "mic", 4096, NULL, 6, NULL, 0);
    xTaskCreatePinnedToCore(net_task, "net", 4096, NULL, 5, NULL, 0);
    xTaskCreatePinnedToCore(rx_task, "rx", 4096, NULL, 5, NULL, 0);
    xTaskCreatePinnedToCore(ota_task, "ota", 6144, NULL, 3, &s_ota_task, 0);
    xTaskCreatePinnedToCore(ring_task, "ring", 4096, NULL, 2, NULL, 0);
    xTaskCreatePinnedToCore(event_task, "event", 4096, NULL, 3, &s_event_task, 0);
    if (s_spk) {
        xTaskCreatePinnedToCore(speaker_task, "speaker", 4096, NULL, 6, NULL, 1);
    }
}
