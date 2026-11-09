idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi esp_netif esp_event nvs_flash lwip esp_driver_i2c esp_driver_i2s esp_psram
                                app_update esp_app_format mbedtls)
