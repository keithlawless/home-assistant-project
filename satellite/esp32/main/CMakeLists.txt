idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       REQUIRES esp_wifi esp_netif esp_event nvs_flash lwip esp_driver_i2c esp_driver_i2s esp_psram
                                app_update esp_app_format mbedtls)
# The project builds at -Og (debug); the 48 kHz decimator needs real
# optimization, so this file gets -O2.
target_compile_options(${COMPONENT_LIB} PRIVATE -O2)
# TACO_SAT=bedroom builds with secrets.bedroom.h (another board's name,
# port and token); unset, secrets.h. The image's build differs per board.
if(DEFINED ENV{TACO_SAT} AND NOT "$ENV{TACO_SAT}" STREQUAL "")
    target_compile_definitions(${COMPONENT_LIB} PRIVATE TACO_SECRETS="secrets.$ENV{TACO_SAT}.h")
endif()
